#!/usr/bin/env python3
"""Tests for the OpenSubtitles REST client download/search flow.

Covers the behaviour ported from the official VLSub extension
(``opensubtitles/vlsub-opensubtitles-com``) into
:class:`opensubtitles_client.OpenSubtitlesClient`:

* movie hash computation (``moviehash`` module)
* search params: moviehash/moviebytesize, query/season/episode, imdb_id,
  languages, order_by/order_direction
* result mapping from the API ``attributes`` payload
* empty hash search -> name search fallback
* login + bearer-token caching, 401 re-login retry, missing-credentials error
* the download pipeline: POST /download (file_id as string) -> GET link ->
  gunzip -> save with ``<video>.<lang>.<format>`` naming + encoding
* the async wrappers and their error/timeout mapping

No network: the client's ``_http_request`` is replaced with a fake that
records calls and returns canned payloads.  Runs without pytest or a display:

    python3 tests/test_download.py
"""

from __future__ import annotations

import asyncio
import gzip
import json
import os
import sys
import tempfile
import urllib.parse
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# keep the tests hermetic regardless of the caller's environment
for _k in (
    "OPENSUBTITLES_USERNAME",
    "OPENSUBTITLES_PASSWORD",
):
    os.environ.pop(_k, None)

import opensubtitles_client  # noqa: E402
from cache import SearchCache  # noqa: E402
from models import SearchQuery, SubtitleResult, VideoInfo  # noqa: E402
from moviehash import compute_movie_hash  # noqa: E402
from opensubtitles_client import OpenSubtitlesClient  # noqa: E402
from search import build_query, video_from_path  # noqa: E402
from settings import Settings  # noqa: E402

EPISODE_FILE = "The.Big.Bang.Theory.S05E18.HDTV.x264-LOL.mp4"

SUBTITLE_CONTENT = b"WEBVTT\n00:00:01.000 --> 00:00:02.000\nhi\n"


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------


def _subtitle_item(
    sub_id: int = 1,
    language: str = "en",
    release: str = "The.Big.Bang.Theory.S05E18.HDTV.x264-LOL",
    file_id: int = 111,
    file_name: str = "The.Big.Bang.Theory.S05E18.en.srt",
    download_count: int = 42,
    new_download_count: int = 5,
    votes: int = 3,
    ratings: float = 7.5,
    from_trusted: bool = False,
    hearing_impaired: bool = False,
    moviehash_match: bool = False,
    upload_date: str | None = "2024-01-01T10:00:00Z",
) -> dict:
    return {
        "id": sub_id,
        "type": "subtitle",
        "attributes": {
            "subtitle_id": sub_id,
            "language": language,
            "release": release,
            "download_count": download_count,
            "new_download_count": new_download_count,
            "votes": votes,
            "ratings": ratings,
            "from_trusted": from_trusted,
            "hearing_impaired": hearing_impaired,
            "upload_date": upload_date,
            "moviehash_match": moviehash_match,
            "url": "https://www.opensubtitles.com/en/subtitles/1",
            "files": [{"file_id": file_id, "file_name": file_name}],
            "feature_details": {"movie_name": "The Big Bang Theory", "year": 2009},
        },
    }


