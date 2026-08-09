#!/usr/bin/env python3
"""
sub_helper.py — JSON backend for the mpv "sub" subtitle downloader UI.

Speaks JSON on stdout (one object per run) so an mpv Lua script can drive a
VLC-style subtitle picker. Human-readable logs go to stderr.

Modes
-----
  search   <video> [--lang en] [--lang fr] ... [--providers a,b] [--query "..."]
           -> {"ok": true, "video": {...}, "subs": [...]}

  download <video> <provider> <subid> [--lang en] [--dir DIR] [--encoding utf-8]
           -> {"ok": true, "file": "/abs/path/Video.en.srt", ...}

Credentials
-----------
OpenSubtitles.com requires a free account to download. Credentials are picked
up from, in order:
  1. env vars  SUBLIMINAL_PROVIDER_OPENSUBTITLESCOM_USERNAME / _PASSWORD
  2. ~/.config/subliminal/subliminal.toml  ->  [provider.opensubtitlescom]
                                               username = "..."  password = "..."
Searching works without credentials; downloading will report a clear error
until they are configured.

The interpreter discovery works even when the system python3 cannot import
subliminal: the script re-executes itself with the venv python that owns the
`subliminal` executable on PATH (parsed from its shebang).
"""

from __future__ import annotations

import dataclasses
import json
import os
import shutil
import sys
import traceback

# --------------------------------------------------------------------------
# interpreter bootstrap (re-exec with the subliminal venv python if needed)
# --------------------------------------------------------------------------


def _venv_python() -> str | None:
    exe = shutil.which("subliminal")
    if not exe:
        return None
    try:
        with open(exe, "r", errors="replace") as f:
            first = f.readline()
        if first.startswith("#!"):
            py = first[2:].strip()
            if py and os.path.exists(py):
                return py
    except OSError:
        pass
    return None


try:
    import subliminal  # noqa: F401
except ImportError:  # pragma: no cover - environment dependent
    py = _venv_python()
    if py and py != sys.executable and os.path.exists(py):
        os.execv(py, [py] + sys.argv)

import logging  # noqa: E402

import subliminal  # noqa: E402
from babelfish import Language  # noqa: E402
from subliminal.cache import region  # noqa: E402
from subliminal.exceptions import AuthenticationError  # noqa: E402
from subliminal.refiners.hash import refine as hash_refine  # noqa: E402
from subliminal.video import Movie, Video  # noqa: E402

logging.basicConfig(level=logging.ERROR, stream=sys.stderr)

try:  # dogpile >= 1.3 renamed the mutex internals; subliminal pins < 1.3
    from dogpile.cache.util import MutexLock
except ImportError:  # pragma: no cover
    from subliminal.cli.helpers import MutexLock

DEFAULT_PROVIDERS = ["opensubtitlescom", "podnapisi", "subtis", "tvsubtitles"]
CACHE_FILE = os.path.join(
    os.path.expanduser("~/.cache/subliminal"), "subliminal.dbm"
)

# --------------------------------------------------------------------------
# config: credentials from env / toml, matching the CLI behaviour
# --------------------------------------------------------------------------


def _read_toml_config() -> dict:
    """Parse ~/.config/subliminal/subliminal.toml, tolerating its absence."""
    path = os.path.expanduser("~/.config/subliminal/subliminal.toml")
    if not os.path.isfile(path):
        return {}
    try:
        import tomllib  # python 3.11+
    except ImportError:  # pragma: no cover
        try:
            import tomli as tomllib  # type: ignore
        except ImportError:
            return {}
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        return data.get("provider", {}) or {}
    except Exception:
        return {}


def provider_configs(extra: dict | None = None) -> dict:
    """Build the provider_configs mapping passed to the subliminal pool."""
    cfg: dict = {}
    toml = _read_toml_config()

    for name in DEFAULT_PROVIDERS:
        opts: dict = {}
        for key in ("username", "password", "api_key", "timeout"):
            env = os.environ.get(
                f"SUBLIMINAL_PROVIDER_{name.upper()}_{key.upper()}"
            )
            if env:
                opts[key] = env
        t = toml.get(name) or {}
        for key in ("username", "password", "api_key", "timeout"):
            if key in t and key not in opts:
                opts[key] = t[key]
        if opts:
            cfg[name] = opts

    # providers with no configured timeout still get a sane default
    for name in DEFAULT_PROVIDERS:
        cfg.setdefault(name, {})
        cfg[name].setdefault("timeout", 20)

    if extra:
        for name, opts in extra.items():
            cfg.setdefault(name, {})
            cfg[name].update(opts)
    return cfg


def configure_cache() -> None:
    """Configure the shared dogpile region exactly like the subliminal CLI."""
    os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
    region.configure(
        "dogpile.cache.dbm",
        expiration_time=86400 * 30,
        arguments={"filename": CACHE_FILE, "lock_factory": MutexLock},
    )


def parse_languages(codes: list[str]) -> set:
    return {Language.fromietf(c.strip()) for c in codes if c.strip()}


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False))
    sys.stdout.write("\n")
    sys.stdout.flush()


def emit_error(message: str, hint: str | None = None) -> None:
    emit({"ok": False, "error": message, "hint": hint})


