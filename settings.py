"""Persistent user settings.

Stored as a small JSON document under the XDG config directory so the app
remembers the last language selection, sort order, window size and download
folder between runs.
"""

from __future__ import annotations

import json
import logging
import os
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

APP_ID = "org.mpvui.SubtitleDownloader"

CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
) / "mpvui-subtitles"
DATA_DIR = Path(
    os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")
) / "mpvui-subtitles"
SUBTITLE_DIR = Path.home() / ".local/share" / "mpv" / "subtitles"

SETTINGS_FILE = CONFIG_DIR / "settings.json"

#: sort modes — (settings key, dropdown label, API ``order_by`` field).  The
#: API field list mirrors the official VLSub extension's sort options; a
#: ``None`` API field means "best match" (client-side scoring, no server
#: ordering).  Dropdown order must match this order in the UI.
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


def system_language() -> str:
    """Best-effort default subtitle language from the user's locale.

    Maps the LANG/LC_MESSAGES language code onto the picker catalog;
    falls back to English for unset/POSIX locales and unknown codes.
    """
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

    #: OpenSubtitles.com credentials (also read from the OPENSUBTITLES_API_KEY
    #: / _USERNAME / _PASSWORD environment variables, which take precedence).
    #: Leave api_key empty to use the default key shipped in the reference
    #: VLSub extension.
    api_key: str = ""
    username: str = ""
    password: str = ""

    #: Pre-issued session token (grab one at https://opensubtitles.com).  When
    #: set it is used as-is on every request — no login round-trip — and takes
    #: precedence over username/password.  Overridable with the
    #: OPENSUBTITLES_TOKEN environment variable.
    token: str = ""

    # -- persistence --------------------------------------------------------

    def save(self) -> None:
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(
                json.dumps(asdict(self), indent=2, ensure_ascii=False),
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
        known = {f.name for f in cls.__dataclass_fields__.values()}
        kwargs = {k: v for k, v in data.items() if k in known}
        return cls(**kwargs)
