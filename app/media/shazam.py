"""Shazam music identification service and session storage."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiogram import html
from shazamio import Shazam

from app.cache.fallback import FallbackCache
from app.cache.redis import get_redis
from app.callbacks.shazam import ShazamCB
from app.core.errors import ConversionError
from app.core.logging import get_logger
from app.media.ffmpeg import run_ffmpeg
from app.media.probe import probe
from app.ui.emoji import EmojiRegistry
from app.ui.kb import KB, inline_button
from app.ui.styles import Style

log = get_logger(__name__)

_IN_MEMORY_SHAZAM = FallbackCache(2048, 3600)


@dataclass(frozen=True, slots=True)
class ShazamTrack:
    """Structured result returned by Shazam identification."""

    title: str
    artist: str
    album: str | None = None
    genres: tuple[str, ...] = ()
    release_year: str | None = None
    cover_url: str | None = None
    shazam_url: str | None = None
    apple_music_url: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "artist": self.artist,
            "album": self.album,
            "genres": list(self.genres),
            "release_year": self.release_year,
            "cover_url": self.cover_url,
            "shazam_url": self.shazam_url,
            "apple_music_url": self.apple_music_url,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ShazamTrack:
        return cls(
            title=str(data.get("title") or "Unknown"),
            artist=str(data.get("artist") or "Unknown"),
            album=data.get("album"),
            genres=tuple(data.get("genres") or ()),
            release_year=data.get("release_year"),
            cover_url=data.get("cover_url"),
            shazam_url=data.get("shazam_url"),
            apple_music_url=data.get("apple_music_url"),
        )


def create_shazam_session_id() -> str:
    return uuid.uuid4().hex[:8]


async def store_shazam_session(sid: str, track: ShazamTrack) -> None:
    key = f"mewvia:shazam:{sid}"
    payload = track.to_dict()
    try:
        redis = get_redis()
        await redis.set(key, json.dumps(payload), ex=3600)
        _IN_MEMORY_SHAZAM.discard(sid)
    except Exception as exc:
        log.warning("shazam_session_redis_store_failed", error=str(exc))
        _IN_MEMORY_SHAZAM.put(sid, payload)


async def get_shazam_session(sid: str) -> ShazamTrack | None:
    key = f"mewvia:shazam:{sid}"
    try:
        redis = get_redis()
        raw = await redis.get(key)
        if raw:
            return ShazamTrack.from_dict(json.loads(raw))
    except Exception as exc:
        log.warning("shazam_session_redis_get_failed", error=str(exc))

    data = _IN_MEMORY_SHAZAM.get(sid)
    return ShazamTrack.from_dict(data) if data else None


async def prepare_shazam_audio(src_file: Path, out_sample: Path, max_duration: int = 45) -> Path:
    """Extract a clean 128k MP3 sample for Shazam fingerprinting."""
    args = [
        "-i",
        str(src_file),
        "-vn",
        "-t",
        str(max_duration),
        "-c:a",
        "libmp3lame",
        "-b:a",
        "128k",
        str(out_sample),
    ]
    await run_ffmpeg(args)
    if not out_sample.is_file():  # noqa: ASYNC240
        raise ConversionError("استخراج صوت جهت شناسایی ناموفق بود.")
    return out_sample


async def identify_music_from_file(input_file: Path, work_dir: Path) -> ShazamTrack | None:
    """Extract audio from input file (video/audio/voice) and identify track via Shazam."""
    info = await probe(input_file)
    if not info.has_audio:
        raise ConversionError("این فایل فاقد هرگونه استریم صدا (Audio Track) است.")

    sample_mp3 = work_dir / "shazam_sample.mp3"
    await prepare_shazam_audio(input_file, sample_mp3)

    shazam = Shazam()
    res = await shazam.recognize(str(sample_mp3))
    track_data = res.get("track")
    if not track_data or not isinstance(track_data, dict):
        return None

    title = str(track_data.get("title") or "").strip()
    artist = str(track_data.get("subtitle") or "").strip()
    if not title:
        return None

    album = None
    release_year = None
    for sec in track_data.get("sections", []):
        if sec.get("type") == "SONG":
            for meta in sec.get("metadata", []):
                m_title = (meta.get("title") or "").lower()
                if m_title == "album":
                    album = meta.get("text")
                elif m_title in ("released", "year"):
                    release_year = meta.get("text")

    genres: list[str] = []
    g_primary = track_data.get("genres", {}).get("primary")
    if g_primary:
        genres.append(str(g_primary))

    images = track_data.get("images", {})
    cover_url = images.get("coverarthq") or images.get("coverart")

    shazam_url = track_data.get("url") or track_data.get("share", {}).get("href")

    apple_music_url = None
    for action in track_data.get("hub", {}).get("actions", []):
        if action.get("type") == "applemusicplay":
            apple_music_url = action.get("uri")

    return ShazamTrack(
        title=title,
        artist=artist,
        album=album,
        genres=tuple(genres),
        release_year=release_year,
        cover_url=cover_url,
        shazam_url=shazam_url,
        apple_music_url=apple_music_url,
    )


def format_shazam_caption(track: ShazamTrack, registry: EmojiRegistry, bot_username: str) -> str:
    """Format sleek Shazam recognition card."""
    lines = [
        "🔍 <b>موزیک با موفقیت شناسایی شد!</b>",
        "",
        f"{registry.text('music')} <b>نام آهنگ:</b> <code>{html.quote(track.title)}</code>",
        f"{registry.text('artist')} <b>خواننده:</b> <code>{html.quote(track.artist)}</code>",
    ]
    if track.album:
        lines.append(f"{registry.text('album')} <b>آلبوم:</b> {html.quote(track.album)}")
    if track.release_year:
        lines.append(
            f"{registry.text('clock')} <b>سال انتشار:</b> {html.quote(track.release_year)}"
        )
    if track.genres:
        lines.append(f"{registry.text('genre')} <b>سبک:</b> {html.quote(', '.join(track.genres))}")

    lines.append(f"\nشناسایی شده با @{bot_username} {registry.text('paw')}")
    return "\n".join(lines)


def build_shazam_keyboard(sid: str, track: ShazamTrack, registry: EmojiRegistry) -> KB:
    """Build action buttons under identified song."""
    kb = KB(registry)

    # Row 1: Direct Download button
    kb.row(
        inline_button(
            "دانلود این آهنگ (MP3 320)",
            callback_data=ShazamCB(action="dl", sid=sid).pack(),
            style=Style.CONFIRM,
            emoji_slot="download",
            registry=registry,
        )
    )

    # Row 2: Lyrics button
    kb.row(
        inline_button(
            "متن ترانه (Lyrics)",
            callback_data=ShazamCB(action="lyrics", sid=sid).pack(),
            style=Style.PRIMARY,
            emoji_slot="lyrics",
            registry=registry,
        )
    )

    # Row 3: Shazam / Apple Music link
    external_buttons = []
    if track.shazam_url:
        external_buttons.append(
            inline_button(
                "صفحه در Shazam",
                url=track.shazam_url,
                style=Style.PRIMARY,
                emoji_slot="bullet",
                registry=registry,
            )
        )
    if track.apple_music_url:
        external_buttons.append(
            inline_button(
                "Apple Music",
                url=track.apple_music_url,
                style=Style.PRIMARY,
                emoji_slot="bullet",
                registry=registry,
            )
        )
    if external_buttons:
        kb.row(*external_buttons)

    # Row 4: Close
    kb.row(
        inline_button(
            "بستن",
            callback_data=ShazamCB(action="close", sid=sid).pack(),
            style=Style.DESTRUCTIVE,
            emoji_slot="error",
            registry=registry,
        )
    )

    return kb