def sub_to_json(s: subliminal.Subtitle) -> dict:
    d = {
        "provider": s.provider_name,
        "id": getattr(s, "id", None),
        "language": str(s.language),
        "format": getattr(s, "format", None),
        "download_count": getattr(s, "download_count", None),
        "page_link": getattr(s, "page_link", None),
    }
    for attr in ("name", "release_info", "notes", "uploader_name"):
        val = getattr(s, attr, None)
        if val:
            d[attr] = str(val)[:300]
    if hasattr(s, "moviehash_match"):
        d["hash_match"] = bool(s.moviehash_match)
    return d


def video_to_json(v: Video) -> dict:
    d = {
        "name": v.name,
        "path": getattr(v, "original_path", None) or v.name,
        "type": "episode" if isinstance(v, subliminal.video.Episode) else "movie",
        "hashes": sorted(v.hashes.keys()),
        "existing_languages": sorted(str(l) for l in v.subtitle_languages),
    }
    if isinstance(v, subliminal.video.Episode):
        d.update(series=v.series, season=v.season, episode=v.episode)
        if v.year:
            d["year"] = v.year
    elif getattr(v, "year", None):
        d["year"] = v.year
    return d


def build_search_video(path: str, query: str | None) -> Video:
    v = subliminal.scan_video(path)
    if query:
        # manual search: drop the hash/identity, search by the query text
        return Movie(name=query)
    try:
        hash_refine(v)  # compute opensubtitles-style hashes (no network)
    except Exception:
        pass
    return v


def do_search(path: str, langs: set, providers: list[str],
              query: str | None, max_results: int) -> None:
    video = build_search_video(path, query)
    result = subliminal.list_subtitles(
        {video},
        langs,
        providers=providers,
        provider_configs=provider_configs(),
    )
    subs = result.get(video, [])
    subs.sort(key=lambda s: (getattr(s, "download_count", 0) or 0), reverse=True)
    emit({
        "ok": True,
        "mode": "search",
        "video": video_to_json(video),
        "langs": sorted(str(l) for l in langs),
        "providers": providers,
        "subs": [sub_to_json(s) for s in subs[:max_results]],
    })


def do_download(path: str, provider: str, subid: str, lang: str,
                directory: str | None, encoding: str) -> None:
    video = subliminal.scan_video(path)
    try:
        hash_refine(video)
    except Exception:
        pass
    langs = parse_languages([lang])
    result = subliminal.list_subtitles(
        {video},
        langs,
        providers=[provider],
        provider_configs=provider_configs(),
    )
    match = None
    for s in result.get(video, []):
        if str(getattr(s, "id", "")) == subid:
            match = s
            break
    if match is None:
        emit_error(
            f"Subtitle {provider}/{subid} was not found in a fresh search. "
            "It may have been removed upstream — try refreshing the list."
        )
        return

    try:
        subliminal.download_subtitles([match], provider_configs=provider_configs())
    except AuthenticationError as e:
        emit_error(
            str(e) or "Authentication failed.",
            hint=(
                "Add OpenSubtitles.com credentials to "
                "~/.config/subliminal/subliminal.toml or set "
                "SUBLIMINAL_PROVIDER_OPENSUBTITLESCOM_USERNAME/PASSWORD. "
                "See the README."
            ),
        )
        return
    except Exception as e:
        emit_error(f"Download failed: {e}")
        return

    if not match.content:
        emit_error(
            "Downloaded an empty result. This provider may need "
            "credentials (see README) or the file may be unavailable."
        )
        return

    if directory is None:
        directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    try:
        saved = subliminal.save_subtitles(
            video, [match], directory=directory, encoding=encoding or None
        )
    except Exception as e:
        emit_error(f"Could not write subtitle file: {e}")
        return
    if not saved:
        emit_error("The subtitle was fetched but could not be written to disk.")
        return
    p = saved[0].content_path
    emit({
        "ok": True,
        "mode": "download",
        "file": p,
        "basename": os.path.basename(p),
        "language": str(saved[0].language),
        "size": os.path.getsize(p),
    })


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="subliminal JSON backend")
    sub = parser.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("search")
    sp.add_argument("video")
    sp.add_argument("-l", "--lang", action="append", default=["en"])
    sp.add_argument("-p", "--providers", default=",".join(DEFAULT_PROVIDERS))
    sp.add_argument("--query", default=None)
    sp.add_argument("--max", type=int, default=100)

    dp = sub.add_parser("download")
    dp.add_argument("video")
    dp.add_argument("provider")
    dp.add_argument("subid")
    dp.add_argument("-l", "--lang", default="en")
    dp.add_argument("-d", "--dir", default=None)
    dp.add_argument("-e", "--encoding", default="utf-8")

    args = parser.parse_args(argv)
    try:
        configure_cache()
        if args.command == "search":
            providers = [p.strip() for p in args.providers.split(",") if p.strip()]
            do_search(
                args.video,
                parse_languages(args.lang),
                providers,
                args.query,
                args.max,
            )
        else:
            do_download(
                args.video, args.provider, args.subid,
                args.lang, args.dir, args.encoding,
            )
        return 0
    except AuthenticationError as e:
        emit_error(
            str(e) or "Authentication failed.",
            hint=(
                "OpenSubtitles.com needs a free account. Add credentials to "
                "~/.config/subliminal/subliminal.toml or set "
                "SUBLIMINAL_PROVIDER_OPENSUBTITLESCOM_USERNAME/PASSWORD."
            ),
        )
        return 1
    except FileNotFoundError as e:
        emit_error(f"File not found: {e}")
        return 1
    except Exception as e:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        emit_error(f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
