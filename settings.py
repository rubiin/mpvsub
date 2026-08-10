"""Persistent user settings.

Stored as a small JSON document under the XDG config directory so the app
remembers the last language selection, sort mode/direction, download
folder, encoding and credentials between runs. The window size is not
persisted — the window is fixed-size (overridable via --width/--height).
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from models import LANGUAGE_CATALOG

log = logging.getLogger(__name__)

_KNOWN_LANG_CODES = set(LANGUAGE_CATALOG)

#: locale → OpenSubtitles code aliases for the region-less codes
_LOCALE_ALIASES = {
    "pt": "pt-br",
    "zh": "zh-cn",
    "az": "az-az",
}

APP_ID = "org.mpvsub.SubtitleDownloader"

CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "mpvsub-subtitles"
DATA_DIR = Path(
    os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")
) / "mpvsub-subtitles"
SUBTITLE_DIR = Path.home() / ".local/share" / "mpv" / "subtitles"

SETTINGS_FILE = CONFIG_DIR / "settings.json"

#: sort modes — (key, dropdown label, API ``order_by`` field;
#: ``None`` = client-side best match). UI order must match this.
SORT_MODES: tuple[tuple[str, str, Optional[str]], ...] = (
    ("score", "Best match", None),
    ("downloads", "Downloads", "download_count"),
    ("new_downloads", "New downloads", "new_download_count"),
    ("rating", "Ratings", "ratings"),
    ("votes", "Votes", "votes"),
    ("newest", "Upload date", "upload_date"),
    ("trusted", "Trusted uploader", "from_trusted"),
    ("hd", "HD", "hd"),
    ("release", "Release", "release"),
)


#: constant mixed into the obfuscation key so the same machine-id never
#: yields the same bytes as another app using this scheme
_OBFUSCATION_PEPPER = b"mpvsub-subtitles:v1"
_machine_key_cache: Optional[bytes] = None


def _machine_key() -> bytes:
    """Machine-local obfuscation key (cached).

    From ``/etc/machine-id`` (hostname fallback), so a copied settings.json
    can't be decoded on another machine — just re-enter the password.
    """
    global _machine_key_cache
    if _machine_key_cache is None:
        try:
            machine_id = Path("/etc/machine-id").read_text(encoding="utf-8").strip()
        except OSError:
            machine_id = socket.gethostname()
        _machine_key_cache = hashlib.sha256(
            _OBFUSCATION_PEPPER + machine_id.encode("utf-8")
        ).digest()
    return _machine_key_cache


def _obfuscate(text: str) -> str:
    """XOR-obfuscate *text* with the machine key; base64-encoded, no padding."""
    if not text:
        return ""
    key = _machine_key()
    data = text.encode("utf-8")
    xored = bytes(b ^ key[i % len(key)] for i, b in enumerate(data))
    return base64.urlsafe_b64encode(xored).decode("ascii").rstrip("=")


def _deobfuscate(code: str) -> str:
    """Inverse of :func:`_obfuscate`; returns "" on any decode failure
    (foreign key or corrupt data) rather than showing mojibake.
    """
    if not code:
        return ""
    key = _machine_key()
    try:
        padded = code + "=" * (-len(code) % 4)
        xored = base64.urlsafe_b64decode(padded)
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(xored)).decode(
            "utf-8"
        )
    except (ValueError, TypeError, UnicodeDecodeError):
        return ""


def system_language() -> str:
    """Default subtitle language from the user's locale; English fallback."""
    raw = (
        os.environ.get("LC_ALL")
        or os.environ.get("LC_MESSAGES")
        or os.environ.get("LANG")
        or ""
    )
    code = raw.split(".")[0].split("_")[0].strip().lower()
    # the catalog (OpenSubtitles codes) uses regional variants where the
    # system locale only gives the base code
    code = _LOCALE_ALIASES.get(code, code)
    if code in _KNOWN_LANG_CODES:
        return code
    return "en"


@dataclass(slots=True)
class Settings:
    languages: list[str] = field(default_factory=lambda: [system_language()])
    sort: str = "score"
    sort_direction: str = "desc"  # "desc" | "asc" (used with the API order_by)
    max_results: int = 120
    download_dir: str = str(SUBTITLE_DIR)
    encoding: str = "utf-8"
    timeout: float = 20.0

    #: OpenSubtitles credentials. Env vars (OPENSUBTITLES_USERNAME /
    #: OPENSUBTITLES_PASSWORD) take precedence; every request needs an
    #: account. ``username`` is stored in plaintext, ``password`` is
    #: persisted obfuscated (``password_obfuscated``) — see :func:`_obfuscate`.
    username: str = ""
    password: str = ""

    # -- persistence --------------------------------------------------------

    def save(self) -> None:
        data = asdict(self)
        data.pop("password", None)
        data["password_obfuscated"] = _obfuscate(self.password)
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(
                json.dumps(data, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
        except OSError as exc:  # pragma: no cover - best effort only
            log.warning("could not save settings: %s", exc)

    @classmethod
    def load(cls) -> "Settings":
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        if "password_obfuscated" in data:
            data["password"] = _deobfuscate(str(data.pop("password_obfuscated") or ""))
        # legacy plaintext "password" migrates to the obfuscated form on next save
        known = {f.name for f in cls.__dataclass_fields__.values()}
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(**kwargs)
