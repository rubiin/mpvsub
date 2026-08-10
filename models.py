"""Core data models shared across the application.

Everything the UI, IPC layer and the OpenSubtitles client exchange is
described here as a plain dataclass.  No GTK imports — these types stay
usable from tests and from the asyncio thread.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

# ---------------------------------------------------------------------------
# language catalog
# ---------------------------------------------------------------------------

#: Language picker — OpenSubtitles code → display name (the values the
#: API's ``languages`` parameter accepts, ``en``, ``pt-br``, ``zh-cn``, …).
LANGUAGE_CATALOG: dict[str, str] = {
    "ab": "Abkhazian",
    "af": "Afrikaans",
    "sq": "Albanian",
    "am": "Amharic",
    "ar": "Arabic",
    "an": "Aragonese",
    "hy": "Armenian",
    "as": "Assamese",
    "at": "Asturian",
    "az-az": "Azerbaijani",
    "az-zb": "South Azerbaijani",
    "eu": "Basque",
    "be": "Belarusian",
    "bn": "Bengali",
    "bs": "Bosnian",
    "br": "Breton",
    "my": "Burmese",
    "ca": "Catalan",
    "zh-ca": "Chinese (Cantonese)",
    "ze": "Chinese bilingual",
    "zh-cn": "Chinese (simplified)",
    "zh-tw": "Chinese (traditional)",
    "hr": "Croatian",
    "cs": "Czech",
    "da": "Danish",
    "pr": "Dari",
    "nl": "Dutch",
    "en": "English",
    "eo": "Esperanto",
    "et": "Estonian",
    "ex": "Extremaduran",
    "tl": "Tagalog",
    "fi": "Finnish",
    "fr": "French",
    "gd": "Gaelic",
    "gl": "Galician",
    "ka": "Georgian",
    "de": "German",
    "el": "Greek",
    "he": "Hebrew",
    "hi": "Hindi",
    "hu": "Hungarian",
    "is": "Icelandic",
    "ig": "Igbo",
    "id": "Indonesian",
    "ia": "Interlingua",
    "it": "Italian",
    "ja": "Japanese",
    "kn": "Kannada",
    "kk": "Kazakh",
    "km": "Khmer",
    "ko": "Korean",
    "ku": "Kurdish",
    "lv": "Latvian",
    "lt": "Lithuanian",
    "lb": "Luxembourgish",
    "ma": "Manipuri",
    "mk": "Macedonian",
    "ml": "Malayalam",
    "ms": "Malay",
    "mr": "Marathi",
    "me": "Montenegrin",
    "mn": "Mongolian",
    "nv": "Navajo",
    "ne": "Nepali",
    "no": "Norwegian",
    "oc": "Occitan",
    "or": "Odia",
    "pm": "Portuguese (MZ)",
    "pt-pt": "Portuguese",
    "pt-br": "Portuguese (BR)",
    "ps": "Pushto",
    "ro": "Romanian",
    "ru": "Russian",
    "se": "Northern Sami",
    "sx": "Santali",
    "sd": "Sindhi",
    "si": "Sinhalese",
    "sk": "Slovak",
    "sl": "Slovenian",
    "so": "Somali",
    "es": "Spanish",
    "ea": "Spanish (LA)",
    "sp": "Spanish (EU)",
    "sv": "Swedish",
    "sy": "Syriac",
    "ta": "Tamil",
    "tt": "Tatar",
    "te": "Telugu",
    "tm-td": "Tetum",
    "th": "Thai",
    "tp": "Toki Pona",
    "tr": "Turkish",
    "tk": "Turkmen",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "uz": "Uzbek",
    "vi": "Vietnamese",
    "cy": "Welsh",
}

#: Flag emoji per language; entries without one render name-only.
LANGUAGE_FLAGS: dict[str, str] = {
    "ar": "🇸🇦", "hy": "🇦🇲", "eu": "🇪🇸", "bn": "🇧🇩", "bs": "🇧🇦",
    "my": "🇲🇲", "ca": "🇪🇸", "zh-ca": "🇭🇰", "zh-cn": "🇨🇳", "zh-tw": "🇹🇼",
    "hr": "🇭🇷", "cs": "🇨🇿", "da": "🇩🇰", "pr": "🇦🇫", "nl": "🇳🇱",
    "en": "🇬🇧", "eo": "🇪🇺", "et": "🇪🇪", "tl": "🇵🇭", "fi": "🇫🇮",
    "fr": "🇫🇷", "gd": "🏴󠁧󠁢󠁳󠁣󠁴󠁿", "gl": "🇪🇸", "ka": "🇬🇪", "de": "🇩🇪",
    "el": "🇬🇷", "he": "🇮🇱", "hi": "🇮🇳", "hu": "🇭🇺", "is": "🇮🇸",
    "id": "🇮🇩", "it": "🇮🇹", "ja": "🇯🇵", "kn": "🇮🇳", "kk": "🇰🇿",
    "km": "🇰🇭", "ko": "🇰🇷", "lv": "🇱🇻", "lt": "🇱🇹", "lb": "🇱🇺",
    "mk": "🇲🇰", "ml": "🇮🇳", "ms": "🇲🇾", "mr": "🇮🇳", "mn": "🇲🇳",
    "ne": "🇳🇵", "no": "🇳🇴", "oc": "🇫🇷", "or": "🇮🇳", "pm": "🇲🇿",
    "pt-pt": "🇵🇹", "pt-br": "🇧🇷", "ps": "🇦🇫", "ro": "🇷🇴", "ru": "🇷🇺",
    "sd": "🇵🇰", "si": "🇱🇰", "sk": "🇸🇰", "sl": "🇸🇮", "so": "🇸🇴",
    "es": "🇪🇸", "sv": "🇸🇪", "sy": "🇸🇾", "ta": "🇮🇳", "te": "🇮🇳",
    "th": "🇹🇭", "tr": "🇹🇷", "tk": "🇹🇲", "uk": "🇺🇦", "ur": "🇵🇰",
    "uz": "🇺🇿", "vi": "🇻🇳", "cy": "🏴󠁧󠁢󠁷󠁬󠁳󠁿",
}


def language_name(code: str) -> str:
    """Human-readable language name for an OpenSubtitles code.

    Resolves against the picker catalog; unknown codes are returned as-is.
    """
    return LANGUAGE_CATALOG.get(code, code)


def language_flag(code: str) -> str:
    """Flag emoji for an OpenSubtitles code; empty string when none."""
    return LANGUAGE_FLAGS.get(code, "")


# ---------------------------------------------------------------------------
# model dataclasses
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class VideoInfo:
    """Everything we know about the video being searched for."""

    path: str
    filename: str
    title: str = ""
    series: Optional[str] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    year: Optional[int] = None
    kind: str = "movie"  # "movie" | "episode"
    hashes: tuple[str, ...] = ()


@dataclass(slots=True)
class SubtitleResult:
    """A single search hit, as shown in the results list."""

    id: str
    provider: str
    language: str  # opensubtitles code, e.g. "en" / "pt-br"
    language_name: str  # e.g. "English"
    name: str
    release_info: str
    rating: Optional[float]
    downloads: Optional[int]
    new_downloads: Optional[int] = None
    votes: Optional[int] = None
    from_trusted: bool = False
    upload_date: Optional[datetime] = None
    hearing_impaired: bool = False
    format: Optional[str] = None
    page_link: Optional[str] = None
    score: float = 0.0
    hash_match: bool = False
    raw: Any = field(default=None, repr=False)  # download payload (file_id …)

    @property
    def flag(self) -> str:
        return language_flag(self.language)


@dataclass(slots=True)
class SearchQuery:
    """A fully-resolved search request."""

    text: str = ""
    year: Optional[int] = None
    season: Optional[int] = None
    episode: Optional[int] = None
    imdb_id: Optional[str] = None
    kind: str = "auto"  # "auto" | "movie" | "episode"
    use_file: bool = False  # prefer scanning/hashing the on-disk video
    languages: tuple[str, ...] = ("en",)


@dataclass(slots=True)
class DownloadResult:
    ok: bool
    path: Optional[str] = None
    error: Optional[str] = None
    hint: Optional[str] = None
    track_id: Optional[int] = None


@dataclass(slots=True)
class CliArgs:
    """Parsed command-line arguments."""

    socket: Optional[str] = None
    file: Optional[str] = None
    query: Optional[str] = None
    debug: bool = False
    width: Optional[int] = None
    height: Optional[int] = None
