"""Social media & web media downloader engine (yt-dlp + gallery-dl).

Supports over 1800 platforms:
- YouTube (video, shorts, audio/music)
- Instagram (reels, stories, posts, carousels)
- TikTok (videos without watermark, audio)
- Twitter / X (videos, GIFs, photos)
- Pinterest (video pins, high-res image pins)
- SoundCloud (audio tracks)
- Reddit, Aparat, Facebook, Vimeo and more.

All downloads are scoped to temporary directories and bounded by the
process-wide job concurrency limiter and timeouts.
"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yt_dlp

from app.cache.fallback import FallbackCache
from app.cache.redis import get_redis
from app.config import get_settings
from app.core.errors import ConversionError
from app.core.logging import get_logger
from app.core.units import human_duration
from app.media.worker import run_worker

log = get_logger(__name__)

_ytdlp_init_lock = threading.Lock()


def _make_ydl(opts: dict[str, Any]) -> yt_dlp.YoutubeDL:
    """Thread-safe YoutubeDL factory preventing concurrent plugin registration race."""
    with _ytdlp_init_lock:
        return yt_dlp.YoutubeDL(opts)


# Fallback in-memory token cache (token -> payload dict, capped).
_IN_MEMORY_TOKENS = FallbackCache(1024, 3600)

URL_REGEX = re.compile(
    r"https?://(?:www\.)?[-a-zA-Z0-9@:%._+~#=]{1,256}\.[a-zA-Z0-9()]{1,6}\b(?:[-a-zA-Z0-9()@:%_+.~#?&/=]*)",
    re.IGNORECASE,
)

PLATFORM_INFO: dict[str, tuple[str, str]] = {
    "youtube": ("یوتیوب", "🎬"),
    "instagram": ("اینستاگرام", "📱"),
    "tiktok": ("تیک‌تاک", "🎵"),
    "twitter": ("توییتر (X)", "🐦"),
    "pinterest": ("پینترست", "📌"),
    "soundcloud": ("ساندکلاد", "🎧"),
    "spotify": ("اسپاتیفای", "🟢"),
    "reddit": ("ردیت", "🤖"),
    "aparat": ("آپارات", "📺"),
    "facebook": ("فیسبوک", "🌐"),
    "other": ("وب‌سایت", "🌐"),
}


def find_cookie_file(platform_key: str | None = None) -> Path | None:
    """Check for platform-specific cookie file."""
    if platform_key not in PLATFORM_INFO or platform_key in ("spotify", "other"):
        return None
    p = get_settings().cookie_dir / f"{platform_key}.txt"
    try:
        if p.is_file() and p.stat().st_size > 0 and os.access(p, os.R_OK):
            return p
    except OSError:
        pass
    return None


@dataclass(frozen=True, slots=True)
class DownloadInfo:
    """Normalized metadata for a media URL."""

    url: str
    platform_key: str
    platform_name: str
    platform_icon: str
    title: str
    duration_seconds: int | None
    duration_str: str
    uploader: str | None
    thumbnail_url: str | None
    is_video: bool
    is_audio_only: bool
    is_gallery: bool
    is_playlist: bool = False
    playlist_count: int = 0
    playlist_items: tuple[dict[str, Any], ...] = ()


SUPPORTED_PLATFORMS: frozenset[str] = frozenset(
    {
        "youtube",
        "instagram",
        "tiktok",
        "twitter",
        "pinterest",
        "soundcloud",
        "spotify",
        "reddit",
        "aparat",
        "facebook",
    }
)

DIRECT_MEDIA_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".mp4",
        ".mkv",
        ".webm",
        ".mov",
        ".mp3",
        ".m4a",
        ".ogg",
        ".wav",
        ".flac",
    }
)


def is_direct_media_url(url: str) -> bool:
    """Return True if the URL directly points to an audio or video file extension."""
    if not url:
        return False
    try:
        path = urlparse(url).path.lower()
        return any(path.endswith(ext) for ext in DIRECT_MEDIA_EXTENSIONS)
    except Exception:
        return False


def is_supported_media_url(url: str) -> bool:
    """Check if the URL belongs to a supported downloader platform or is a direct media file."""
    if not url or not is_safe_remote_url(url):
        return False
    key, _, _ = detect_platform(url)
    if key in SUPPORTED_PLATFORMS:
        return True
    return is_direct_media_url(url)


_LOCAL_HOST_SUFFIXES = (
    ".localhost",
    ".local",
    ".internal",
    ".home.arpa",
    ".lan",
)


def is_safe_remote_url(url: str) -> bool:
    """Reject URLs that point at loopback, private, link-local or internal hosts.

    The download engine runs inside the Docker network, next to Postgres,
    Redis, the Bot API server and the WARP proxy. Letting yt-dlp/gallery-dl
    fetch ``http://postgres:5432/...`` would turn the bot into an SSRF proxy.
    Public links stay allowed (the feature intentionally supports thousands of
    sites); only obviously internal targets are refused.
    """
    if not url:
        return False
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    if parsed.scheme.lower() not in ("http", "https"):
        return False
    # Embedded credentials can be used to confuse host parsing in downstream
    # tools, and are never needed for a public media link.
    if parsed.username or parsed.password:
        return False
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return False
    if host == "localhost" or host.endswith(_LOCAL_HOST_SUFFIXES):
        return False
    # Single-label names are Docker service names / intranet hosts. Public
    # media platforms always have a dot (youtu.be, x.com, ...).
    if "." not in host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        # libc/glibc also accepts historical IPv4 shorthands such as
        # ``127.1`` or octal/hex octets; normalise them before deciding.
        try:
            packed = socket.inet_aton(host)
        except OSError:
            return True
        ip = ipaddress.ip_address(packed)
    return bool(ip.is_global)


def _host_matches(host: str, *domains: str) -> bool:
    """True when *host* is exactly one of *domains* or a subdomain of one."""
    return any(host == domain or host.endswith(f".{domain}") for domain in domains)


def detect_platform(url: str) -> tuple[str, str, str]:
    """Identify the platform from the URL host.

    Matching is done on complete domain labels: ``netflix.com`` must not be
    mistaken for X/Twitter just because it happens to contain ``x.com``.
    """
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]

    if _host_matches(host, "youtube.com", "youtu.be", "youtube-nocookie.com"):
        key = "youtube"
    elif _host_matches(host, "instagram.com", "instagr.am"):
        key = "instagram"
    elif _host_matches(host, "tiktok.com"):
        key = "tiktok"
    elif _host_matches(host, "twitter.com", "x.com", "t.co"):
        key = "twitter"
    elif host == "pin.it" or host.startswith("pinterest.") or ".pinterest." in host:
        key = "pinterest"
    elif _host_matches(host, "soundcloud.com", "snd.sc"):
        key = "soundcloud"
    elif _host_matches(host, "spotify.com", "spotify.link", "spoti.fi"):
        key = "spotify"
    elif _host_matches(host, "reddit.com", "redd.it"):
        key = "reddit"
    elif _host_matches(host, "aparat.com"):
        key = "aparat"
    elif _host_matches(host, "facebook.com", "fb.watch", "fb.com", "fb.me"):
        key = "facebook"
    else:
        key = "other"

    name, icon = PLATFORM_INFO[key]
    return key, name, icon


def clean_instagram_url(url: str) -> str:
    """Normalize and strip tracking query parameters from Instagram URLs."""
    m = re.match(r"https?://(?:www\.)?instagram\.com/(p|reel|reels|tv)/([^/?#&]+)", url)
    if m:
        kind = m.group(1)
        code = m.group(2)
        if kind == "reels":
            kind = "reel"
        return f"https://www.instagram.com/{kind}/{code}/"
    return url


def is_instagram_video_url(url: str) -> bool:
    """Check if the Instagram URL is guaranteed to be a reel or video."""
    return bool(re.search(r"/(reel|reels|tv)/", url))


def extract_first_url(text: str) -> str | None:
    """Find the first HTTP(S) URL in the message text."""
    if not text:
        return None
    match = URL_REGEX.search(text.strip())
    if not match:
        return None
    raw_url = match.group(0)
    if "instagram.com" in raw_url:
        return clean_instagram_url(raw_url)
    return raw_url


async def store_download_context(token: str, payload: dict[str, Any]) -> None:
    """Save URL metadata under an 8-char short token with a 1-hour TTL."""
    key = f"mewvia:dl:{token}"
    try:
        redis = get_redis()
        await redis.set(key, json.dumps(payload), ex=3600)
        _IN_MEMORY_TOKENS.discard(token)
    except Exception as exc:
        log.warning("downloader_token_redis_failed", error=str(exc))
        _IN_MEMORY_TOKENS.put(token, payload)


async def get_download_context(token: str) -> dict[str, Any] | None:
    """Retrieve download context by short token."""
    key = f"mewvia:dl:{token}"
    try:
        redis = get_redis()
        raw = await redis.get(key)
        if raw:
            data: dict[str, Any] = json.loads(raw)
            return data
    except Exception as exc:
        log.warning("downloader_token_redis_get_failed", error=str(exc))

    return _IN_MEMORY_TOKENS.get(token)


def create_token() -> str:
    """Generate a collision-resistant 8-char hex token for callback_data."""
    return secrets.token_hex(4)


def _build_ytdlp_opts(
    *,
    download: bool = False,
    outtmpl: str | None = None,
    format_str: str | None = None,
    postprocessors: list[dict[str, Any]] | None = None,
    timeout: int = 25,
    max_filesize: int | None = None,
    platform_key: str | None = None,
    cookie_file: str | None = None,
) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": not download,
        "socket_timeout": timeout,
        "extract_flat": False,
        "nocheckcertificate": True,
        "remote_components": ["ejs:github"],
        "proxy": "socks5h://warp:1080",
        "concurrent_fragment_downloads": 5,
        "extractor_args": {
            "youtubepot-bgutilhttp": {
                "base_url": ["http://warp:4416"],
            },
        },
    }

    # Enable browser TLS fingerprint impersonation (bypasses Instagram/Cloudflare bot gating)
    try:
        from yt_dlp.networking.impersonate import ImpersonateTarget

        opts["impersonate"] = ImpersonateTarget.from_str("chrome")
    except Exception as exc:
        log.debug("ytdlp_impersonate_setup_failed", error=str(exc))

    if cookie_file:
        opts["cookiefile"] = str(cookie_file)
    else:
        cookie = find_cookie_file(platform_key)
        if cookie:
            opts["cookiefile"] = str(cookie)

    if outtmpl:
        opts["outtmpl"] = outtmpl
    if format_str:
        opts["format"] = format_str
    if postprocessors:
        opts["postprocessors"] = postprocessors
    if max_filesize:
        opts["max_filesize"] = max_filesize

    return opts


def extract_spotify_metadata_sync(url: str) -> dict[str, Any]:
    """Fetch track or playlist/album metadata from Spotify public embeds/oembed."""
    clean_url = url.split("?")[0].rstrip("/")
    parsed = urlparse(clean_url)
    parts = [p for p in parsed.path.split("/") if p]
    kind = parts[0] if parts else "track"
    item_id = parts[1] if len(parts) > 1 else ""

    oembed_url = f"https://open.spotify.com/oembed?url={clean_url}"
    oembed_data: dict[str, Any] = {}
    try:
        import urllib.request

        req = urllib.request.Request(oembed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:  # nosec B310
            oembed_data = json.loads(r.read(256 * 1024).decode("utf-8"))
    except Exception as exc:
        log.warning("spotify_oembed_failed", url=url, error=str(exc))

    title = oembed_data.get("title") or "Spotify Media"
    thumbnail = oembed_data.get("thumbnail_url")

    tracks: list[dict[str, Any]] = []
    artist: str | None = None
    duration_sec: int | None = None

    embed_url = f"https://open.spotify.com/embed/{kind}/{item_id}"
    try:
        import urllib.request

        req = urllib.request.Request(embed_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:  # nosec B310
            html_text = r.read(4 * 1024 * 1024).decode("utf-8")
            m = re.search(
                r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                html_text,
                re.DOTALL,
            )
            if m:
                d = json.loads(m.group(1))
                entity = (
                    d.get("props", {})
                    .get("pageProps", {})
                    .get("state", {})
                    .get("data", {})
                    .get("entity", {})
                )
                artists_list = entity.get("artists", [])
                if artists_list:
                    artist = ", ".join(a.get("name") for a in artists_list if a.get("name"))

                if kind == "track":
                    dur_ms = entity.get("duration")
                    if dur_ms:
                        duration_sec = int(dur_ms / 1000)
                    track_name = entity.get("name") or title
                    tracks.append(
                        {
                            "title": track_name,
                            "artist": artist,
                            "duration_seconds": duration_sec,
                        }
                    )
                elif kind in ("playlist", "album"):
                    track_list = entity.get("trackList", [])
                    for t in track_list:
                        t_dur = int(t.get("duration", 0) / 1000) if t.get("duration") else None
                        tracks.append(
                            {
                                "title": t.get("title") or "Unknown",
                                "artist": t.get("subtitle") or artist or "Various Artists",
                                "duration_seconds": t_dur,
                            }
                        )
    except Exception as exc:
        log.warning("spotify_embed_parse_failed", url=url, error=str(exc))

    if not tracks and kind == "track":
        tracks.append(
            {
                "title": title,
                "artist": artist,
                "duration_seconds": None,
            }
        )

    return {
        "kind": kind,
        "title": title,
        "artist": artist,
        "thumbnail": thumbnail,
        "duration_seconds": duration_sec,
        "tracks": tracks,
    }


def _download_spotify_track_sync(
    title: str,
    artist: str | None,
    thumbnail_url: str | None,
    work_dir: Path,
    max_bytes: int | None = None,
) -> Path:
    """Download matching audio for a Spotify track in a single pass with SoundCloud fallback."""
    query_parts = [artist, title, "audio"] if artist else [title, "audio"]
    clean_q = " ".join(p for p in query_parts if p)
    yt_query = f"ytsearch1:{clean_q}"
    sc_query = f"scsearch5:{artist} {title}" if artist else f"scsearch5:{title}"

    # Try SoundCloud first (lightning fast ~0.8s, no bot-gating, high fidelity)
    mp3_file: Path | None = None
    try:
        mp3_file = _download_audio_sync(sc_query, work_dir, max_bytes=max_bytes, ignore_errors=True)
    except Exception as sc_exc:
        log.debug("spotify_sc_primary_failed", query=sc_query, error=str(sc_exc)[:150])

    # Fallback to YouTube if SoundCloud didn't return a file
    if not mp3_file or not mp3_file.is_file():
        try:
            mp3_file = _download_audio_sync(yt_query, work_dir, max_bytes=max_bytes)
        except Exception as yt_exc:
            log.warning(
                "spotify_yt_fallback_failed",
                query=clean_q,
                error=str(yt_exc)[:150],
            )

    if not mp3_file or not mp3_file.is_file():
        # Title only fallback on SoundCloud
        try:
            mp3_file = _download_audio_sync(
                f"scsearch5:{title}", work_dir, max_bytes=max_bytes, ignore_errors=True
            )
        except Exception as t_exc:
            log.warning("spotify_title_fallback_failed", title=title, error=str(t_exc)[:150])

    if not mp3_file or not mp3_file.is_file():
        raise ConversionError(f"امکان دانلود قطعه «{title}» از ساندکلاد یا یوتیوب وجود نداشت.")

    # Embed original Spotify cover and ID3 tags using Mutagen
    try:
        from mutagen.id3 import APIC, ID3, TIT2, TPE1, ID3NoHeaderError

        try:
            tags = ID3(mp3_file)
        except ID3NoHeaderError:
            tags = ID3()

        tags.add(TIT2(encoding=3, text=title))
        if artist:
            tags.add(TPE1(encoding=3, text=artist))
        if thumbnail_url:
            try:
                import urllib.request

                req = urllib.request.Request(thumbnail_url, headers={"User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=10) as resp:  # nosec B310
                    thumb_bytes = resp.read(4 * 1024 * 1024 + 1)
                    if len(thumb_bytes) > 4 * 1024 * 1024:
                        raise ValueError("cover too large")
                tags.add(
                    APIC(
                        encoding=3,
                        mime="image/jpeg",
                        type=3,
                        desc="Cover",
                        data=thumb_bytes,
                    )
                )
            except Exception:
                pass
        tags.save(mp3_file)
    except Exception as exc:
        log.warning("spotify_tagging_failed", error=str(exc))

    return mp3_file


async def download_spotify_track(
    title: str,
    artist: str | None,
    thumbnail_url: str | None,
    work_dir: Path,
    max_bytes: int | None = None,
) -> Path:
    """Async wrapper for downloading and tagging a Spotify track."""
    return Path(await run_worker("spotify", title, artist, thumbnail_url, work_dir, max_bytes))


def _probe_info_sync(url: str, cookie_file: str | None = None) -> DownloadInfo:
    """Extract metadata using yt-dlp, falling back to gallery-dl if needed."""
    platform_key, platform_name, platform_icon = detect_platform(url)

    # 1. Spotify special handler
    if platform_key == "spotify":
        sp = extract_spotify_metadata_sync(url)
        kind = sp["kind"]
        title = sp["title"]
        artist = sp["artist"]
        thumb = sp["thumbnail"]
        tracks = sp["tracks"]
        is_pl = kind in ("playlist", "album")
        dur_sec = sp.get("duration_seconds")
        dur_str = human_duration(float(dur_sec)) if dur_sec else "—"

        return DownloadInfo(
            url=url,
            platform_key="spotify",
            platform_name="اسپاتیفای",
            platform_icon="🟢",
            title=title,
            duration_seconds=dur_sec,
            duration_str=dur_str,
            uploader=artist or "Spotify",
            thumbnail_url=thumb,
            is_video=False,
            is_audio_only=True,
            is_gallery=False,
            is_playlist=is_pl,
            playlist_count=len(tracks),
            playlist_items=tuple(tracks),
        )

    if platform_key == "instagram":
        url = clean_instagram_url(url)
    is_ig_video = platform_key == "instagram" and is_instagram_video_url(url)

    # 2. General yt-dlp extraction
    opts = _build_ytdlp_opts(
        download=False,
        timeout=15,
        platform_key=platform_key,
        cookie_file=cookie_file,
    )
    opts["extract_flat"] = "in_playlist"
    info: dict[str, Any] | None = None
    probe_error: str | None = None
    try:
        with _make_ydl(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        probe_error = str(exc)
        log.warning("ytdlp_probe_failed", url=url, error=probe_error[:300])
        # If Instagram, retry with Instagram InfoExtractor directly to bypass
        # transient JSON / checkpoint errors
        if platform_key == "instagram":
            # 1. First retry anonymously without any cookie if cookie was used
            if opts.get("cookiefile"):
                opts_anon = dict(opts)
                opts_anon.pop("cookiefile", None)
                try:
                    with _make_ydl(opts_anon) as ydl_anon:
                        info = ydl_anon.extract_info(url, download=False)
                except Exception as anon_exc:
                    log.warning("instagram_anon_retry_failed", error=str(anon_exc))

            # 2. If tried anonymously and failed, retry with disk cookie if available
            elif not opts.get("cookiefile"):
                disk_cookie = find_cookie_file("instagram")
                if disk_cookie:
                    opts_cookie = dict(opts)
                    opts_cookie["cookiefile"] = str(disk_cookie)
                    try:
                        with _make_ydl(opts_cookie) as ydl_cookie:
                            info = ydl_cookie.extract_info(url, download=False)
                    except Exception as c_exc:
                        log.warning("instagram_cookie_retry_failed", error=str(c_exc))

            # 3. If still no info, retry with raw InfoExtractor
            if not info:
                try:
                    opts_raw = dict(opts)
                    opts_raw.pop("cookiefile", None)
                    with _make_ydl(opts_raw) as ydl_retry:
                        ie = ydl_retry.get_info_extractor("Instagram")
                        ie.initialize()
                        ie.raise_no_formats = lambda *a, **kw: None
                        info = ie.extract(url)
                except Exception as retry_exc:
                    log.warning("instagram_retry_probe_failed", error=str(retry_exc))

        # 4. If TikTok failed via yt-dlp (e.g. sensitive content, age restriction), retry via TikWM
        if platform_key == "tiktok" and not info:
            log.info("trying_tiktok_probe_via_tikwm", url=url)
            info = _probe_tiktok_via_tikwm(url)

    # 3. Check gallery-dl compatibility if yt-dlp failed or gave no media
    is_gallery = False
    can_be_gallery = platform_key in ("instagram", "pinterest", "reddit", "twitter") or (
        platform_key == "tiktok"
        and ("/photo/" in url.lower() or (info and info.get("_fallback_images")))
    )
    if (
        can_be_gallery
        and not is_ig_video
        and (not info or (info.get("_type") == "playlist" and not info.get("entries")))
    ):
        is_blocked = False
        if probe_error:
            err_lower = probe_error.lower()
            if any(
                k in err_lower
                for k in (
                    "not comfortable for some audiences",
                    "log in for access",
                    "sign in to confirm",
                    "login required",
                    "private video",
                    "this video is private",
                    "unavailable",
                    "blocked from accessing",
                    "ip address is blocked",
                )
            ):
                is_blocked = True

        if not is_blocked:
            try:
                from gallery_dl import extractor

                if extractor.find(url):
                    is_gallery = True
            except Exception:
                pass

    if not info and not is_gallery:
        if probe_error:
            err_lower = probe_error.lower()
            if (
                "not comfortable for some audiences" in err_lower
                or "log in for access" in err_lower
            ):
                if platform_key == "tiktok":
                    raise ConversionError(
                        "⚠️ این پست در تیک‌تاک دارای محتوای حساس یا محدودیت سنی (۱۸+) است "
                        "و نیاز به ورود به حساب دارد."
                    )
                raise ConversionError(
                    f"⚠️ مشاهده این محتوا در {platform_name} نیاز به ورود به حساب کاربری دارد."
                )
            if "sign in to confirm" in err_lower or "bot" in err_lower:
                raise ConversionError(
                    "⚠️ یوتیوب برای مشاهده این ویدیو درخواست تأیید هویت (Bot Verification) داده است."
                )
            if "private" in err_lower:
                raise ConversionError("🔒 این ویدیو خصوصی (Private) است.")
            if "unavailable" in err_lower or "deleted" in err_lower or "removed" in err_lower:
                raise ConversionError("❌ این ویدیو در دسترس نیست یا حذف شده است.")
            if "blocked from accessing" in err_lower or "ip address is blocked" in err_lower:
                raise ConversionError(
                    f"🚫 دسترسی موقت به این محتوا از سوی {platform_name} مسدود شده است."
                )
        raise ConversionError(f"امکان استخراج اطلاعات از این لینک {platform_name} وجود ندارد.")

    info = info or {}
    raw_desc = (info.get("description") or "").strip()
    raw_title = (info.get("title") or "").strip()

    is_dummy_title = (
        not raw_title
        or raw_title.startswith("Video by ")
        or raw_title.startswith("Post by ")
        or raw_title.startswith("رسانه ")
        or raw_title.lower()
        in ("media", "video", "post", "audio", "instagram post", "instagram video")
    )

    if raw_desc:
        first_line = raw_desc.splitlines()[0].strip()
        title = (first_line[:90] + "…") if len(first_line) > 90 else first_line
    elif not is_dummy_title:
        title = (raw_title[:90] + "…") if len(raw_title) > 90 else raw_title
    else:
        title = ""

    duration = info.get("duration")
    duration_sec = int(duration) if duration and isinstance(duration, (int, float)) else None
    duration_str = human_duration(float(duration_sec)) if duration_sec else "—"

    uploader = info.get("uploader") or info.get("channel") or info.get("creator")
    thumbnail = info.get("thumbnail") or (
        info["thumbnails"][-1]["url"] if info.get("thumbnails") else None
    )

    # Detect video formats on top level or within wrapped entries (e.g. Twitter/X, TikTok)
    formats = info.get("formats", [])
    has_video = any(f.get("vcodec") and f.get("vcodec") != "none" for f in formats)

    entries = info.get("entries") or []
    if not has_video and entries:
        for e in entries:
            if isinstance(e, dict):
                e_fmts = e.get("formats") or []
                if any(f.get("vcodec") and f.get("vcodec") != "none" for f in e_fmts) or (
                    e.get("vcodec") and e.get("vcodec") != "none"
                ):
                    has_video = True
                    break

    # Detect playlist in yt-dlp response.
    # Only YouTube, Spotify, and SoundCloud can be treated as multi-track playlists/albums.
    # Social posts (Twitter/X, TikTok, Instagram, etc.) are single media or carousels!
    is_pl = False
    pl_items: list[dict[str, Any]] = []
    if platform_key in ("youtube", "spotify", "soundcloud"):
        is_pl = (info.get("_type") == "playlist") or (
            bool(entries) and (not formats or info.get("_type") == "playlist")
        )
        if is_pl and entries:
            for e in entries:
                if isinstance(e, dict):
                    pl_items.append(
                        {
                            "title": (e.get("title") or "Unknown").strip(),
                            "url": e.get("url") or e.get("webpage_url") or "",
                            "duration_seconds": e.get("duration"),
                            "uploader": e.get("uploader") or uploader,
                        }
                    )

    if is_ig_video:
        has_video = True
        is_gallery = False
        is_audio_only = False
    else:
        is_audio_only = platform_key in ("soundcloud", "spotify") or (
            not has_video and bool(formats)
        )
        if not has_video and not is_audio_only:
            is_gallery = True

    return DownloadInfo(
        url=url,
        platform_key=platform_key,
        platform_name=platform_name,
        platform_icon=platform_icon,
        title=title,
        duration_seconds=duration_sec,
        duration_str=duration_str,
        uploader=uploader,
        thumbnail_url=thumbnail,
        is_video=has_video and not is_audio_only,
        is_audio_only=is_audio_only,
        is_gallery=is_gallery,
        is_playlist=is_pl,
        playlist_count=len(pl_items) if is_pl else 0,
        playlist_items=tuple(pl_items),
    )


async def probe_media_url(url: str, cookie_file: str | None = None) -> DownloadInfo:
    """Async wrapper for metadata probe."""
    return DownloadInfo(**await run_worker("probe", url, cookie_file))


_IOS_VIDEO_CODECS = frozenset({"h264", "hevc"})
_IOS_AUDIO_CODECS = frozenset({"aac", "mp3", "mp2"})
_IOS_PIX_FMTS = frozenset({"yuv420p", "yuvj420p"})
_VIDEO_CONTAINERS = frozenset({".mp4", ".m4v", ".mov", ".mkv", ".webm"})
_GALLERY_MEDIA_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".webp", ".gif"} | _VIDEO_CONTAINERS)


def _ensure_ios_compatibility_sync(video_path: Path, work_dir: Path) -> Path:
    """Ensure downloaded video is 100% compatible with Apple iOS Photos/Camera Roll.

    Apple iOS (Photos.app, QuickTime, AVFoundation) strictly requires:
    - Video codec: H.264 (avc1) or HEVC (hvc1). VP9/AV1 videos cannot be saved on iOS!
    - Pixel format: yuv420p (8-bit 4:2:0).
    - Audio codec: AAC or MP3 (or none).
    - Container: MP4 with +faststart (moov atom at beginning of file).

    Both video *and* audio are checked: an H.264 stream muxed with Opus is
    still unplayable on iOS, and WebM/MKV files were previously returned
    untouched.
    """
    if not video_path.is_file() or video_path.stat().st_size == 0:
        return video_path
    if video_path.suffix.lower() not in _VIDEO_CONTAINERS:
        return video_path

    try:
        cmd = [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name,pix_fmt",
            "-of",
            "json",
            str(video_path),
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        data = json.loads(res.stdout or "{}")
        streams = data.get("streams", [])
        video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
        if video_stream is None:
            return video_path
        audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

        vcodec = (video_stream.get("codec_name") or "").lower()
        acodec = (audio_stream.get("codec_name") or "").lower() if audio_stream else ""
        pix_fmt = (video_stream.get("pix_fmt") or "").lower()
        # A video without any audio track is perfectly playable on iOS, so the
        # missing stream must not force a full re-encode.
        audio_ok = audio_stream is None or acodec in _IOS_AUDIO_CODECS

        # Keep the converted file next to its source. Gallery downloads keep
        # per-site subfolders, and two folders can contain the same filename;
        # writing every result into `work_dir` would overwrite one of them.
        out_dir = work_dir
        if video_path.parent != work_dir and work_dir in video_path.parents:
            out_dir = video_path.parent
        out_path = out_dir / f"ios_{video_path.stem}.mp4"
        if out_path == video_path:
            out_path = out_dir / f"ios_{video_path.stem}_converted.mp4"

        # Already H.264/HEVC with a compatible pixel format and audio: only a
        # faststart remux (and container change for MKV/MOV) is needed.
        if vcodec in _IOS_VIDEO_CODECS and pix_fmt in _IOS_PIX_FMTS and audio_ok:
            faststart_cmd = [
                "ffmpeg",
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(video_path),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(out_path),
            ]
            fs_res = subprocess.run(faststart_cmd, capture_output=True, timeout=30)
            if fs_res.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0:
                return out_path
            # Fall through to a full transcode; a copy can fail on odd
            # containers even when the codecs themselves are supported.

        # Unsupported codec/container: transcode to standard H.264 + AAC + yuv420p.
        log.info("transcoding_video_for_ios_camera_roll", vcodec=vcodec, pix_fmt=pix_fmt)
        transcode_cmd = [
            "ffmpeg",
            "-y",
            "-loglevel",
            "error",
            "-i",
            str(video_path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-crf",
            "23",
            "-pix_fmt",
            "yuv420p",
        ]
        if audio_stream is not None:
            transcode_cmd.extend(["-c:a", "aac", "-b:a", "128k"])
        else:
            transcode_cmd.append("-an")
        transcode_cmd.extend(["-movflags", "+faststart", str(out_path)])
        tc_res = subprocess.run(transcode_cmd, capture_output=True, timeout=120)
        if tc_res.returncode == 0 and out_path.is_file() and out_path.stat().st_size > 0:
            return out_path
    except Exception as exc:
        log.warning("ios_video_compatibility_check_failed", error=str(exc))

    return video_path


def _download_video_sync(
    url: str,
    work_dir: Path,
    max_height: int | None = None,
    max_bytes: int | None = None,
    cookie_file: str | None = None,
) -> Path:
    if "instagram.com" in url:
        url = clean_instagram_url(url)
    outtmpl = str(work_dir / "media.%(ext)s")

    if max_height:
        format_str = (
            f"bestvideo[height<={max_height}][vcodec^=avc]+bestaudio[ext=m4a]/"
            f"bestvideo[height<={max_height}][vcodec^=h264]+bestaudio[ext=m4a]/"
            f"best[height<={max_height}][vcodec^=avc]/"
            f"best[height<={max_height}][vcodec^=h264]/"
            f"best[height<={max_height}][format_id!*=dash]/"
            f"bestvideo[height<={max_height}][ext=mp4]+bestaudio[ext=m4a]/"
            f"best[height<={max_height}][ext=mp4]/"
            f"best[height<={max_height}]/best"
        )
    else:
        format_str = (
            "b[vcodec^=avc]/b[vcodec^=h264]/"
            "bv[vcodec^=avc]+ba[ext=m4a]/bv[vcodec^=h264]+ba[ext=m4a]/"
            "best[format_id!*=dash]/"
            "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        )

    opts = _build_ytdlp_opts(
        download=True,
        outtmpl=outtmpl,
        format_str=format_str,
        timeout=45,
        max_filesize=max_bytes,
        cookie_file=cookie_file,
    )
    opts["merge_output_format"] = "mp4"

    try:
        with _make_ydl(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        is_tiktok = any(k in url.lower() for k in ("tiktok.com", "vm.tiktok", "vt.tiktok"))
        if is_tiktok:
            log.info("trying_tiktok_download_via_tikwm", url=url)
            fb_path = _download_tiktok_via_tikwm(url, work_dir)
            if fb_path and fb_path.is_file() and fb_path.stat().st_size > 0:
                return fb_path

        err_str = str(exc).lower()
        if opts.get("cookiefile"):
            log.warning(
                "video_download_failed_with_cookie_retrying_anonymous",
                url=url,
                error=str(exc),
            )
            opts_no_cookie = dict(opts)
            opts_no_cookie.pop("cookiefile", None)
            try:
                with _make_ydl(opts_no_cookie) as ydl_retry:
                    ydl_retry.download([url])
            except Exception as retry_exc:
                retry_err = str(retry_exc).lower()
                if (
                    "login" in retry_err
                    or "empty media" in retry_err
                    or "redirect to login" in retry_err
                ):
                    raise ConversionError(
                        "⚠️ اینستاگرام برای مشاهده این پست نیاز به لاگین دارد.\n\n"
                        "علت: این پست احتمالاً در یک پیج خصوصی (Private) قرار دارد."
                    ) from retry_exc
                raise ConversionError(f"خطا در دانلود ویدیو: {str(retry_exc)[:150]}") from retry_exc
        else:
            if "login" in err_str or "empty media" in err_str or "redirect to login" in err_str:
                if is_tiktok:
                    raise ConversionError(
                        "⚠️ این ویدیو در تیک‌تاک دارای محتوای حساس یا محدودیت سنی (۱۸+) است "
                        "و نیاز به ورود به حساب دارد."
                    ) from None
                raise ConversionError(
                    "⚠️ اینستاگرام برای مشاهده این پست نیاز به لاگین دارد.\n\n"
                    "علت: این پست احتمالاً در یک پیج خصوصی (Private) قرار دارد."
                ) from None
            if "not comfortable for some audiences" in err_str or "log in for access" in err_str:
                raise ConversionError(
                    "⚠️ این ویدیو در تیک‌تاک دارای محتوای حساس یا محدودیت سنی (۱۸+) است "
                    "و نیاز به ورود به حساب دارد."
                ) from None
            if "sign in to confirm" in err_str:
                raise ConversionError(
                    "⚠️ یوتیوب برای این ویدیو درخواست تأیید هویت (Bot Verification) داده است."
                ) from None
            raise

    # Find the output file
    candidates = list(work_dir.glob("media.*"))
    if not candidates:
        # Fallback search for any non-empty file
        candidates = [p for p in work_dir.glob("*") if p.is_file() and p.stat().st_size > 0]

    if not candidates:
        raise ConversionError("دانلود ویدیو ناموفق بود (فایلی ذخیره نشد).")

    # Prefer .mp4 if multiple
    mp4s = [p for p in candidates if p.suffix.lower() == ".mp4"]
    selected = mp4s[0] if mp4s else candidates[0]
    return _ensure_ios_compatibility_sync(selected, work_dir)


def _download_audio_sync(
    url: str,
    work_dir: Path,
    max_bytes: int | None = None,
    cookie_file: str | None = None,
    ignore_errors: bool = False,
) -> Path:
    outtmpl = str(work_dir / "audio.%(ext)s")
    postprocessors = [
        {
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }
    ]

    opts = _build_ytdlp_opts(
        download=True,
        outtmpl=outtmpl,
        format_str="140/bestaudio[ext=m4a]/bestaudio/best",
        postprocessors=postprocessors,
        timeout=45,
        max_filesize=max_bytes,
        cookie_file=cookie_file,
    )
    opts["postprocessor_args"] = {"FFmpegExtractAudio": ["-threads", "2"]}
    if ignore_errors:
        opts["ignoreerrors"] = True

    try:
        with _make_ydl(opts) as ydl:
            ydl.download([url])
    except Exception as exc:
        err_str = str(exc).lower()
        if "login" in err_str or "empty media" in err_str or "redirect to login" in err_str:
            raise ConversionError(
                "⚠️ اینستاگرام برای مشاهده این پست نیاز به لاگین یا کوکی دارد.\n\n"
                "علت: این پست ممکن است خصوصی باشد، یا اینستاگرام برای آی‌پی سرور محدودیت گذاشته است."
            ) from None
        if "sign in to confirm" in err_str:
            raise ConversionError(
                "⚠️ یوتیوب برای این ویدیو درخواست تأیید هویت (Bot Verification) داده است."
            ) from None
        raise

    candidates = list(work_dir.glob("audio.mp3"))
    if not candidates:
        candidates = [p for p in work_dir.glob("*") if p.is_file() and p.stat().st_size > 0]

    if not candidates:
        raise ConversionError("استخراج صوت ناموفق بود.")

    return candidates[0]


def _download_gallery_sync(
    url: str,
    work_dir: Path,
    cookie_file: str | None = None,
) -> list[Path]:
    """Download images/galleries using gallery-dl CLI."""
    if "instagram.com" in url:
        url = clean_instagram_url(url)
    dest_dir = work_dir / "gallery"
    dest_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        sys.executable,
        "-m",
        "gallery_dl",
        "--proxy",
        "socks5h://warp:1080",
        "--dest",
        str(dest_dir),
        "--no-mtime",
        "--range",
        "1-20",
        "--filesize-max",
        str(get_settings().media_max_bytes),
    ]

    platform_key, platform_name, _ = detect_platform(url)
    cookie = Path(cookie_file) if cookie_file else find_cookie_file(platform_key)
    if cookie and cookie.is_file() and cookie.stat().st_size > 0:
        cmd.extend(["--cookies", str(cookie)])

    if platform_key == "instagram":
        ua = (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
        )
        cmd.extend(
            [
                "-o",
                f"extractor.headers.User-Agent={ua}",
            ]
        )

    cmd.append(url)

    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise ConversionError("دانلود گالری به دلیل اتمام زمان متوقف شد.") from None

    # Collect downloaded image, animation and video files, ensuring iOS
    # compatibility for videos. Snapshot the inputs first: the converter writes
    # `ios_*.mp4` files into the same directory, and a live rglob could pick
    # those up as duplicates (or return both the WebM original and its MP4).
    source_files = [
        p
        for p in dest_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in _GALLERY_MEDIA_SUFFIXES
    ]
    results: list[Path] = []
    for path in source_files:
        converted = (
            _ensure_ios_compatibility_sync(path, dest_dir)
            if path.suffix.lower() in _VIDEO_CONTAINERS
            else path
        )
        if converted not in results:
            results.append(converted)

    if not results:
        err = proc.stderr.decode(errors="replace").strip()
        log.warning("gallery_dl_failed", error=err[:300])

        # Modern 2026 fallback: download Instagram photos via TLS-impersonated yt-dlp CDN thumbnails
        if platform_key == "instagram":
            log.info("trying_instagram_photo_fallback_via_ytdlp", url=url)
            fallback_photos = _download_instagram_photos_via_ytdlp(url, dest_dir)
            if fallback_photos:
                return sorted(fallback_photos)

        # Fallback for TikTok photos via TikWM
        if platform_key == "tiktok" or "tiktok.com" in url:
            log.info("trying_tiktok_photo_fallback_via_tikwm", url=url)
            fallback_photos = _download_tiktok_photos_via_tikwm(url, dest_dir)
            if fallback_photos:
                return sorted(fallback_photos)

        err_lower = err.lower()
        if "login" in err_lower or "redirect to login" in err_lower or "cookies" in err_lower:
            if platform_key == "instagram":
                raise ConversionError(
                    "⚠️ اینستاگرام برای مشاهده این پست نیاز به لاگین دارد.\n\n"
                    "علت: این پست ممکن است در یک پیج خصوصی باشد."
                )
            elif platform_key == "tiktok":
                raise ConversionError(
                    "⚠️ تیک‌تاک برای مشاهده این محتوا نیاز به ورود به حساب دارد.\n\n"
                    "علت: این پست دارای محدودیت سنی (۱۸+) یا محتوای حساس است."
                )
            else:
                raise ConversionError(
                    f"⚠️ برای مشاهده این محتوا از {platform_name} نیاز به لاگین است."
                )

        if platform_key in ("tiktok", "youtube", "twitter"):
            raise ConversionError(f"❌ امکان دریافت این مدیا از {platform_name} وجود نداشت.")
        raise ConversionError("فایلی در این پست یافت نشد یا دسترسی به آن محدود است.")

    return sorted(results)


def _download_instagram_photos_via_ytdlp(url: str, dest_dir: Path) -> list[Path]:
    """Extract and download Instagram media via Chrome TLS-impersonated yt-dlp CDN URLs."""
    import requests
    from yt_dlp.networking.impersonate import ImpersonateTarget

    clean_url = clean_instagram_url(url)
    opts: dict[str, Any] = {
        "quiet": True,
        "proxy": "socks5h://warp:1080",
        "impersonate": ImpersonateTarget.from_str("chrome"),
    }
    try:
        with _make_ydl(opts) as ydl:
            ie = ydl.get_info_extractor("Instagram")
            ie.initialize()
            ie.raise_no_formats = lambda *args, **kwargs: None
            info = ie.extract(clean_url)
        entries = info.get("entries") or [info]
        results: list[Path] = []
        remaining = get_settings().media_max_bytes
        for idx, e in enumerate(entries[:20], 1):
            formats = e.get("formats") or []
            v_fmts = [
                f for f in formats if f.get("vcodec") and f.get("vcodec") != "none" and f.get("url")
            ]
            if v_fmts:
                # Video item in post/carousel! Prefer AVC/H264 stream for iOS Camera Roll
                avc_fmts = [
                    f
                    for f in v_fmts
                    if "avc" in (f.get("vcodec") or "").lower()
                    or "h264" in (f.get("vcodec") or "").lower()
                ]
                chosen_fmt = avc_fmts[-1] if avc_fmts else v_fmts[-1]
                v_url = chosen_fmt["url"]
                resp = requests.get(
                    v_url, proxies={"https": "socks5h://warp:1080"}, stream=True, timeout=30
                )
                out = dest_dir / f"instagram_media_{idx:02d}.mp4"
                if _save_response(resp, out, remaining):
                    remaining -= out.stat().st_size
                    out = _ensure_ios_compatibility_sync(out, dest_dir)
                    results.append(out)
            else:
                # Photo item! Download full-res image
                thumbs = e.get("thumbnails") or []
                if thumbs:
                    img_url = thumbs[-1]["url"]
                    resp = requests.get(
                        img_url, proxies={"https": "socks5h://warp:1080"}, timeout=15, stream=True
                    )
                    out = dest_dir / f"instagram_media_{idx:02d}.jpg"
                    if _save_response(resp, out, remaining):
                        remaining -= out.stat().st_size
                        results.append(out)
        return results
    except Exception as exc:
        log.warning("instagram_photo_fallback_failed", error=str(exc))
        return []


def _probe_tiktok_via_tikwm(url: str) -> dict[str, Any] | None:
    """Fallback probe for TikTok videos/photos when yt-dlp faces 18+/sensitive restrictions."""
    import requests

    try:
        r = requests.get(
            f"https://www.tikwm.com/api/?url={url}",
            proxies={"https": "socks5h://warp:1080"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == 0:
                d = data.get("data", {})
                images = d.get("images") or []
                play = d.get("play")
                author = d.get("author") or {}
                uploader = author.get("nickname") or author.get("unique_id") or "TikTok"
                title = d.get("title") or ""
                thumb = d.get("cover")
                dur = d.get("duration")
                formats = []
                entries = []
                if play and not images:
                    formats = [{"url": play, "vcodec": "h264", "acodec": "aac", "ext": "mp4"}]
                elif images:
                    entries = [{"url": img, "thumbnail": img} for img in images]
                return {
                    "id": d.get("id", "tiktok_media"),
                    "title": title,
                    "description": title,
                    "uploader": uploader,
                    "thumbnail": thumb,
                    "duration": dur,
                    "formats": formats,
                    "entries": entries,
                    "_type": "playlist" if images else "video",
                    "_fallback_play_url": play,
                    "_fallback_images": images,
                }
    except Exception as exc:
        log.warning("tiktok_tikwm_probe_failed", error=str(exc))
    return None


def _download_tiktok_via_tikwm(url: str, work_dir: Path) -> Path | None:
    """Download TikTok video via TikWM when yt-dlp encounters restricted content."""
    import requests

    try:
        r = requests.get(
            f"https://www.tikwm.com/api/?url={url}",
            proxies={"https": "socks5h://warp:1080"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == 0:
                play = data.get("data", {}).get("play")
                if play:
                    out = work_dir / "media.mp4"
                    with requests.get(
                        play,
                        headers={"User-Agent": "Mozilla/5.0"},
                        proxies={"https": "socks5h://warp:1080"},
                        stream=True,
                        timeout=45,
                    ) as vr:
                        if _save_response(vr, out, get_settings().media_max_bytes):
                            if out.is_file() and out.stat().st_size > 0:
                                return _ensure_ios_compatibility_sync(out, work_dir)
    except Exception as exc:
        log.warning("tiktok_tikwm_download_failed", error=str(exc))
    return None


def _save_response(resp: Any, path: Path, limit: int) -> bool:
    with resp:
        if resp.status_code != 200:
            return False
        total = 0
        try:
            with path.open("wb") as out:
                for chunk in resp.iter_content(chunk_size=65536):
                    total += len(chunk)
                    if total > limit:
                        raise ConversionError("حجم فایل از حد مجاز بیشتر است.")
                    out.write(chunk)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return total > 0


def _download_tiktok_photos_via_tikwm(url: str, dest_dir: Path) -> list[Path]:
    """Download TikTok photo carousels via TikWM."""
    import requests

    results: list[Path] = []
    try:
        r = requests.get(
            f"https://www.tikwm.com/api/?url={url}",
            proxies={"https": "socks5h://warp:1080"},
            timeout=10,
        )
        if r.status_code == 200:
            data = r.json()
            if data.get("code") == 0:
                images = data.get("data", {}).get("images") or []
                remaining = get_settings().media_max_bytes
                for idx, img_url in enumerate(images[:20], 1):
                    resp = requests.get(
                        img_url,
                        headers={"User-Agent": "Mozilla/5.0"},
                        proxies={"https": "socks5h://warp:1080"},
                        timeout=20,
                        stream=True,
                    )
                    out = dest_dir / f"tiktok_photo_{idx:02d}.jpg"
                    if _save_response(resp, out, remaining):
                        remaining -= out.stat().st_size
                        results.append(out)
    except Exception as exc:
        log.warning("tiktok_tikwm_photos_download_failed", error=str(exc))
    return results


async def download_video(
    url: str,
    work_dir: Path,
    max_height: int | None = None,
    max_bytes: int | None = None,
    cookie_file: str | None = None,
) -> Path:
    """Async wrapper for video downloading."""
    return Path(await run_worker("video", url, work_dir, max_height, max_bytes, cookie_file))


async def download_audio(
    url: str,
    work_dir: Path,
    max_bytes: int | None = None,
    cookie_file: str | None = None,
) -> Path:
    """Async wrapper for audio downloading."""
    return Path(await run_worker("audio", url, work_dir, max_bytes, cookie_file))


async def download_gallery(
    url: str,
    work_dir: Path,
    cookie_file: str | None = None,
) -> list[Path]:
    """Async wrapper for gallery downloading."""
    return [Path(p) for p in await run_worker("gallery", url, work_dir, cookie_file)]
