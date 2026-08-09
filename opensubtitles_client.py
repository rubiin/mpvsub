"""OpenSubtitles.com REST API v1 client (no subliminal, stdlib only).

Replaces the subliminal-based backend with a direct client for the modern
OpenSubtitles REST API (``https://api.opensubtitles.com/api/v1``), ported
from the official VLSub extension for VLC
(``opensubtitles/vlsub-opensubtitles-com``) minus its i18n and VLC-UI code:

* ``POST /login``        -> bearer token (registered account only — the API
                            rejects credential-less logins), cached for 24 h;
                            ``401`` responses trigger a re-login + retry
* ``GET  /subtitles``    -> search by ``moviehash``+``moviebytesize`` or by
                            ``query`` / ``imdb_id`` / ``season_number`` /
                            ``episode_number``, with ``languages``,
                            ``order_by`` and ``order_direction``
* ``POST /download``     -> ``{file_id}`` -> ``{link, remaining, …}``; the
                            link serves gzipped subtitle content
* empty hash searches fall back to a name search, like the extension

Only the stdlib (``urllib``) is used; all network work runs inside
``asyncio.to_thread`` so the GTK main loop never blocks.  Credentials are
read from ``OPENSUBTITLES_API_KEY`` / ``OPENSUBTITLES_USERNAME`` /
``OPENSUBTITLES_PASSWORD`` (or the app settings); the default API key is the
one shipped in the reference VLSub extension.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from cache import SearchCache
from models import (
    DownloadResult,
    SearchQuery,
    SubtitleResult,
    VideoInfo,
    language_name,
)
from moviehash import compute_movie_hash
from search import cache_key
from settings import SORT_MODES

log = logging.getLogger(__name__)

#: default Api-Key — the one shipped in the official VLSub extension config
DEFAULT_API_KEY = "d3Sba6j6VYnty3ir5T8GXYoAuiLSBf0S"
API_BASE = "https://api.opensubtitles.com/api/v1"
USER_AGENT = "mpvui-subtitles/1.0"

#: how long a login token is considered valid (the extension uses 24 h)
TOKEN_TTL_SECONDS = 24 * 60 * 60

#: transient statuses worth retrying — server overloaded / rate limited
_TRANSIENT_STATUSES = {503, 429}

#: seconds to sleep between download retries (attempt 1, attempt 2)
_DOWNLOAD_RETRY_BACKOFF = (2.0, 4.0)

#: settings sort key -> API ``order_by`` field
_SORT_BY_FIELD = {key: api for key, _label, api in SORT_MODES}

CREDENTIALS_HINT = (
    "OpenSubtitles.com needs a free account + API key. Set "
    "OPENSUBTITLES_API_KEY, OPENSUBTITLES_USERNAME and OPENSUBTITLES_PASSWORD "
    "(or the api_key/username/password fields in "
    "~/.config/mpvui-subtitles/settings.json). See the README."
)


def _clean_imdb_id(value: str) -> Optional[str]:
    """Normalize an IMDB id (``tt1234567`` / ``1234567``) to digits."""
    digits = re.sub(r"[^0-9]", "", value or "")
    return digits.lstrip("0") or None


class OpenSubtitlesClient:
    """Thread-safe search/download facade driven from the asyncio loop."""

    def __init__(self, settings, cache: SearchCache) -> None:
        self.settings = settings
        self.cache = cache
        self._token: Optional[str] = None
        self._token_expires: float = 0.0
        self._token_lock = threading.Lock()
        self._download_retry_backoff = _DOWNLOAD_RETRY_BACKOFF

    # -- configuration ------------------------------------------------------

    def _api_key(self) -> str:
        return (
            os.environ.get("OPENSUBTITLES_API_KEY")
            or self.settings.api_key
            or DEFAULT_API_KEY
        )

    def _credentials(self) -> tuple[str, str]:
        username = os.environ.get("OPENSUBTITLES_USERNAME") or self.settings.username
        password = os.environ.get("OPENSUBTITLES_PASSWORD") or self.settings.password
        return username, password

    def _configured_token(self) -> Optional[str]:
        """Pre-issued session token (env var wins over settings)."""
        return os.environ.get("OPENSUBTITLES_TOKEN") or self.settings.token or None

    # -- HTTP layer ---------------------------------------------------------

    def _http_request(
        self,
        method: str,
        url: str,
        headers: Optional[dict] = None,
        body: Optional[bytes] = None,
        timeout: float = 30.0,
        retries: int = 2,
    ) -> tuple[int, bytes]:
        """Perform one request; returns ``(status, body_bytes)``.

        Network failures are retried (like the extension's retry setting);
        HTTP error statuses are returned to the caller, never raised.
        """
        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            request = urllib.request.Request(
                url, data=body, headers=headers or {}, method=method
            )
            try:
                with urllib.request.urlopen(request, timeout=timeout) as resp:
                    return resp.status, resp.read()
            except urllib.error.HTTPError as exc:
                return exc.code, exc.read()
            except (urllib.error.URLError, OSError) as exc:
                last_exc = exc
                if attempt < retries:
                    log.debug("request failed, retrying (%s): %s", attempt + 1, exc)
                    continue
        raise RuntimeError(f"Network error talking to OpenSubtitles: {last_exc}")

    def _request_json(
        self,
        method: str,
        url: str,
        headers: Optional[dict] = None,
        body: Optional[bytes] = None,
        timeout: float = 30.0,
        retries: int = 2,
    ) -> tuple[int, Optional[dict], bytes]:
        status, data = self._http_request(
            method, url, headers=headers, body=body, timeout=timeout, retries=retries
        )
        parsed: Optional[dict] = None
        if data:
            try:
                parsed = json.loads(data.decode("utf-8", errors="replace"))
            except ValueError:
                parsed = None
        return status, parsed, data

    # -- session ------------------------------------------------------------

    def _ensure_token(self) -> Optional[str]:
        """Return a valid bearer token, logging in when needed/expired.

        A pre-issued token (``OPENSUBTITLES_TOKEN`` / settings ``token``) is
        used as-is with no login round-trip.  Otherwise, without credentials
        the client stays token-less (searching works with just the Api-Key);
        with credentials a failed login raises so the user sees why.
        """
        preset = self._configured_token()
        if preset:
            if self._token != preset:
                self._token = preset
                self._token_expires = time.time() + TOKEN_TTL_SECONDS
            return self._token
        username, password = self._credentials()
        if not username or not password:
            # anonymous access: the API rejects credential-less logins, so go
            # tokenless — searching works with just the Api-Key
            return None
        if self._token and self._token_expires > time.time():
            return self._token
        with self._token_lock:
            # re-check inside the lock: another thread may have logged in
            if self._token and self._token_expires > time.time():
                return self._token
            body = json.dumps({"username": username, "password": password}).encode("utf-8")
            headers = {
                "Api-Key": self._api_key(),
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            }
            status, data, _raw = self._request_json(
                "POST",
                f"{API_BASE}/login",
                headers=headers,
                body=body,
                timeout=15.0,
            )
            if status == 200 and data and data.get("token"):
                self._token = data["token"]
                expires = data.get("expires")
                self._token_expires = (
                    float(expires) if isinstance(expires, (int, float)) else time.time() + TOKEN_TTL_SECONDS
                )
                return self._token

            message = (data or {}).get("message", "") if isinstance(data, dict) else ""
            self._token, self._token_expires = None, 0.0
            detail = f": {message}" if message else f" (HTTP {status})"
            raise RuntimeError(
                f"Login failed{detail} — check your OpenSubtitles credentials."
            )

    def _auth_headers(self, extra: Optional[dict] = None) -> dict:
        headers = {"Api-Key": self._api_key(), "User-Agent": USER_AGENT}
        token = self._ensure_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        if extra:
            headers.update(extra)
        return headers

    # -- search -------------------------------------------------------------

    def _sort_params(self) -> dict:
        api_field = _SORT_BY_FIELD.get(self.settings.sort)
        if not api_field:
            return {}
        direction = "asc" if self.settings.sort_direction == "asc" else "desc"
        return {"order_by": api_field, "order_direction": direction}

    def _language_params(self, query: SearchQuery) -> dict:
        return {"languages": ",".join(c.lower() for c in query.languages)}

    def _name_params(self, query: SearchQuery) -> dict:
        params: dict[str, str] = {}
        imdb = _clean_imdb_id(query.imdb_id or "")
        if imdb:
            params["imdb_id"] = imdb
            return params  # IMDB id is the primary filter, like the extension
        if query.text:
            params["query"] = query.text
        if query.year:
            params["year"] = str(query.year)
        if query.kind == "episode" and query.season is not None and query.episode is not None:
            params["season_number"] = str(query.season)
            params["episode_number"] = str(query.episode)
            params["type"] = "episode"
        elif query.kind == "movie":
            params["type"] = "movie"
        return params

    def _search_params(self, query: SearchQuery, video: Optional[VideoInfo]) -> dict:
        params: dict[str, str] = {}
        params.update(self._language_params(query))
        params.update(self._sort_params())
        path = video.path if video else None
        if query.use_file and path and os.path.isfile(path):
            moviehash, size = compute_movie_hash(path)
            params["moviehash"] = moviehash
            params["moviebytesize"] = str(size)
        else:
            params.update(self._name_params(query))
        return params

    def _auth_request_json(
        self,
        method: str,
        url: str,
        body: Optional[bytes] = None,
        timeout: float = 30.0,
    ) -> tuple[int, Optional[dict], bytes]:
        """JSON request with auth headers; on 401 re-login and retry once."""
        extra = {"Content-Type": "application/json"} if body is not None else None
        headers = self._auth_headers(extra)
        status, data, raw = self._request_json(
            method, url, headers=headers, body=body, timeout=timeout
        )
        if status == 401:
            # token expired: clear it, re-login and retry once
            self._token, self._token_expires = None, 0.0
            headers = self._auth_headers(extra)
            status, data, raw = self._request_json(
                method, url, headers=headers, body=body, timeout=timeout
            )
        return status, data, raw

    def _fetch_subtitles(self, params: dict) -> list[SubtitleResult]:
        url = f"{API_BASE}/subtitles?" + urllib.parse.urlencode(params)
        status, data, _raw = self._auth_request_json(
            "GET", url, timeout=self.settings.timeout
        )
        if status != 200:
            raise RuntimeError(self._error_message(status, data, "Search"))
        items = (data or {}).get("data") or []
        results = [
            self._to_result(item)
            for item in items
            if isinstance(item, dict) and item.get("type") == "subtitle"
        ]
        log.info("search returned %d subtitles (params: %s)", len(results), params)
        for r in results:
            file_id = r.raw.get("file_id") if isinstance(r.raw, dict) else None
            log.info(
                "  title=%s language=%s downloads=%s id=%s file_id=%s",
                (r.name or "")[:90],
                r.language,
                r.downloads,
                r.id,
                file_id,
            )
        return results

    def _search_sync(self, query: SearchQuery, video: Optional[VideoInfo]) -> list[SubtitleResult]:
        if not query.languages:
            raise ValueError("No languages selected — pick at least one language.")
        params = self._search_params(query, video)
        results = self._fetch_subtitles(params)
        if not results and "moviehash" in params and (query.text or query.imdb_id):
            # empty hash search -> name search fallback, like the extension
            log.info("hash search empty; falling back to name search")
            name_params = dict(self._language_params(query))
            name_params.update(self._sort_params())
            name_params.update(self._name_params(query))
            results = self._fetch_subtitles(name_params)
        return results[: self.settings.max_results]

    @staticmethod
    def _subtitle_format(file_name: str) -> str:
        ext = Path(file_name or "").suffix.lower().lstrip(".")
        return ext if ext in {"srt", "vtt", "ass", "ssa", "sub", "txt"} else "srt"

    @staticmethod
    def _parse_upload_date(value: Any) -> Optional[datetime]:
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None

    def _to_result(self, item: dict) -> SubtitleResult:
        attr = item.get("attributes") or {}
        files = attr.get("files") or []
        first = files[0] if isinstance(files, list) and files else {}
        file_name = str(first.get("file_name") or "") if isinstance(first, dict) else ""
        lang = str(attr.get("language") or "en")
        raw = {
            "file_id": first.get("file_id") if isinstance(first, dict) else None,
            "file_name": file_name,
            "subtitle_id": attr.get("subtitle_id"),
            "language": lang,
        }
        return SubtitleResult(
            id=str(attr.get("subtitle_id") or item.get("id") or ""),
            provider="opensubtitles",
            language=lang,
            language_name=language_name(lang),
            name=str(attr.get("release") or file_name or "Unknown")[:200],
            release_info=str(attr.get("release") or "")[:120],
            rating=self._as_float(attr.get("ratings")),
            downloads=self._as_int(attr.get("download_count")),
            new_downloads=self._as_int(attr.get("new_download_count")),
            votes=self._as_int(attr.get("votes")),
            from_trusted=bool(attr.get("from_trusted")),
            upload_date=self._parse_upload_date(attr.get("upload_date")),
            hearing_impaired=bool(attr.get("hearing_impaired")),
            format=self._subtitle_format(file_name),
            page_link=attr.get("url"),
            hash_match=bool(attr.get("moviehash_match")),
            raw=raw,
        )

    @staticmethod
    def _as_float(value: Any) -> Optional[float]:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _as_int(value: Any) -> Optional[int]:
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def _error_message(self, status: int, data: Optional[dict], action: str) -> str:
        message = data.get("message") if isinstance(data, dict) else None
        if status == 401:
            return (
                "Authentication failed — check your OpenSubtitles credentials "
                "and API key."
            )
        if status == 403:
            return "Access forbidden (403) — the API key may be invalid."
        if status == 404:
            return "Not found (404) — the resource may have been removed."
        if status == 429:
            return (
                "Too many requests (429) — OpenSubtitles rate-limits its API. "
                "Wait a minute and try again."
            )
        if status == 503:
            return (
                "Service unavailable (503) — OpenSubtitles' download service "
                "is down right now. Try again in a few minutes."
            )
        if message:
            return f"{action} failed (HTTP {status}): {message}"
        return f"{action} failed (HTTP {status})"

    # -- download -----------------------------------------------------------

    def _post_download_with_retry(
        self, body: bytes
    ) -> tuple[int, Optional[dict], bytes]:
        """POST /download, retrying transient errors (503/429) with backoff.

        OpenSubtitles' download service periodically answers 503 from its
        load balancer (it is not an auth problem).  Retry a couple of times
        with a short backoff before surfacing the error — the same spirit
        as the reference extension's retry setting.
        """
        status, data, raw = 0, None, b""
        attempts = len(self._download_retry_backoff) + 1
        for attempt in range(attempts):
            status, data, raw = self._auth_request_json(
                "POST",
                f"{API_BASE}/download",
                body=body,
                timeout=self.settings.timeout,
            )
            if status not in _TRANSIENT_STATUSES:
                return status, data, raw
            if attempt < attempts - 1:
                wait = self._download_retry_backoff[attempt]
                log.warning(
                    "download endpoint transient error %s, retrying in %.0fs "
                    "(attempt %d/%d)",
                    status,
                    wait,
                    attempt + 1,
                    attempts,
                )
                time.sleep(wait)
        return status, data, raw

    def _output_path(
        self, query: SearchQuery, video: Optional[VideoInfo], raw: dict
    ) -> Path:
        if video and video.filename:
            stem = Path(video.filename).stem
        elif query.text:
            # typed title: sanitize for the filesystem (spaces -> _), like
            # the reference extension
            stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", query.text)
            stem = re.sub(r"\s+", "_", stem).strip("_") or "subtitle"
        else:
            stem = "subtitle"
        lang = raw.get("language") or "en"
        fmt = self._subtitle_format(raw.get("file_name") or "")
        return Path(self.settings.download_dir) / f"{stem}.{lang}.{fmt}"

    @staticmethod
    def _gunzip(data: bytes) -> bytes:
        try:
            return gzip.decompress(data)
        except (gzip.BadGzipFile, EOFError, OSError):
            return data  # the link already served plain content

    def _download_sync(
        self,
        sub: SubtitleResult,
        video: Optional[VideoInfo],
        query: SearchQuery,
    ) -> str:
        raw = sub.raw
        if not isinstance(raw, dict) or not raw.get("file_id"):
            raise RuntimeError("This subtitle has no file payload to download.")

        log.info(
            "downloading subtitle file_id=%s [%s] — %s",
            raw.get("file_id"),
            raw.get("language"),
            (raw.get("file_name") or sub.name or "")[:90],
        )

        # POST /download — file_id as a *string* (avoids scientific notation
        # for large ids, like the reference implementation); transient server
        # errors (503/429) are retried with backoff
        body = json.dumps({"file_id": str(raw["file_id"])}).encode("utf-8")
        status, data, _raw = self._post_download_with_retry(body)
        if status != 200 or not isinstance(data, dict) or not data.get("link"):
            raise RuntimeError(self._error_message(status, data, "Download"))

        remaining = data.get("remaining")
        log.info(
            "download ok — daily quota remaining: %s",
            remaining if remaining is not None else "unknown",
        )

        link = data["link"]
        status, content = self._http_request(
            "GET", link, headers={"User-Agent": USER_AGENT}, timeout=60.0, retries=3
        )
        log.info("subtitle content fetch: HTTP %s, %d bytes", status, len(content or b""))
        if status != 200 or not content:
            raise RuntimeError(
                "Downloaded an empty result — the file may have been removed."
            )

        payload = self._gunzip(content)
        encoding = self.settings.encoding or "utf-8"
        if encoding.lower() != "utf-8":
            payload = payload.decode("utf-8", errors="replace").encode(
                encoding, errors="replace"
            )

        directory = Path(self.settings.download_dir)
        directory.mkdir(parents=True, exist_ok=True)
        path = self._output_path(query, video, raw)
        path.write_bytes(payload)
        log.info("saved subtitle to %s", path)
        return str(path)

    # -- async API ----------------------------------------------------------

    async def search(
        self, query: SearchQuery, video: Optional[VideoInfo]
    ) -> list[SubtitleResult]:
        key = f"{cache_key(query, video)}|{self.settings.sort}:{self.settings.sort_direction}"
        cached = self.cache.get(key)
        if cached is not None:
            log.debug("cache hit for %s", key)
            return cached
        try:
            results = await asyncio.wait_for(
                asyncio.to_thread(self._search_sync, query, video),
                timeout=180.0,
            )
        except asyncio.TimeoutError as exc:
            raise RuntimeError("Search timed out — OpenSubtitles is slow right now.") from exc
        self.cache.set(key, results)
        return results

    async def download(
        self,
        sub: SubtitleResult,
        video: Optional[VideoInfo],
        query: SearchQuery,
    ) -> DownloadResult:
        try:
            path = await asyncio.wait_for(
                asyncio.to_thread(self._download_sync, sub, video, query),
                timeout=120.0,
            )
            return DownloadResult(ok=True, path=path)
        except asyncio.TimeoutError:
            log.warning("download timed out")
            return DownloadResult(ok=False, error="Download timed out.")
        except RuntimeError as exc:
            message = str(exc)
            hint = (
                CREDENTIALS_HINT
                if "Authentication" in message or "Login failed" in message
                else None
            )
            log.warning("download failed: %s", message)
            return DownloadResult(ok=False, error=message, hint=hint)
        except Exception as exc:  # noqa: BLE001
            log.exception("download failed")
            return DownloadResult(ok=False, error=str(exc))
