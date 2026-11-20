"""Music search engine for direct song search on Spotify and SoundCloud."""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import time
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from typing import Any

from app.cache.fallback import FallbackCache
from app.cache.redis import get_redis
from app.core.logging import get_logger
from app.core.units import human_duration
from app.media.downloader import _build_ytdlp_opts, _make_ydl

log = get_logger(__name__)

_IN_MEMORY_SESSIONS = FallbackCache(1024, 3600)

_spotify_cached_token: str | None = None
_spotify_token_expires_at: float = 0.0

SPOTIFY_SEARCH_SHA256 = "eff59fa0a3d026b88b56fddbcf4bdfa16a186b8175a5c1a358c072e053c2e5b0"


@dataclass(frozen=True, slots=True)
class SearchTrack:
    id: str
    platform: str  # "spotify" or "soundcloud"
    title: str
    artist: str
    duration_str: str
    duration_seconds: int
    url: str
    thumbnail_url: str | None = None
    album: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> SearchTrack:
        return cls(
            id=d["id"],
            platform=d["platform"],
            title=d["title"],
            artist=d["artist"],
            duration_str=d["duration_str"],
            duration_seconds=int(d["duration_seconds"]),
            url=d["url"],
            thumbnail_url=d.get("thumbnail_url"),
            album=d.get("album"),
        )


def _get_spotify_token_sync() -> str:
    """Acquire an anonymous Spotify Web Player bearer token from public embed payload."""
    global _spotify_cached_token, _spotify_token_expires_at

    now = time.time()
    if _spotify_cached_token and now < _spotify_token_expires_at:
        return _spotify_cached_token

    embed_urls = [
        "https://open.spotify.com/embed/track/4cOdK2wGLETKBW3PvgPWqT",
        "https://open.spotify.com/embed/track/7lQ8MOhq6IN2w8EYcFNSUk",
    ]

    for url in embed_urls:
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
                    )
                },
            )
            with urllib.request.urlopen(req, timeout=8) as resp:  # nosec B310
                html = resp.read().decode("utf-8", errors="replace")
                m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
                if not m:
                    continue
                data = json.loads(m.group(1))
                session_info = (
                    data.get("props", {})
                    .get("pageProps", {})
                    .get("state", {})
                    .get("settings", {})
                    .get("session", {})
                )
                token = session_info.get("accessToken")
                exp_ms = session_info.get("accessTokenExpirationTimestampMs")
                if token:
                    _spotify_cached_token = token
                    # Default expiration 50 minutes if not present
                    _spotify_token_expires_at = (exp_ms / 1000.0) if exp_ms else (now + 3000)
                    return token
        except Exception as exc:
            log.warning("spotify_token_fetch_failed", url=url, error=str(exc))

    if _spotify_cached_token:
        return _spotify_cached_token

    raise RuntimeError("could not bootstrap anonymous spotify token")


