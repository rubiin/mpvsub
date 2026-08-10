#!/usr/bin/env python3
"""Plain-assert tests for the non-GUI logic.

Runs without pytest or a display:

    python3 tests/test_logic.py

Covers: models, settings persistence, the TTL cache and the search
scoring / sorting / filtering helpers.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cache import SearchCache  # noqa: E402
from models import SearchQuery, SubtitleResult, VideoInfo  # noqa: E402
from search import (  # noqa: E402
    build_query,
    build_query_fields,
    cache_key,
    score_subtitle,
    sort_results,
    video_from_path,
)
from settings import Settings  # noqa: E402


def make_sub(**overrides) -> SubtitleResult:
    base = dict(
        id="1",
        provider="podnapisi",
        language="en",
        language_name="English",
        name="Example",
        release_info="1080p",
        rating=7.0,
        downloads=100,
        upload_date=None,
        hearing_impaired=False,
        format="srt",
        page_link=None,
        score=0.0,
        hash_match=False,
        raw=None,
    )
    base.update(overrides)
    return SubtitleResult(**base)


def test_settings_roundtrip() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        import settings as settings_mod

        settings_mod.CONFIG_DIR = Path(tmp)
        settings_mod.SETTINGS_FILE = Path(tmp) / "settings.json"
        s = Settings(languages=["en", "es"], sort="downloads")
        s.save()
        loaded = Settings.load()
        assert loaded.languages == ["en", "es"]
        assert loaded.sort == "downloads"
        assert loaded.download_dir  # defaults preserved


def test_settings_load_missing_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        import settings as settings_mod

        settings_mod.SETTINGS_FILE = Path(tmp) / "nope.json"
        # system_language() reads LC_ALL first, so pin all three to a POSIX
        # locale to exercise the English fallback deterministically.
        old = {k: os.environ.get(k) for k in ("LC_ALL", "LC_MESSAGES", "LANG")}
        for k in old:
            os.environ[k] = "C"
        try:
            s = Settings.load()
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
        assert s.languages == ["en"]


def test_settings_password_obfuscated_roundtrip() -> None:
    """Password is persisted obfuscated (never plaintext) and round-trips."""
    with tempfile.TemporaryDirectory() as tmp:
        import settings as settings_mod

        settings_mod.CONFIG_DIR = Path(tmp)
        settings_mod.SETTINGS_FILE = Path(tmp) / "settings.json"
        s = Settings(username="you@example.com", password="hunter2!secret")
        s.save()

        raw = settings_mod.SETTINGS_FILE.read_text(encoding="utf-8")
        assert "hunter2!secret" not in raw
        assert "password_obfuscated" in raw
        assert '"password"' not in raw

        loaded = Settings.load()
        assert loaded.username == "you@example.com"
        assert loaded.password == "hunter2!secret"


def test_settings_legacy_plaintext_password_migrates() -> None:
    """A legacy plaintext ``password`` key is read and obfuscated on save."""
    with tempfile.TemporaryDirectory() as tmp:
        import settings as settings_mod

        settings_mod.CONFIG_DIR = Path(tmp)
        settings_mod.SETTINGS_FILE = Path(tmp) / "settings.json"
        settings_mod.SETTINGS_FILE.write_text(
            json.dumps({"username": "u", "password": "legacy-plain"}),
            encoding="utf-8",
        )

        loaded = Settings.load()
        assert loaded.password == "legacy-plain"
        loaded.save()

        raw = settings_mod.SETTINGS_FILE.read_text(encoding="utf-8")
        assert "legacy-plain" not in raw
        assert "password_obfuscated" in raw
        assert Settings.load().password == "legacy-plain"


def test_settings_foreign_or_corrupt_obfuscation_yields_empty() -> None:
    """An obfuscated value from another machine (or corrupted) reads as ""."""
    with tempfile.TemporaryDirectory() as tmp:
        import settings as settings_mod

        settings_mod.CONFIG_DIR = Path(tmp)
        settings_mod.SETTINGS_FILE = Path(tmp) / "settings.json"
        # a zero key makes XOR the identity, so the stored bytes are the raw
        # payload — simulate a foreign key with bytes that aren't valid UTF-8
        settings_mod._machine_key_cache = b"\x00" * 32
        try:
            settings_mod.SETTINGS_FILE.write_text(
                json.dumps(
                    {
                        "password_obfuscated": base64.urlsafe_b64encode(
                            b"\xff\xfe\xfd\xfc"
                        ).decode("ascii"),
                    }
                ),
                encoding="utf-8",
            )
            assert Settings.load().password == ""

            # corrupt base64 also degrades to ""
            settings_mod.SETTINGS_FILE.write_text(
                json.dumps({"password_obfuscated": "### not base64 ###"}),
                encoding="utf-8",
            )
            assert Settings.load().password == ""
        finally:
            settings_mod._machine_key_cache = None


def test_cache_ttl() -> None:
    cache = SearchCache(ttl=0.05)
    item = make_sub()
    assert cache.get("k") is None
    cache.set("k", [item])
    assert cache.get("k") == [item]
    import time

    time.sleep(0.1)
    assert cache.get("k") is None


def test_cache_bounded() -> None:
    cache = SearchCache(ttl=60)
    for i in range(70):
        cache.set(f"k{i}", [make_sub(id=str(i))])
    assert len(cache) <= 64


def test_build_query_manual_movie() -> None:
    q = build_query(None, "Inception 2010", ["en"])
    assert q.text == "Inception 2010"
    assert q.kind == "movie"
    assert q.use_file is False


def test_build_query_manual_episode() -> None:
    q = build_query(None, "Breaking Bad S01E02", ["en"])
    assert q.kind == "episode"
    assert q.season == 1
    assert q.episode == 2


def test_build_query_fields() -> None:
    q = build_query_fields("Better Call Saul", 1, 2, ["en"])
    assert q.kind == "episode"
    assert q.season == 1 and q.episode == 2
    q2 = build_query_fields("Inception", None, None, ["es"])
    assert q2.kind == "movie"
    assert q2.languages == ("es",)


def test_build_query_auto_file() -> None:
    with tempfile.NamedTemporaryFile(suffix=".mkv", delete=False) as f:
        name = f.name
    try:
        video = video_from_path(name)
        q = build_query(video, "", ["en"])
        assert q.use_file is True
        assert q.text  # title prefilled
    finally:
        Path(name).unlink(missing_ok=True)


def test_video_from_path_episode() -> None:
    video = video_from_path("Better.Call.Saul.S03E04.1080p.mkv")
    assert video.kind == "episode"
    assert video.season == 3
    assert video.episode == 4
    assert video.series


def test_score_hash_match() -> None:
    video = VideoInfo(path="/x/a.mkv", filename="a.mkv", title="A", year=2010)
    q = SearchQuery(text="A", use_file=True)
    s = make_sub(hash_match=True)
    assert score_subtitle(s, video, q) >= 4.0


def test_sort_modes() -> None:
    subs = [
        make_sub(id="a", rating=5.0, downloads=10, upload_date=datetime.now()),
        make_sub(id="b", rating=9.0, downloads=500, upload_date=datetime.now() - timedelta(days=2)),
        make_sub(id="c", rating=None, downloads=300, upload_date=None),
    ]
    by_rating = sort_results(subs, "rating", None, SearchQuery())
    assert [s.id for s in by_rating] == ["b", "a", "c"]
    by_downloads = sort_results(subs, "downloads", None, SearchQuery())
    assert [s.id for s in by_downloads] == ["b", "c", "a"]
    by_newest = sort_results(subs, "newest", None, SearchQuery())
    assert by_newest[0].id == "a"  # newest first
    assert by_newest[-1].id == "c"  # no date last


def test_score_default_sort() -> None:
    subs = [make_sub(id="plain"), make_sub(id="hash", hash_match=True)]
    by_score = sort_results(subs, "score", None, SearchQuery())
    assert by_score[0].id == "hash"


def test_cache_key_differs_by_language() -> None:
    q1 = SearchQuery(text="A", languages=("en",))
    q2 = SearchQuery(text="A", languages=("es",))
    assert cache_key(q1, None) != cache_key(q2, None)


def main() -> None:
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"FAIL {fn.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"ERR  {fn.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
