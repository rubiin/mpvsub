"""Pure search logic (no GTK): guessit metadata, query building, and the
scoring / sorting applied to results before they reach the UI.
"""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from models import SearchQuery, SubtitleResult, VideoInfo

#: modes this module can sort client-side (others arrive server-ordered)
_CLIENT_SORT_MODES = {"score", "rating", "downloads", "votes", "trusted", "newest"}


# ---------------------------------------------------------------------------
# metadata detection
# ---------------------------------------------------------------------------


def guessit_metadata(name: str) -> dict:
    """Run guessit on a filename/query; never raises."""
    try:
        from guessit import guessit

        return dict(guessit(name)) or {}
    except Exception:  # noqa: BLE001 - guessit is best-effort
        return {}


def video_from_path(path: str) -> VideoInfo:
    """Build a :class:`VideoInfo` from a local file path using guessit."""
    filename = os.path.basename(path)
    g = guessit_metadata(filename)
    kind = str(g.get("type") or "")
    series = g.get("series") or (g.get("title") if kind == "episode" else None)
    season = g.get("season")
    episode = g.get("episode")
    if isinstance(episode, list):
        episode = episode[0] if episode else None
    try:
        season = int(season) if season is not None else None
        episode = int(episode) if episode is not None else None
    except (TypeError, ValueError):
        season, episode = None, None
    year = g.get("year")
    try:
        year = int(year) if year is not None else None
    except (TypeError, ValueError):
        year = None

    is_episode = kind == "episode" or bool(series and season is not None)
    title = g.get("title") or series or Path(filename).stem
    return VideoInfo(
        path=path,
        filename=filename,
        title=str(title),
        series=str(series) if series else None,
        season=season,
        episode=episode,
        year=year,
        kind="episode" if is_episode else "movie",
    )


# ---------------------------------------------------------------------------
# query building
# ---------------------------------------------------------------------------


def build_query(
    video: Optional[VideoInfo],
    text: str,
    languages: list[str],
) -> SearchQuery:
    """Build the query for the next search.

    Typed *text* wins; otherwise a local file gets hash-searched and remote
    media falls back to a name search.
    """
    query = SearchQuery(languages=tuple(languages) or ("en",))
    if text.strip():
        g = guessit_metadata(text)
        query.text = text.strip()
        season = g.get("season")
        episode = g.get("episode")
        if isinstance(episode, list):
            episode = episode[0] if episode else None
        if season is not None and episode is not None:
            query.kind = "episode"
            query.season = int(season)
            query.episode = int(episode)
        else:
            query.kind = "movie"
        if g.get("year"):
            query.year = int(g["year"])
        return query

    if video is None:
        return query  # empty → caller shows "enter a search term"

    # auto mode
    if video.path and os.path.isfile(video.path):
        query.use_file = True
        query.text = video.title
    elif video.kind == "episode" and video.series is not None:
        query.kind = "episode"
        query.text = video.series
        query.season = video.season
        query.episode = video.episode
        query.year = video.year
    else:
        query.kind = "movie"
        query.text = video.title
        query.year = video.year
    return query


# ---------------------------------------------------------------------------
# scoring / sorting / filtering
# ---------------------------------------------------------------------------


def score_subtitle(
    sub: SubtitleResult, video: Optional[VideoInfo], query: SearchQuery
) -> float:
    """Best-match heuristic used for the default sort."""
    score = 0.0
    if sub.hash_match:
        score += 4.0
    rel = (sub.release_info or "").lower()
    if video is not None:
        if video.year and str(video.year) in rel:
            score += 1.0
        filename = (video.filename or "").lower()
        for res in ("2160p", "1080p", "720p", "576p", "480p"):
            if res in filename and res in rel:
                score += 0.8
                break
        if query.kind == "episode" and video.series:
            if sub.name and query.text.lower() in sub.name.lower():
                score += 0.5
    if sub.hearing_impaired:
        score -= 0.3
    return score


def sort_results(
    items: list[SubtitleResult],
    mode: str,
    video: Optional[VideoInfo],
    query: SearchQuery,
) -> list[SubtitleResult]:
    """Return *items* sorted by *mode*. Modes the server already ordered
    (``new_downloads``, ``hd``, ``release``) pass through unchanged.
    """
    for sub in items:
        if mode == "score":
            sub.score = score_subtitle(sub, video, query)

    if mode == "rating":
        return sorted(
            items,
            key=lambda s: (s.rating is not None, s.rating if s.rating is not None else -1.0),
            reverse=True,
        )
    if mode == "downloads":
        return sorted(
            items,
            key=lambda s: (s.downloads is not None, s.downloads if s.downloads is not None else -1),
            reverse=True,
        )
    if mode == "votes":
        return sorted(
            items,
            key=lambda s: (s.votes is not None, s.votes if s.votes is not None else -1),
            reverse=True,
        )
    if mode == "trusted":
        return sorted(items, key=lambda s: s.from_trusted, reverse=True)
    if mode == "newest":
        return sorted(
            items,
            key=lambda s: (
                s.upload_date is not None,
                s.upload_date or datetime.min,
            ),
            reverse=True,
        )
    # modes the server ordered via order_by (new_downloads/hd/release) are
    # already sorted — keep them as-is instead of re-sorting by score
    if mode not in _CLIENT_SORT_MODES:
        return items
    # default: best match
    return sorted(items, key=lambda s: s.score, reverse=True)


def build_query_fields(
    title: str,
    season: Optional[int],
    episode: Optional[int],
    languages: list[str],
    imdb_id: Optional[str] = None,
) -> SearchQuery:
    """Manual search query built from the form's Title/Season/Episode fields."""
    query = SearchQuery(
        text=title.strip(),
        season=season,
        episode=episode,
        imdb_id=imdb_id,
        languages=tuple(languages) or ("en",),
    )
    query.kind = "episode" if (season is not None and episode is not None) else "movie"
    return query


def cache_key(query: SearchQuery, video: Optional[VideoInfo]) -> str:
    parts = [
        "file" if query.use_file else query.kind,
        query.text,
        str(query.season),
        str(query.episode),
        str(query.year),
        query.imdb_id or "",
        ",".join(query.languages),
        video.path if video else "",
    ]
    return "|".join(parts)