class FakeHttp:
    """Records calls; returns canned payloads for every API endpoint."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict, bytes | None]] = []
        self.login_data: dict = {"token": "tok-123"}
        self.login_status = 200
        self.search_items: list[dict] = []
        self.search_status = 200
        self.download_data: dict = {
            "link": "https://cdn.example.com/sub.gz",
            "file_name": "sub.srt",
            "remaining": 4,
            "requests": 1,
        }
        self.download_status = 200
        # optional queue: if set, each /download call pops the next status
        self.download_statuses: list[int] | None = None
        self.link_content: bytes = gzip.compress(SUBTITLE_CONTENT)
        self.link_status = 200
        self._unauthorized_left = 0

    def unauthorized_times(self, n: int) -> None:
        """Make the next *n* subtitles calls answer 401 (token expired)."""
        self._unauthorized_left = n

    def __call__(self, method, url, headers=None, body=None, timeout=30.0, retries=2):
        headers = dict(headers or {})
        self.calls.append((method, url, headers, body))
        parsed = urllib.parse.urlparse(url)

        if parsed.path.endswith("/login"):
            if self.login_status != 200:
                return self.login_status, b'{"message":"bad login"}'
            return 200, json.dumps(self.login_data).encode("utf-8")

        if parsed.path.endswith("/subtitles"):
            if self._unauthorized_left > 0:
                self._unauthorized_left -= 1
                return 401, b'{"message":"token expired"}'
            if self.search_status != 200:
                return self.search_status, b"{}"
            payload = {"total_count": len(self.search_items), "data": self.search_items}
            return 200, json.dumps(payload).encode("utf-8")

        if parsed.path.endswith("/download"):
            status = self.download_status
            if self.download_statuses:
                status = self.download_statuses.pop(0)
            if status != 200:
                return status, b'{"message":"download refused"}'
            return 200, json.dumps(self.download_data).encode("utf-8")

        # the CDN link that serves the (gzipped) subtitle content
        return self.link_status, self.link_content

    # -- introspection helpers ----------------------------------------------

    def calls_for(self, suffix: str, method: str = "GET") -> list[tuple[str, str, dict, bytes | None]]:
        return [
            c for c in self.calls
            if c[0] == method and urllib.parse.urlparse(c[1]).path.endswith(suffix)
        ]

    def search_query(self, index: int = 0) -> dict[str, list[str]]:
        call = self.calls_for("/subtitles")[index]
        return urllib.parse.parse_qs(urllib.parse.urlparse(call[1]).query)


def _client(tmp: str, **settings_overrides) -> tuple[OpenSubtitlesClient, FakeHttp]:
    """Client with credentials, so the login/401 paths run like in production."""
    kwargs = {
        "username": "you@example.com",
        "password": "secret",
        **settings_overrides,
    }
    client = OpenSubtitlesClient(
        Settings(download_dir=tmp, **kwargs),
        SearchCache(),
    )
    fake = FakeHttp()
    client._http_request = fake  # type: ignore[method-assign]
    return client, fake


def _episode_query() -> SearchQuery:
    return SearchQuery(
        text="The Big Bang Theory",
        season=5,
        episode=18,
        kind="episode",
        languages=("en",),
    )


def _raw_result() -> SubtitleResult:
    return SubtitleResult(
        id="1",
        provider="opensubtitles",
        language="en",
        language_name="English",
        name="The Big Bang Theory S05E18",
        release_info="HDTV",
        rating=None,
        downloads=10,
        format="srt",
        page_link=None,
        raw={"file_id": 111, "file_name": "sub.en.srt", "language": "en"},
    )


# ---------------------------------------------------------------------------
# movie hash
# ---------------------------------------------------------------------------


def test_movie_hash_small_file() -> None:
    with tempfile.NamedTemporaryFile(delete=False) as fh:
        fh.write(b"12345678")
        path = fh.name
    try:
        h, size = compute_movie_hash(path)
        assert size == 8
        # 8 + 0x3837363534333231 (LE "12345678")
        assert h == "3837363534333239"
    finally:
        Path(path).unlink(missing_ok=True)


def test_movie_hash_large_zero_file() -> None:
    with tempfile.NamedTemporaryFile(delete=False) as fh:
        fh.write(b"\x00" * (200 * 1024))  # > 128 KiB: head + tail hashed
        path = fh.name
    try:
        h, size = compute_movie_hash(path)
        assert size == 200 * 1024
        assert h == f"{size:016x}"  # all-zero chunks add nothing
    finally:
        Path(path).unlink(missing_ok=True)


# ---------------------------------------------------------------------------
# search parameters
# ---------------------------------------------------------------------------


def test_search_hash_params() -> None:
    with tempfile.NamedTemporaryFile(suffix=".mkv", delete=False) as fh:
        fh.write(b"\x00" * 1024)
        path = fh.name
    try:
        with tempfile.TemporaryDirectory() as tmp:
            client, fake = _client(tmp)
            fake.search_items = [_subtitle_item()]
            video = video_from_path(path)
            query = build_query(video, "", ["en"])
            assert query.use_file is True

            client._search_sync(query, video)

            q = fake.search_query(0)
            assert q["moviehash"] == [compute_movie_hash(path)[0]]
            assert q["moviebytesize"] == [str(os.path.getsize(path))]
            assert q["languages"] == ["en"]
            assert "query" not in q
            # only one request: the hash search returned a hit
            assert len(fake.calls_for("/subtitles")) == 1
    finally:
        Path(path).unlink(missing_ok=True)


def test_search_name_params() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp)
        fake.search_items = [_subtitle_item()]
        client._search_sync(_episode_query(), None)

        q = fake.search_query(0)
        assert q["query"] == ["The Big Bang Theory"]
        assert q["season_number"] == ["5"]
        assert q["episode_number"] == ["18"]
        assert q["type"] == ["episode"]
        assert q["languages"] == ["en"]


def test_search_imdb_params() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp)
        fake.search_items = [_subtitle_item()]
        query = SearchQuery(text="", imdb_id="tt0898266", kind="movie", languages=("en",))
        client._search_sync(query, None)

        q = fake.search_query(0)
        assert q["imdb_id"] == ["898266"]  # tt-prefix stripped, like the extension
        assert "query" not in q


def test_search_sort_params() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp, sort="downloads")
        fake.search_items = [_subtitle_item()]
        client._search_sync(_episode_query(), None)

        q = fake.search_query(0)
        assert q["order_by"] == ["download_count"]
        assert q["order_direction"] == ["desc"]


def test_search_best_match_sends_no_order() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp, sort="score")
        fake.search_items = [_subtitle_item()]
        client._search_sync(_episode_query(), None)

        q = fake.search_query(0)
        assert "order_by" not in q
        assert "order_direction" not in q


# ---------------------------------------------------------------------------
# result mapping
# ---------------------------------------------------------------------------


def test_result_mapping() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp)
        fake.search_items = [
            _subtitle_item(
                sub_id=7,
                language="pt-br",
                release="Show.1080p.WEB",
                file_name="Show.pt-br.vtt",
                from_trusted=True,
                hearing_impaired=True,
                moviehash_match=True,
            )
        ]
        results = client._search_sync(_episode_query(), None)

        assert len(results) == 1
        sub = results[0]
        assert sub.id == "7"
        assert sub.provider == "opensubtitles"
        assert sub.language == "pt-br"
        assert sub.language_name == "Portuguese (BR)"
        assert sub.name == "Show.1080p.WEB"
        assert sub.release_info == "Show.1080p.WEB"
        assert sub.downloads == 42
        assert sub.new_downloads == 5
        assert sub.votes == 3
        assert sub.rating == 7.5
        assert sub.from_trusted is True
        assert sub.hearing_impaired is True
        assert sub.format == "vtt"
        assert sub.hash_match is True
        assert sub.page_link == "https://www.opensubtitles.com/en/subtitles/1"
        assert sub.raw == {
            "file_id": 111,
            "file_name": "Show.pt-br.vtt",
            "subtitle_id": 7,
            "language": "pt-br",
        }


def test_result_limits_to_max_results() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp, max_results=2)
        fake.search_items = [_subtitle_item(sub_id=i) for i in range(5)]
        results = client._search_sync(_episode_query(), None)
        assert len(results) == 2


# ---------------------------------------------------------------------------
# fallback + auth
# ---------------------------------------------------------------------------


def test_empty_hash_search_falls_back_to_name() -> None:
    with tempfile.NamedTemporaryFile(suffix=".mkv", delete=False) as fh:
        fh.write(b"\x00" * 1024)
        path = fh.name
    try:
        with tempfile.TemporaryDirectory() as tmp:
            client, fake = _client(tmp)
            # no hash hits, one name hit
            client._search_sync(
                SearchQuery(text="Show", use_file=True, languages=("en",)),
                VideoInfo(path=path, filename="Show.mkv", title="Show"),
            )
            calls = fake.calls_for("/subtitles")
            assert len(calls) == 2
            first = urllib.parse.parse_qs(urllib.parse.urlparse(calls[0][1]).query)
            second = urllib.parse.parse_qs(urllib.parse.urlparse(calls[1][1]).query)
            assert "moviehash" in first
            assert "query" in second
    finally:
        Path(path).unlink(missing_ok=True)


def test_login_token_cached_across_searches() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp)
        fake.search_items = [_subtitle_item()]
        client._search_sync(_episode_query(), None)
        client._search_sync(
            SearchQuery(text="Other", kind="movie", languages=("fr",)), None
        )
        # one login for both searches (token cached 24 h)
        assert len(fake.calls_for("/login", "POST")) == 1
        login_headers = fake.calls_for("/login", "POST")[0][2]
        assert login_headers.get("Api-Key") == opensubtitles_client.DEFAULT_API_KEY
        assert "you@example.com" in (fake.calls_for("/login", "POST")[0][3] or b"").decode()


def test_401_triggers_relogin_and_retry() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp)
        fake.search_items = [_subtitle_item()]
        fake.unauthorized_times(1)

        results = client._search_sync(_episode_query(), None)

        assert len(results) == 1
        assert len(fake.calls_for("/subtitles")) == 2
        # re-login happened between the two attempts
        assert len(fake.calls_for("/login", "POST")) == 2


def test_missing_credentials_raises() -> None:
    """No username/password -> clear error, no anonymous mode."""
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp, username="", password="")
        try:
            client._search_sync(_episode_query(), None)
        except RuntimeError as exc:
            assert "credentials" in str(exc).lower()
            assert len(fake.calls_for("/login", "POST")) == 0
            assert len(fake.calls_for("/subtitles")) == 0
        else:
            raise AssertionError("expected RuntimeError without credentials")


# ---------------------------------------------------------------------------
# download
# ---------------------------------------------------------------------------


def test_download_flow_writes_named_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp)
        video = VideoInfo(path="/x/The.Big.Bang.Theory.S05E18.mkv",
                          filename="The.Big.Bang.Theory.S05E18.mkv", title="TBBT")
        sub = _raw_result()
        result = asyncio.run(client.download(sub, video, _episode_query()))

        assert result.ok is True
        path = Path(tmp) / "The.Big.Bang.Theory.S05E18.en.srt"
        assert result.path == str(path)
        assert path.read_bytes() == SUBTITLE_CONTENT

        # POST /download with file_id as a *string*
        posts = fake.calls_for("/download", "POST")
        assert len(posts) == 1
        body = json.loads(posts[0][3] or b"{}")
        assert body == {"file_id": "111"}
        assert posts[0][2].get("Content-Type") == "application/json"
        assert posts[0][2].get("Authorization") == "Bearer tok-123"

        # the CDN link was fetched and gunzipped
        assert any(c[0] == "GET" and "cdn.example.com" in c[1] for c in fake.calls)


def test_download_no_video_uses_query_title_for_name() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp)
        result = asyncio.run(client.download(_raw_result(), None, _episode_query()))
        assert result.ok is True
        assert Path(result.path or "").name == "The_Big_Bang_Theory.en.srt"


def test_download_overwrites_existing_file() -> None:
    """A subtitle already at the target path is replaced, not skipped."""
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp)
        target = Path(tmp) / "The_Big_Bang_Theory.en.srt"
        target.write_text("stale subtitle", encoding="utf-8")

        result = asyncio.run(client.download(_raw_result(), None, _episode_query()))

        assert result.ok is True
        assert target.read_bytes() == SUBTITLE_CONTENT


def test_download_saves_next_to_video_file() -> None:
    """With a real local video, the subtitle lands beside it, not in the
    configured download dir (so mpv auto-loads the external track)."""
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as dl:
        video_path = Path(tmp) / "Show.mkv"
        video_path.write_bytes(b"\x00" * 1024)
        client, fake = _client(dl)
        video = VideoInfo(path=str(video_path), filename="Show.mkv", title="Show")

        result = asyncio.run(client.download(_raw_result(), video, _episode_query()))

        assert result.ok is True
        assert result.path == str(Path(tmp) / "Show.en.srt")
        assert Path(result.path).exists()
        assert not (Path(dl) / "Show.en.srt").exists()


def test_download_missing_raw_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp)
        sub = _raw_result()
        sub.raw = None
        result = asyncio.run(client.download(sub, None, _episode_query()))
        assert result.ok is False
        assert "payload" in (result.error or "")


def test_download_retries_on_transient_503_then_succeeds() -> None:
    """503s from the download endpoint are retried, not failed instantly."""
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp)
        client._download_retry_backoff = (0.0, 0.0)  # keep the test fast
        fake.download_statuses = [503, 503, 200]

        result = asyncio.run(client.download(_raw_result(), None, _episode_query()))

        assert result.ok is True
        assert len(fake.calls_for("/download", "POST")) == 3


def test_download_503_after_retries_surfaces_clear_error() -> None:
    """Persistent 503 -> friendly error after all retries are exhausted."""
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp)
        client._download_retry_backoff = (0.0, 0.0)
        fake.download_statuses = [503, 503, 503]

        result = asyncio.run(client.download(_raw_result(), None, _episode_query()))

        assert result.ok is False
        assert "Service unavailable" in (result.error or "")
        assert len(fake.calls_for("/download", "POST")) == 3


def test_download_auth_failure_surfaces_hint() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp)
        fake.download_status = 401
        result = asyncio.run(client.download(_raw_result(), None, _episode_query()))
        assert result.ok is False
        assert "Authentication" in (result.error or "")
        assert result.hint  # credentials hint surfaced


def test_download_empty_content_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp)
        fake.link_content = b""
        result = asyncio.run(client.download(_raw_result(), None, _episode_query()))
        assert result.ok is False
        assert "empty" in (result.error or "")


def test_download_encoding_conversion() -> None:
    content = "café résumé\n".encode("utf-8")
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp, encoding="latin-1")
        fake.link_content = gzip.compress(content)
        result = asyncio.run(client.download(_raw_result(), None, _episode_query()))
        assert result.ok is True
        data = Path(result.path or "").read_bytes()
        assert data == content.decode("utf-8").encode("latin-1")


# ---------------------------------------------------------------------------
# async wrappers / cache
# ---------------------------------------------------------------------------


def test_search_async_uses_cache() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp)
        fake.search_items = [_subtitle_item()]

        first = asyncio.run(client.search(_episode_query(), None))
        second = asyncio.run(client.search(_episode_query(), None))

        assert first is second  # same cached list object
        assert len(fake.calls_for("/subtitles")) == 1


def test_search_async_cache_misses_on_sort_change() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp)
        fake.search_items = [_subtitle_item()]

        asyncio.run(client.search(_episode_query(), None))
        client.settings.sort = "downloads"
        asyncio.run(client.search(_episode_query(), None))

        assert len(fake.calls_for("/subtitles")) == 2


def test_search_async_timeout_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        client, fake = _client(tmp)

        import types as _types

        fake_asyncio = _types.ModuleType("asyncio")
        fake_asyncio.TimeoutError = asyncio.TimeoutError

        async def _to_thread(*args, **kwargs):
            raise asyncio.TimeoutError("simulated hang")

        async def _wait_for(aw, timeout):
            return await aw

        fake_asyncio.to_thread = _to_thread
        fake_asyncio.wait_for = _wait_for
        old = opensubtitles_client.asyncio
        opensubtitles_client.asyncio = fake_asyncio  # type: ignore[assignment]
        try:
            try:
                asyncio.run(client.search(_episode_query(), None))
            except RuntimeError as exc:
                assert "timed out" in str(exc)
            else:
                raise AssertionError("expected RuntimeError on timeout")
        finally:
            opensubtitles_client.asyncio = old


# ---------------------------------------------------------------------------
# runner
# ---------------------------------------------------------------------------


def main() -> int:
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