def search_spotify_sync(query: str, limit: int = 5) -> list[SearchTrack]:
    """Search tracks directly on Spotify via GraphQL pathfinder query."""
    clean_query = query.strip()
    if not clean_query:
        return []

    token = _get_spotify_token_sync()
    variables = {
        "searchTerm": clean_query,
        "offset": 0,
        "limit": limit,
        "numberOfTopResults": limit,
        "includeAudiobooks": False,
        "includePreReleases": False,
        "includeAlbumPreReleases": False,
        "includeAuthors": False,
        "includeEpisodeContentRatingsV2": False,
    }
    extensions = {"persistedQuery": {"version": 1, "sha256Hash": SPOTIFY_SEARCH_SHA256}}
    params = urllib.parse.urlencode(
        {
            "operationName": "searchDesktop",
            "variables": json.dumps(variables),
            "extensions": json.dumps(extensions),
        }
    )
    url = f"https://api-partner.spotify.com/pathfinder/v1/query?{params}"

    req = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "app-platform": "WebPlayer",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception as exc:
        log.warning("spotify_search_request_failed", query=clean_query, error=str(exc))
        return []

    items = data.get("data", {}).get("searchV2", {}).get("tracksV2", {}).get("items", [])

    results: list[SearchTrack] = []
    for item in items[:limit]:
        d = item.get("item", {}).get("data", {})
        uri = d.get("uri") or ""
        track_id = uri.split(":")[-1] if uri else ""
        if not track_id:
            continue

        name = (d.get("name") or "Unknown Title").strip()
        artists_list = [
            a.get("profile", {}).get("name", "").strip()
            for a in d.get("artists", {}).get("items", [])
            if a.get("profile", {}).get("name")
        ]
        artist_name = ", ".join(artists_list) or "Spotify"
        album_name = d.get("albumOfTrack", {}).get("name")
        cover_sources = d.get("albumOfTrack", {}).get("coverArt", {}).get("sources", [])
        cover_url = cover_sources[0].get("url") if cover_sources else None

        dur_ms = d.get("duration", {}).get("totalMilliseconds", 0)
        dur_sec = max(0, int(dur_ms // 1000))
        dur_str = human_duration(float(dur_sec)) if dur_sec else "—"

        spotify_url = f"https://open.spotify.com/track/{track_id}"
        results.append(
            SearchTrack(
                id=track_id,
                platform="spotify",
                title=name,
                artist=artist_name,
                duration_str=dur_str,
                duration_seconds=dur_sec,
                url=spotify_url,
                thumbnail_url=cover_url,
                album=album_name,
            )
        )

    return results


def search_soundcloud_sync(query: str, limit: int = 5) -> list[SearchTrack]:
    """Search tracks on SoundCloud using yt-dlp scsearch extractor."""
    clean_query = query.strip()
    if not clean_query:
        return []

    opts = _build_ytdlp_opts(download=False, platform_key="soundcloud")
    opts["extract_flat"] = "in_playlist"
    opts["ignoreerrors"] = True

    try:
        with _make_ydl(opts) as ydl:
            res = ydl.extract_info(f"scsearch{limit}:{clean_query}", download=False)
    except Exception as exc:
        log.warning("soundcloud_search_failed", query=clean_query, error=str(exc))
        return []

    entries = (res or {}).get("entries") or []
    results: list[SearchTrack] = []

    for _idx, e in enumerate(entries[:limit], 1):
        if not e or not isinstance(e, dict):
            continue
        title = (e.get("title") or "Unknown").strip()
        uploader = (e.get("uploader") or e.get("artist") or "SoundCloud").strip()
        track_url = e.get("url") or e.get("webpage_url") or ""
        if not track_url:
            continue

        raw_dur = e.get("duration")
        dur_sec = int(raw_dur) if raw_dur and isinstance(raw_dur, (int, float)) else 0
        dur_str = human_duration(float(dur_sec)) if dur_sec else "—"
        thumb = e.get("thumbnail")

        track_id = f"sc_{secrets.token_hex(4)}"
        results.append(
            SearchTrack(
                id=track_id,
                platform="soundcloud",
                title=title,
                artist=uploader,
                duration_str=dur_str,
                duration_seconds=dur_sec,
                url=track_url,
                thumbnail_url=thumb,
                album="SoundCloud",
            )
        )

    return results


async def search_spotify(query: str, limit: int = 5) -> list[SearchTrack]:
    """Async wrapper for Spotify search."""
    return await asyncio.to_thread(search_spotify_sync, query, limit)


async def search_soundcloud(query: str, limit: int = 5) -> list[SearchTrack]:
    """Async wrapper for SoundCloud search."""
    return await asyncio.to_thread(search_soundcloud_sync, query, limit)


def create_search_session_id() -> str:
    """Generate collision-resistant 8-char hex session token."""
    return secrets.token_hex(4)


async def store_search_session(sid: str, payload: dict[str, Any]) -> None:
    """Store music search session state with 1-hour TTL."""
    key = f"mewvia:music_search:{sid}"
    try:
        redis = get_redis()
        await redis.set(key, json.dumps(payload), ex=3600)
        _IN_MEMORY_SESSIONS.discard(sid)
    except Exception as exc:
        log.warning("music_search_session_redis_failed", error=str(exc))
        _IN_MEMORY_SESSIONS.put(sid, payload)


async def get_search_session(sid: str) -> dict[str, Any] | None:
    """Retrieve music search session state by token."""
    key = f"mewvia:music_search:{sid}"
    try:
        redis = get_redis()
        raw = await redis.get(key)
        if raw:
            return json.loads(raw)
    except Exception as exc:
        log.warning("music_search_session_redis_get_failed", error=str(exc))

    return _IN_MEMORY_SESSIONS.get(sid)
