"""Social media & web media downloader router.

Handles links sent by users:
1. Detects URL from message (YouTube, Instagram, TikTok, Twitter/X, Pinterest, SoundCloud, etc.)
2. Probes metadata and format availability
3. For YouTube videos and all Playlists/Albums: presents interactive options (quality/format/ZIP)
4. For all other platforms: auto-downloads immediately at top speed
5. Downloads within temporary sandbox under process-wide concurrency & quota controls
6. Sends media back to the user with custom captions and thumbnails
"""

from __future__ import annotations

import asyncio
import io
import re
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router, html
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter, Command, CommandObject, StateFilter
from aiogram.types import (
    CallbackQuery,
    FSInputFile,
    InputMediaPhoto,
    InputMediaVideo,
    Message,
)
from sqlalchemy.ext.asyncio import AsyncSession

from app.callbacks.downloader import DownloaderCB
from app.config import Settings
from app.core.errors import ConversionError, MewviaError
from app.core.logging import get_logger
from app.core.quota import check_daily_quota
from app.core.units import fa_digits, human_bytes
from app.db.repo import ConversionRepo, InstagramCookieRepo
from app.features.downloader.action_keyboards import (
    build_audio_actions_kb,
    build_photo_actions_kb,
    build_video_actions_kb,
)
from app.features.registry import Feature, register
from app.filters import IsAdmin
from app.media.downloader import (
    create_token,
    detect_platform,
    download_audio,
    download_gallery,
    download_spotify_track,
    download_video,
    extract_first_url,
    get_download_context,
    is_safe_remote_url,
    is_supported_media_url,
    probe_media_url,
    store_download_context,
)
from app.media.jobs import run_media_job
from app.media.kinds import MediaKind
from app.media.lyrics import (
    build_full_lyrics_message,
    embed_lyrics_to_mp3,
    fetch_lyrics,
)
from app.media.probe import probe
from app.middlewares.group_guard import store_panel
from app.ui.emoji import EmojiRegistry, get_emoji_registry
from app.ui.kb import KB, inline_button
from app.ui.progress import ProgressController, render_progress_bar
from app.ui.styles import Style
from app.ui.texts import error_text, t

log = get_logger(__name__)

router = Router(name="downloader")


def _add_to_zip(zip_path: Path, file_path: Path, arcname: str) -> None:
    with zipfile.ZipFile(zip_path, "a", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(file_path, arcname=arcname)


# Containers Telegram accepts via sendVideo. After the iOS pass every one of
# these should normally be `.mp4`, but a failed conversion may leave the
# original file behind.
_VIDEO_SUFFIXES = frozenset({".mp4", ".m4v", ".mov", ".mkv", ".webm"})


def _is_video_file(path: Path) -> bool:
    return path.suffix.lower() in _VIDEO_SUFFIXES


async def _edit_status(status_msg: ProgressController | Message | None, text: str) -> bool:
    """Edit the status message, tolerating deleted/uneditable messages."""
    if status_msg is None:
        return False
    try:
        await status_msg.edit_text(text)
        return True
    except Exception as exc:
        log.debug("download_status_edit_failed", error=str(exc))
        return False


async def _notify_error(
    status_msg: ProgressController | Message | None,
    text: str,
    *,
    bot: Bot,
    chat_id: int,
    fallback_msg: Message | None = None,
) -> None:
    """Deliver an error through the status message, reply chain or a fresh DM."""
    if await _edit_status(status_msg, text):
        return
    try:
        if fallback_msg is not None:
            await fallback_msg.reply(text)
        else:
            await bot.send_message(chat_id=chat_id, text=text)
    except Exception as exc:
        log.debug("download_error_notify_failed", error=str(exc))


def _context_owner_mismatch(ctx: dict[str, Any], user_id: int, chat_id: int) -> bool:
    """True when the callback belongs to a different user/chat than the request."""
    owner = ctx.get("user_id")
    chat = ctx.get("chat_id")
    return (isinstance(owner, int) and owner != user_id) or (
        isinstance(chat, int) and chat != chat_id
    )


async def ready_registry() -> EmojiRegistry:
    try:
        registry = get_emoji_registry()
        await registry.refresh_premium_flag()
        await registry.load()
        return registry
    except Exception:
        return get_emoji_registry()


async def _perform_download(
    *,
    chat_id: int,
    user_id: int,
    reply_to_id: int | None,
    action: str,
    ctx: dict[str, Any],
    status_msg: Message | ProgressController | None,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
    fallback_msg: Message | None = None,
) -> None:
    """Execute the media download and send the media back to the user."""
    registry = await ready_registry()
    url = str(ctx.get("url", ""))
    platform_key = str(ctx.get("platform_key", "other"))
    title = str(ctx.get("title", "media"))
    start_time = time.perf_counter()

    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=settings.tmp_dir) as tmp_dir:
        work_dir = Path(tmp_dir)

        cookie_file_str: str | None = None
        cookie_record = None
        repo_cookie = InstagramCookieRepo(session)
        if platform_key == "instagram":
            cookie_record = await repo_cookie.get_next_cookie()
            if (
                cookie_record
                and cookie_record.cookie_text
                and cookie_record.is_active
                and cookie_record.status != "invalid"
            ):
                ig_cookie_path = work_dir / "ig_session.txt"
                ig_cookie_path.write_text(cookie_record.cookie_text, encoding="utf-8")
                cookie_file_str = str(ig_cookie_path)

        # Release database transaction so connection returns to pool during heavy download
        await session.commit()

        try:
            if action.startswith("video"):
                max_h: int | None = None
                if action == "video_720":
                    max_h = 720
                elif action == "video_480":
                    max_h = 480
                elif action == "video_360":
                    max_h = 360

                async def _do_video() -> Path:
                    return await download_video(
                        url,
                        work_dir,
                        max_height=max_h,
                        max_bytes=settings.upload_limit,
                        cookie_file=cookie_file_str,
                    )

                downloaded_file = await run_media_job(settings, _do_video, stage="download_video")
                file_size = downloaded_file.stat().st_size

                if file_size > settings.upload_limit:
                    limit_str = human_bytes(settings.upload_limit)
                    size_str = human_bytes(file_size)
                    err_msg = f"❌ حجم ویدیو ({size_str}) بیشتر از سقف مجاز ({limit_str}) است."
                    await _notify_error(
                        status_msg,
                        err_msg,
                        bot=bot,
                        chat_id=chat_id,
                        fallback_msg=fallback_msg,
                    )
                    return

                caption_lines = []
                if title:
                    caption_lines.append(f"{registry.text('video')} <b>{html.quote(title)}</b>")
                caption_lines.append(
                    f"دانلود شده با @{settings.bot_username} {registry.text('paw')}"
                )
                caption = "\n\n".join(caption_lines)

                v_w: int | None = None
                v_h: int | None = None
                v_dur: int | None = None
                try:
                    v_info = await probe(downloaded_file)
                    v_w = v_info.width if v_info.width > 0 else None
                    v_h = v_info.height if v_info.height > 0 else None
                    v_dur = int(v_info.duration) if v_info.duration > 0 else None
                except Exception:
                    pass

                video_kb = build_video_actions_kb(registry)
                sent_video = await bot.send_video(
                    chat_id=chat_id,
                    video=FSInputFile(downloaded_file),
                    caption=caption[:1024],
                    width=v_w,
                    height=v_h,
                    duration=v_dur,
                    supports_streaming=True,
                    reply_to_message_id=reply_to_id,
                    reply_markup=video_kb.as_markup(),
                )

                if sent_video and sent_video.video:
                    await store_panel(
                        chat_id=chat_id,
                        message_id=sent_video.message_id,
                        owner_id=user_id,
                        owner_name=str(ctx.get("user_name", "کاربر")),
                        reply_to_message_id=reply_to_id,
                        kind=MediaKind.VIDEO.value,
                        file_id=sent_video.video.file_id,
                        file_size=sent_video.video.file_size or 0,
                    )

            elif action == "audio":

                async def _do_audio() -> Path:
                    return await download_audio(
                        url,
                        work_dir,
                        max_bytes=settings.upload_limit,
                        cookie_file=cookie_file_str,
                    )

                downloaded_file = await run_media_job(settings, _do_audio, stage="download_audio")
                file_size = downloaded_file.stat().st_size

                if file_size > settings.upload_limit:
                    limit_str = human_bytes(settings.upload_limit)
                    size_str = human_bytes(file_size)
                    err_msg = (
                        f"{registry.text('error')} حجم فایل صوتی ({size_str}) "
                        f"بیشتر از سقف مجاز ({limit_str}) است."
                    )
                    await _notify_error(
                        status_msg,
                        err_msg,
                        bot=bot,
                        chat_id=chat_id,
                        fallback_msg=fallback_msg,
                    )
                    return

                uploader = ctx.get("uploader") or "Mewvia"
                dur_secs = ctx.get("duration_seconds")
                dur_int = (
                    int(dur_secs) if isinstance(dur_secs, (int, float)) and dur_secs > 0 else None
                )

                # Fetch and embed lyrics if available
                lyrics = await fetch_lyrics(title, str(uploader), duration_seconds=dur_int)
                if lyrics:
                    embed_lyrics_to_mp3(downloaded_file, lyrics)

                audio_caption_lines = []
                if title:
                    audio_caption_lines.append(
                        f"{registry.text('audio')} <b>{html.quote(title)}</b>"
                    )
                if uploader and uploader not in ("Mewvia", "Unknown"):
                    audio_caption_lines.append(
                        f"{registry.text('user')} <b>سازنده:</b> {html.quote(str(uploader))}"
                    )
                audio_caption_lines.append(
                    f"دانلود شده با @{settings.bot_username} {registry.text('paw')}"
                )
                caption = "\n\n".join(audio_caption_lines)

                audio_kb = build_audio_actions_kb(registry)
                sent_audio = await bot.send_audio(
                    chat_id=chat_id,
                    audio=FSInputFile(downloaded_file),
                    title=title[:64],
                    performer=str(uploader)[:64],
                    caption=caption[:1024],
                    reply_to_message_id=reply_to_id,
                    reply_markup=audio_kb.as_markup(),
                )

                if sent_audio and sent_audio.audio:
                    await store_panel(
                        chat_id=chat_id,
                        message_id=sent_audio.message_id,
                        owner_id=user_id,
                        owner_name=str(ctx.get("user_name", "کاربر")),
                        reply_to_message_id=reply_to_id,
                        kind=MediaKind.AUDIO.value,
                        file_id=sent_audio.audio.file_id,
                        file_size=sent_audio.audio.file_size or 0,
                    )

                # Send lyrics in follow-up reply message if available
                if lyrics and sent_audio:
                    lyrics_msg = build_full_lyrics_message(title, str(uploader), lyrics, registry)
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=lyrics_msg,
                            reply_to_message_id=sent_audio.message_id,
                        )
                    except Exception as l_exc:
                        log.warning("send_lyrics_reply_failed", error=str(l_exc))

            elif action == "gallery":

                async def _do_gallery() -> list[Path]:
                    return await download_gallery(
                        url,
                        work_dir,
                        cookie_file=cookie_file_str,
                    )

                gallery_files = await run_media_job(settings, _do_gallery, stage="download_gallery")
                if not gallery_files:
                    raise ConversionError("هیچ عکسی برای دانلود یافت نشد.")

                file_size = sum(f.stat().st_size for f in gallery_files)
                valid_gallery = [
                    f for f in gallery_files if f.stat().st_size <= settings.upload_limit
                ]
                if not valid_gallery:
                    limit_str = human_bytes(settings.upload_limit)
                    raise ConversionError(f"حجم فایل‌های گالری بیش از حد مجاز ({limit_str}) است.")
                gallery_files = valid_gallery
                gallery_caption_lines = []
                if title:
                    gallery_caption_lines.append(
                        f"{registry.text('photo')} <b>{html.quote(title)}</b>"
                    )
                gallery_caption_lines.append(
                    f"دانلود شده با @{settings.bot_username} {registry.text('paw')}"
                )
                gallery_caption = "\n\n".join(gallery_caption_lines)

                if len(gallery_files) == 1:
                    single_file = gallery_files[0]
                    if _is_video_file(single_file):
                        v_w = None
                        v_h = None
                        v_dur = None
                        try:
                            v_info = await probe(single_file)
                            v_w = v_info.width if v_info.width > 0 else None
                            v_h = v_info.height if v_info.height > 0 else None
                            v_dur = int(v_info.duration) if v_info.duration > 0 else None
                        except Exception:
                            pass

                        video_kb = build_video_actions_kb(registry)
                        sent_v = await bot.send_video(
                            chat_id=chat_id,
                            video=FSInputFile(single_file),
                            caption=gallery_caption[:1024],
                            width=v_w,
                            height=v_h,
                            duration=v_dur,
                            supports_streaming=True,
                            reply_to_message_id=reply_to_id,
                            reply_markup=video_kb.as_markup(),
                        )
                        if sent_v and sent_v.video:
                            await store_panel(
                                chat_id=chat_id,
                                message_id=sent_v.message_id,
                                owner_id=user_id,
                                owner_name=str(ctx.get("user_name", "کاربر")),
                                reply_to_message_id=reply_to_id,
                                kind=MediaKind.VIDEO.value,
                                file_id=sent_v.video.file_id,
                                file_size=sent_v.video.file_size or 0,
                            )
                    else:
                        photo_kb = build_photo_actions_kb(registry)
                        sent_p = await bot.send_photo(
                            chat_id=chat_id,
                            photo=FSInputFile(single_file),
                            caption=gallery_caption[:1024],
                            reply_to_message_id=reply_to_id,
                            reply_markup=photo_kb.as_markup(),
                        )
                        if sent_p and sent_p.photo:
                            await store_panel(
                                chat_id=chat_id,
                                message_id=sent_p.message_id,
                                owner_id=user_id,
                                owner_name=str(ctx.get("user_name", "کاربر")),
                                reply_to_message_id=reply_to_id,
                                kind=MediaKind.PHOTO.value,
                                file_id=sent_p.photo[-1].file_id,
                                file_size=sent_p.photo[-1].file_size or 0,
                            )
                else:
                    for chunk_idx in range(0, len(gallery_files), 10):
                        chunk = gallery_files[chunk_idx : chunk_idx + 10]
                        media_group: list[Any] = []
                        for i, p in enumerate(chunk):
                            cap = gallery_caption[:1024] if chunk_idx == 0 and i == 0 else None
                            if _is_video_file(p):
                                v_w = None
                                v_h = None
                                v_dur = None
                                try:
                                    v_info = await probe(p)
                                    v_w = v_info.width if v_info.width > 0 else None
                                    v_h = v_info.height if v_info.height > 0 else None
                                    v_dur = int(v_info.duration) if v_info.duration > 0 else None
                                except Exception:
                                    pass
                                media_group.append(
                                    InputMediaVideo(
                                        media=FSInputFile(p),
                                        caption=cap,
                                        width=v_w,
                                        height=v_h,
                                        duration=v_dur,
                                        supports_streaming=True,
                                    )
                                )
                            else:
                                media_group.append(
                                    InputMediaPhoto(media=FSInputFile(p), caption=cap)
                                )
                        if len(media_group) == 1:
                            single_item = chunk[0]
                            if _is_video_file(single_item):
                                v_w = None
                                v_h = None
                                v_dur = None
                                try:
                                    v_info = await probe(single_item)
                                    v_w = v_info.width if v_info.width > 0 else None
                                    v_h = v_info.height if v_info.height > 0 else None
                                    v_dur = int(v_info.duration) if v_info.duration > 0 else None
                                except Exception:
                                    pass
                                await bot.send_video(
                                    chat_id=chat_id,
                                    video=FSInputFile(single_item),
                                    width=v_w,
                                    height=v_h,
                                    duration=v_dur,
                                    supports_streaming=True,
                                    reply_to_message_id=reply_to_id,
                                )
                            else:
                                await bot.send_photo(
                                    chat_id=chat_id,
                                    photo=FSInputFile(single_item),
                                    reply_to_message_id=reply_to_id,
                                )
                        else:
                            await bot.send_media_group(
                                chat_id=chat_id,
                                media=media_group,
                                reply_to_message_id=reply_to_id,
                            )
                        await asyncio.sleep(0.3)

            elif action == "spotify_track":
                uploader = ctx.get("uploader") or "Spotify"
                thumb_url = ctx.get("thumbnail_url")

                async def _do_spotify() -> Path:
                    return await download_spotify_track(
                        title=title,
                        artist=str(uploader),
                        thumbnail_url=thumb_url,
                        work_dir=work_dir,
                        max_bytes=settings.upload_limit,
                    )

                downloaded_file = await run_media_job(
                    settings, _do_spotify, stage="download_spotify"
                )
                file_size = downloaded_file.stat().st_size

                # Fetch and embed lyrics if available
                lyrics = await fetch_lyrics(title, str(uploader))
                if lyrics:
                    embed_lyrics_to_mp3(downloaded_file, lyrics)

                caption = (
                    f"{registry.text('spotify')} <b>{html.quote(title)}</b>\n"
                    f"{registry.text('user')} <b>خواننده:</b> {html.quote(str(uploader))}\n\n"
                    f"دانلود شده با @{settings.bot_username} {registry.text('paw')}"
                )
                audio_kb = build_audio_actions_kb(registry)
                sent_audio = await bot.send_audio(
                    chat_id=chat_id,
                    audio=FSInputFile(downloaded_file),
                    title=title[:64],
                    performer=str(uploader)[:64],
                    caption=caption[:1024],
                    reply_to_message_id=reply_to_id,
                    reply_markup=audio_kb.as_markup(),
                )

                if sent_audio and sent_audio.audio:
                    await store_panel(
                        chat_id=chat_id,
                        message_id=sent_audio.message_id,
                        owner_id=user_id,
                        owner_name=str(ctx.get("user_name", "کاربر")),
                        reply_to_message_id=reply_to_id,
                        kind=MediaKind.AUDIO.value,
                        file_id=sent_audio.audio.file_id,
                        file_size=sent_audio.audio.file_size or 0,
                    )

                # Send lyrics in follow-up reply message if available
                if lyrics and sent_audio:
                    lyrics_msg = build_full_lyrics_message(title, str(uploader), lyrics, registry)
                    try:
                        await bot.send_message(
                            chat_id=chat_id,
                            text=lyrics_msg,
                            reply_to_message_id=sent_audio.message_id,
                        )
                    except Exception as l_exc:
                        log.warning("send_lyrics_reply_failed", error=str(l_exc))

            elif action in ("pl_zip", "pl_audio_all", "pl_audio_first5", "pl_video_all"):
                items = list(ctx.get("playlist_items") or [])
                if not items:
                    raise ConversionError("ترکی در این پلی‌لیست برای دانلود یافت نشد.")

                is_zip = action == "pl_zip"
                items_to_download = items[:5] if action == "pl_audio_first5" else items[:100]
                total_items = len(items_to_download)
                sent_count = 0
                total_bytes = 0

                tracks_dir = work_dir / "tracks"
                tracks_dir.mkdir(parents=True, exist_ok=True)
                clean_title = re.sub(r'[\\/*?:"<>|]', "", title)[:30].strip() or "album"
                zip_path = work_dir / f"{clean_title}.zip"

                # Concurrent worker semaphore (6 parallel workers for blazing throughput)
                sem = asyncio.Semaphore(6)

                async def _worker(
                    idx: int, it: dict[str, Any]
                ) -> tuple[int, dict[str, Any], Path | None]:
                    it_title = str(it.get("title") or f"Track {idx}")
                    it_artist = str(it.get("artist") or it.get("uploader") or "")
                    it_url = str(it.get("url") or "")
                    track_dir = tracks_dir / f"t_{idx}"
                    track_dir.mkdir(parents=True, exist_ok=True)

                    async with sem:
                        try:
                            item_timeout = min(60, settings.job_timeout)
                            if platform_key == "spotify":
                                p = await asyncio.wait_for(
                                    download_spotify_track(
                                        title=it_title,
                                        artist=it_artist,
                                        thumbnail_url=ctx.get("thumbnail_url"),
                                        work_dir=track_dir,
                                        max_bytes=settings.upload_limit,
                                    ),
                                    timeout=item_timeout,
                                )
                                return idx, it, p
                            elif action == "pl_video_all":
                                if it_url:
                                    p = await asyncio.wait_for(
                                        download_video(
                                            it_url,
                                            track_dir,
                                            max_bytes=settings.upload_limit,
                                        ),
                                        timeout=item_timeout,
                                    )
                                    return idx, it, p
                            else:
                                if it_url:
                                    try:
                                        p = await asyncio.wait_for(
                                            download_audio(
                                                it_url,
                                                track_dir,
                                                max_bytes=settings.upload_limit,
                                            ),
                                            timeout=item_timeout,
                                        )
                                    except Exception:
                                        p = await asyncio.wait_for(
                                            download_video(
                                                it_url,
                                                track_dir,
                                                max_bytes=settings.upload_limit,
                                            ),
                                            timeout=item_timeout,
                                        )
                                    return idx, it, p
                        except Exception as exc:
                            log.warning(
                                "parallel_track_dl_failed", idx=idx, title=it_title, error=str(exc)
                            )
                            return idx, it, None
                    return idx, it, None

                tasks = [
                    asyncio.create_task(_worker(idx, it))
                    for idx, it in enumerate(items_to_download, 1)
                ]

                last_edit_time = 0.0
                # Stream results as workers finish
                for fut in asyncio.as_completed(tasks):
                    idx, it, file_path = await fut
                    it_title = str(it.get("title") or f"Track {idx}")
                    it_artist = str(it.get("artist") or it.get("uploader") or "")

                    if file_path and file_path.is_file():
                        f_size = file_path.stat().st_size
                        total_bytes += f_size
                        sent_count += 1

                        if is_zip:
                            arcname = f"{idx:02d} - {it_artist} - {it_title}{file_path.suffix}"
                            await asyncio.to_thread(_add_to_zip, zip_path, file_path, arcname)
                        else:
                            try:
                                if action == "pl_video_all":
                                    caption_v = (
                                        f"{registry.text('video')} <b>{html.quote(it_title)}</b>"
                                    )
                                    v_w = None
                                    v_h = None
                                    v_dur = None
                                    try:
                                        v_info = await probe(file_path)
                                        v_w = v_info.width if v_info.width > 0 else None
                                        v_h = v_info.height if v_info.height > 0 else None
                                        v_dur = (
                                            int(v_info.duration) if v_info.duration > 0 else None
                                        )
                                    except Exception:
                                        pass
                                    await bot.send_video(
                                        chat_id=chat_id,
                                        video=FSInputFile(file_path),
                                        caption=caption_v,
                                        width=v_w,
                                        height=v_h,
                                        duration=v_dur,
                                        supports_streaming=True,
                                        reply_to_message_id=reply_to_id,
                                    )
                                else:
                                    await bot.send_audio(
                                        chat_id=chat_id,
                                        audio=FSInputFile(file_path),
                                        title=it_title[:64],
                                        performer=it_artist[:64],
                                        reply_to_message_id=reply_to_id,
                                    )
                                await asyncio.sleep(0.4)
                            except Exception as send_exc:
                                log.warning(
                                    "track_send_failed", title=it_title, error=str(send_exc)
                                )

                    now = time.perf_counter()
                    if status_msg is not None and (
                        now - last_edit_time >= 1.5 or sent_count == total_items
                    ):
                        last_edit_time = now
                        cur_fa = fa_digits(str(sent_count))
                        tot_fa = fa_digits(str(total_items))
                        real_pct = int((sent_count / total_items) * 100)

                        header_text = (
                            f"📥 <b>در حال دانلود آلبوم</b> • <code>{cur_fa}/{tot_fa}</code>"
                        )
                        track_text = f"🎵 <b>{html.quote(it_title)}</b>"
                        artist_text = f"👤 <i>{html.quote(it_artist)}</i>" if it_artist else None

                        try:
                            if isinstance(status_msg, ProgressController):
                                await status_msg.update_stage(
                                    stage_title=header_text,
                                    percent=real_pct,
                                    sub_title=track_text,
                                    extra_line=artist_text,
                                    detail="در حال آماده‌سازی و ارسال…",
                                    force=True,
                                )
                            else:
                                text_lines = [header_text, track_text]
                                if artist_text:
                                    text_lines.append(artist_text)
                                text_lines.append(f"\n<code>{render_progress_bar(real_pct)}</code>")
                                await status_msg.edit_text("\n".join(text_lines))
                        except Exception:
                            pass

                if sent_count == 0:
                    if status_msg is not None:
                        err_pl_text = (
                            f"{registry.text('error')} "
                            "<b>امکان دریافت ترک‌های این پلی‌لیست وجود نداشت.</b>\n"
                            "ممکن است قطعات دارای محدودیت یا در این منطقه مسدود باشند."
                        )
                        await status_msg.edit_text(err_pl_text)
                        status_msg = None
                    return

                if is_zip and zip_path.is_file() and zip_path.stat().st_size > 0:
                    zip_size = zip_path.stat().st_size
                    total_bytes = zip_size
                    if zip_size > settings.upload_limit:
                        limit_str = human_bytes(settings.upload_limit)
                        raise ConversionError(
                            "❌ حجم فایل ZIP آلبوم "
                            f"({human_bytes(zip_size)}) بیشتر از سقف مجاز ({limit_str}) است.\n"
                            "لطفاً گزینه «ارسال دونه‌دونه ترک‌ها (MP3)» را انتخاب کنید."
                        )
                    caption = (
                        f"{registry.text('zip')} <b>آلبوم کامل: {html.quote(title)}</b>\n\n"
                        f"{registry.text('bullet')} تعداد: "
                        f"<code>{fa_digits(str(sent_count))}</code> ترک\n"
                        f"{registry.text('file')} حجم: <code>{human_bytes(zip_size)}</code>\n\n"
                        f"دانلود شده با @{settings.bot_username} {registry.text('paw')}"
                    )
                    await bot.send_document(
                        chat_id=chat_id,
                        document=FSInputFile(zip_path, filename=f"{clean_title}.zip"),
                        caption=caption[:1024],
                        reply_to_message_id=reply_to_id,
                    )

                file_size = total_bytes
                if status_msg is not None:
                    done_text = (
                        f"{registry.text('ok')} <b>دانلود آلبوم / پلی‌لیست به پایان رسید!</b>\n\n"
                        f"{registry.text('pack')} "
                        f"<code>{fa_digits(str(sent_count))}</code> ترک با موفقیت آماده شد."
                    )
                    await status_msg.edit_text(done_text)
                    status_msg = None

            else:
                return

            if cookie_record:
                await repo_cookie.record_usage(cookie_record.id, success=True)

            # Clean up status message
            try:
                if status_msg is not None:
                    await status_msg.delete()
            except Exception:
                pass

            elapsed_ms = int((time.perf_counter() - start_time) * 1000)
            await ConversionRepo(session).log_conversion(
                telegram_id=user_id,
                source_kind="url",
                target_id=f"dl_{platform_key}",
                in_bytes=file_size,
                out_bytes=file_size,
                duration_ms=elapsed_ms,
                status="success",
            )
            log.info("downloader_success", platform=platform_key, user_id=user_id, size=file_size)

        except MewviaError as exc:
            if cookie_record:
                await repo_cookie.record_usage(cookie_record.id, success=False, error=str(exc))
                from app.admin.cookies_router import sync_active_cookie_to_disk

                await sync_active_cookie_to_disk(session)
            err = str(exc) if str(exc) and str(exc) != exc.code else error_text(exc.code)
            await _notify_error(
                status_msg,
                err,
                bot=bot,
                chat_id=chat_id,
                fallback_msg=fallback_msg,
            )
        except Exception as exc:
            if cookie_record:
                await repo_cookie.record_usage(cookie_record.id, success=False, error=str(exc))
                from app.admin.cookies_router import sync_active_cookie_to_disk

                await sync_active_cookie_to_disk(session)
            log.error("downloader_error", url=url, error=str(exc))
            err = "❌ خطا در دانلود رسانه. ممکن است لینک منقضی یا نامعتبر باشد."
            await _notify_error(
                status_msg,
                err,
                bot=bot,
                chat_id=chat_id,
                fallback_msg=fallback_msg,
            )


async def _handle_url_input(
    message: Message,
    url: str,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """Analyze the given URL and present options for YouTube/Playlists or auto-download directly."""
    registry = await ready_registry()
    status = await message.reply("⏳ <b>در حال دریافت اطلاعات لینک…</b>")
    status_msg: Message | None = status if isinstance(status, Message) else None

    cookie_file_str: str | None = None
    platform_key, _, _ = detect_platform(url)
    if platform_key == "instagram":
        repo_cookie = InstagramCookieRepo(session)
        cookie_rec = await repo_cookie.get_next_cookie()
        if (
            cookie_rec
            and cookie_rec.cookie_text
            and cookie_rec.is_active
            and cookie_rec.status != "invalid"
        ):
            disk_cookie = settings.cookie_dir / "instagram.txt"
            if disk_cookie.is_file() and disk_cookie.stat().st_size > 0:
                cookie_file_str = str(disk_cookie)

    try:
        info = await probe_media_url(url, cookie_file=cookie_file_str)
    except MewviaError as exc:
        err = str(exc) if str(exc) and str(exc) != exc.code else error_text(exc.code)
        await _notify_error(status_msg, err, bot=bot, chat_id=message.chat.id, fallback_msg=message)
        return
    except Exception as exc:
        log.warning("downloader_probe_error", url=url, error=str(exc))
        err = (
            "❌ نتوانستم اطلاعات این لینک را دریافت کنم.\n"
            "ممکن است لینک خصوصی باشد، یا این سایت پشتیبانی نشود."
        )
        await _notify_error(status_msg, err, bot=bot, chat_id=message.chat.id, fallback_msg=message)
        return

    user_id = message.from_user.id if message.from_user else 0

    # User preference: Only YouTube videos and all Playlists/Albums show selection cards.
    # All other single media (Instagram, TikTok, Twitter, Spotify, etc.) auto-download at once!
    is_interactive = info.is_playlist or (info.platform_key == "youtube")

    if is_interactive:
        token = create_token()
        await store_download_context(
            token,
            {
                "url": url,
                "platform_key": info.platform_key,
                "platform_name": info.platform_name,
                "title": info.title,
                "uploader": info.uploader,
                "thumbnail_url": info.thumbnail_url,
                "duration_str": info.duration_str,
                "reply_to_id": message.message_id,
                "user_id": user_id,
                "chat_id": message.chat.id,
                "user_name": message.from_user.first_name if message.from_user else "کاربر",
                "is_playlist": info.is_playlist,
                "playlist_count": info.playlist_count,
                "playlist_items": list(info.playlist_items),
            },
        )

        kb = KB(registry)

        if info.is_playlist:
            count_fa = fa_digits(str(info.playlist_count))
            kb.row(
                inline_button(
                    f"دانلود یکجا (فایل ZIP) [{count_fa} ترک]",
                    callback_data=DownloaderCB(action="pl_zip", token=token).pack(),
                    style=Style.CONFIRM,
                    emoji_slot="zip",
                    registry=registry,
                )
            )
            kb.row(
                inline_button(
                    "ارسال دونه‌دونه ترک‌ها (MP3)",
                    callback_data=DownloaderCB(action="pl_audio_all", token=token).pack(),
                    style=Style.PRIMARY,
                    emoji_slot="audio",
                    registry=registry,
                )
            )
            if info.platform_key != "spotify":
                kb.row(
                    inline_button(
                        f"دانلود تمام ویدیوها ({count_fa} عدد)",
                        callback_data=DownloaderCB(action="pl_video_all", token=token).pack(),
                        style=Style.NEUTRAL,
                        emoji_slot="video",
                        registry=registry,
                    )
                )
            if info.playlist_count > 5:
                kb.row(
                    inline_button(
                        "دانلود ۵ مورد اول (سریع)",
                        callback_data=DownloaderCB(action="pl_audio_first5", token=token).pack(),
                        style=Style.NEUTRAL,
                        emoji_slot="lightning",
                        registry=registry,
                    )
                )
        else:
            # YouTube single video
            kb.row(
                inline_button(
                    "بهترین کیفیت",
                    callback_data=DownloaderCB(action="video", token=token).pack(),
                    style=Style.PRIMARY,
                    emoji_slot="video",
                    registry=registry,
                ),
                inline_button(
                    "کیفیت 720p HD",
                    callback_data=DownloaderCB(action="video_720", token=token).pack(),
                    style=Style.PRIMARY,
                    emoji_slot="quality_hd",
                    registry=registry,
                ),
            )
            kb.row(
                inline_button(
                    "کیفیت 480p (حجم کم)",
                    callback_data=DownloaderCB(action="video_480", token=token).pack(),
                    style=Style.NEUTRAL,
                    emoji_slot="quality_sd",
                    registry=registry,
                ),
                inline_button(
                    "استخراج صدا (MP3)",
                    callback_data=DownloaderCB(action="audio", token=token).pack(),
                    style=Style.CONFIRM,
                    emoji_slot="audio",
                    registry=registry,
                ),
            )

        kb.row(
            inline_button(
                t("common.cancel"),
                callback_data=DownloaderCB(action="cancel", token=token).pack(),
                style=Style.DESTRUCTIVE,
                emoji_slot="error",
                registry=registry,
            )
        )

        uploader_line = (
            f"{registry.text('user')} <b>سازنده:</b> {html.quote(info.uploader)}\n"
            if info.uploader
            else ""
        )
        duration_line = (
            f"{registry.text('clock')} <b>مدت زمان:</b> "
            f"<code>{fa_digits(info.duration_str)}</code>\n"
            if info.duration_seconds
            else ""
        )

        title_line = (
            f"{registry.text('text')} <b>عنوان:</b> {html.quote(info.title)}\n"
            if info.title
            else ""
        )

        if info.is_playlist:
            type_str = "پلی‌لیست / آلبوم"
            count_str = fa_digits(str(info.playlist_count))
            count_line = (
                f"{registry.text('bullet')} <b>تعداد ترک‌ها:</b> <code>{count_str}</code> عدد\n"
            )
            p_slot = (
                info.platform_key
                if info.platform_key in ("youtube", "spotify", "soundcloud", "instagram")
                else "download"
            )
            card_text = (
                f"{registry.text(p_slot)} <b>دانلود {type_str} از {info.platform_name}</b>\n\n"
                f"{title_line}"
                f"{uploader_line}"
                f"{count_line}\n"
                "نحوه دانلود را انتخاب کن:"
            )
        else:
            card_text = (
                f"{registry.text('youtube')} <b>دانلود از یوتیوب</b>\n\n"
                f"{title_line}"
                f"{uploader_line}"
                f"{duration_line}\n"
                "کیفیت یا فرمت مورد نظرت را انتخاب کن:"
            )

        sent_card: Message | None = None
        if status_msg:
            edited = await status_msg.edit_text(card_text, reply_markup=kb.as_markup())
            sent_card = edited if isinstance(edited, Message) else status_msg
        else:
            sent_card = await message.reply(card_text, reply_markup=kb.as_markup())

        # In groups, register ownership so only sender can tap quality / action buttons!
        if message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP) and sent_card is not None:
            from app.middlewares.group_guard import store_panel

            user_name = message.from_user.first_name if message.from_user else "کاربر"
            await store_panel(
                message.chat.id,
                sent_card.message_id,
                owner_id=user_id,
                owner_name=user_name,
                reply_to_message_id=message.message_id,
            )
        return

    # Direct auto-download without questioning for all other platforms
    user_id = message.from_user.id if message.from_user else 0
    if not await check_daily_quota(session, user_id, message):
        return

    if info.platform_key == "spotify":
        action = "spotify_track"
    elif info.is_gallery:
        action = "gallery"
    elif info.is_audio_only:
        action = "audio"
    else:
        action = "video"

    progress: ProgressController | Message | None = None
    try:
        progress = await ProgressController.start(
            bot=bot,
            chat_id=message.chat.id,
            reply_to_id=message.message_id,
            initial_stage=f"در حال دانلود از {info.platform_name}…",
            session=session,
            replace_msg=status_msg,
        )
    except Exception as p_exc:
        log.warning("progress_controller_start_failed", error=str(p_exc))
        progress = status_msg

    ctx: dict[str, Any] = {
        "url": url,
        "platform_key": info.platform_key,
        "platform_name": info.platform_name,
        "title": info.title,
        "uploader": info.uploader,
        "thumbnail_url": info.thumbnail_url,
        "duration_str": info.duration_str,
        "reply_to_id": message.message_id,
        "is_playlist": False,
        "playlist_count": 0,
        "playlist_items": [],
        "user_name": message.from_user.first_name if message.from_user else "کاربر",
    }

    await _perform_download(
        chat_id=message.chat.id,
        user_id=user_id,
        reply_to_id=message.message_id,
        action=action,
        ctx=ctx,
        status_msg=progress,
        bot=bot,
        session=session,
        settings=settings,
        fallback_msg=message,
    )


@router.message(Command("dl"))
async def cmd_download_url(
    message: Message,
    command: CommandObject,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """Download media from command: `/dl <url>`."""
    arg_url = (command.args or "").strip()
    url = extract_first_url(arg_url)
    if not url:
        await message.reply(
            "لطفاً لینک مورد نظرت را به این شکل بفرست:\n"
            "<code>/dl https://instagram.com/reel/...</code>"
        )
        return
    await _handle_url_input(message, url, bot, session, settings)


@router.message(Command("cookie", "cookies"), IsAdmin())
async def cmd_cookie_info(message: Message, session: AsyncSession) -> None:
    """Show Instagram cookies management dashboard for admins."""
    from app.admin.cookies_router import ready_registry, render_cookies_dashboard

    registry = await ready_registry()
    text, kb = await render_cookies_dashboard(session, registry)
    await message.reply(text, reply_markup=kb.as_markup())


def _is_cookie_file_upload(message: Message) -> bool:
    if (
        message.animation
        or message.video
        or message.photo
        or message.sticker
        or message.video_note
        or message.audio
        or message.voice
    ):
        return False
    doc = message.document
    if not doc or not doc.file_name:
        return False
    file_name = doc.file_name.lower()
    valid_keywords = ("cookie", "insta", "instagram", "youtube", "yt")
    return file_name.endswith((".txt", ".json")) and any(k in file_name for k in valid_keywords)


@router.message(
    F.chat.type == "private",
    IsAdmin(),
    _is_cookie_file_upload,
)
async def upload_cookie_file(message: Message, bot: Bot, session: AsyncSession) -> None:
    """Allow admins to upload cookies.txt directly to add to the account pool."""
    doc = message.document
    if not doc or not doc.file_name:
        return
    file_name = doc.file_name.lower()
    valid_keywords = ("cookie", "insta", "instagram", "youtube", "yt")
    if not (file_name.endswith(".txt") and any(k in file_name for k in valid_keywords)):
        return

    file_info = await bot.get_file(doc.file_id)
    if not file_info.file_path:
        await message.reply("❌ خطا در دریافت فایل از تلگرام.")
        return

    buf = io.BytesIO()
    await bot.download_file(file_info.file_path, destination=buf)
    raw_text = buf.getvalue().decode("utf-8", errors="ignore")

    from app.admin.cookies_router import (
        ready_registry,
        render_cookies_dashboard,
        sync_active_cookie_to_disk,
    )
    from app.media.cookie_checker import (
        check_instagram_cookie_health_sync,
        parse_and_normalize_cookies,
    )

    netscape_text, cookie_dict = parse_and_normalize_cookies(raw_text)
    account_id = cookie_dict.get("ds_user_id")
    repo = InstagramCookieRepo(session)
    count = await repo.count()
    label = f"اکانت {count + 1}" if not account_id else f"اکانت {account_id[:8]}"

    is_valid, _, err = await asyncio.to_thread(check_instagram_cookie_health_sync, cookie_dict)
    status_str = "valid" if is_valid else "invalid"

    cookie = await repo.add(
        label=label,
        cookie_text=netscape_text,
        account_id=account_id,
        status=status_str,
    )
    if not is_valid:
        cookie.error_message = err

    await sync_active_cookie_to_disk(session)
    await session.flush()

    registry = await ready_registry()
    _, kb = await render_cookies_dashboard(session, registry)
    status_badge = "🟢 سالم و فعال در چرخه" if is_valid else f"⚠️ نیاز به بررسی ({err})"
    await message.reply(
        f"🍪 <b>اکانت «{html.quote(label)}» با موفقیت اضافه شد!</b>\n\n"
        f"وضعیت اولیه: {status_badge}\n"
        "این کوکی در دیتابیس ثبت شد و به صورت خودکار برای دانلودهای اینستاگرام استفاده می‌شود.",
        reply_markup=kb.as_markup(),
    )


class ContainsMediaUrlFilter(BaseFilter):
    """Filter that matches media URLs. In groups, strictly matches only supported platforms."""

    async def __call__(self, message: Message) -> bool | dict[str, Any]:
        text = (message.text or "").strip()
        if not text or text.startswith("/"):
            return False
        url = extract_first_url(text)
        if not url:
            return False

        if not is_safe_remote_url(url):
            return False

        is_group = message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)
        # In groups/supergroups, ONLY react to supported downloader platforms.
        # Regular chat messages containing non-media links are ignored. In
        # private chat any public link is accepted (yt-dlp generic extractor).
        if is_group and not is_supported_media_url(url):
            return False

        return {"extracted_url": url}


@router.message(
    StateFilter(None),
    ContainsMediaUrlFilter(),
)
async def auto_detect_url_message(
    message: Message,
    extracted_url: str,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
) -> None:
    """Automatically catch URLs sent as private chat messages in idle state."""
    await _handle_url_input(message, extracted_url, bot, session, settings)


@router.callback_query(DownloaderCB.filter(F.action == "cancel"))
async def cancel_download(query: CallbackQuery, callback_data: DownloaderCB) -> None:
    if not isinstance(query.message, Message):
        await query.answer(t("common.stale_button"), show_alert=True)
        return

    ctx = await get_download_context(callback_data.token)
    user_id = query.from_user.id if query.from_user else 0
    if ctx and _context_owner_mismatch(ctx, user_id, query.message.chat.id):
        await query.answer(
            "این دکمه برای درخواست شما ثبت نشده است.", show_alert=True
        )
        return

    if not await _edit_status(query.message, "دانلود لغو شد."):
        try:
            await query.message.answer("دانلود لغو شد.")
        except Exception as exc:
            log.debug("cancel_reply_failed", error=str(exc))
    await query.answer()


@router.callback_query(DownloaderCB.filter())
async def execute_download(
    query: CallbackQuery,
    callback_data: DownloaderCB,
    bot: Bot,
    session: AsyncSession,
    settings: Settings,
) -> None:
    action = callback_data.action
    token = callback_data.token

    if not isinstance(query.message, Message):
        await query.answer(t("common.stale_button"), show_alert=True)
        return

    msg = query.message
    ctx = await get_download_context(token)
    if not ctx:
        await query.answer(
            "این درخواست منقضی شده است. لطفاً لینک را دوباره بفرستید.", show_alert=True
        )
        return

    user_id = query.from_user.id if query.from_user else 0
    if _context_owner_mismatch(ctx, user_id, msg.chat.id):
        # A forwarded / shared selection card must not let another account
        # spend the original requester's download (or trigger it at all).
        await query.answer("این دکمه برای درخواست شما ثبت نشده است.", show_alert=True)
        return

    if not await check_daily_quota(session, user_id, query):
        return

    await query.answer()
    is_pl = action.startswith("pl_")
    stage_text = "در حال آماده‌سازی و دانلود آلبوم…" if is_pl else "در حال دانلود رسانه از سرور…"
    progress: ProgressController | Message | None = None
    try:
        progress = await ProgressController.start(
            bot=bot,
            chat_id=msg.chat.id,
            reply_to_id=ctx.get("reply_to_id"),
            initial_stage=stage_text,
            session=session,
            replace_msg=msg,
            auto_tick=not is_pl,
        )
    except Exception as p_exc:
        log.warning("progress_controller_start_failed", error=str(p_exc))
        progress = (
            msg
            if await _edit_status(msg, "⏳ <b>در حال دانلود رسانه از سرور…</b>")
            else None
        )

    await _perform_download(
        chat_id=msg.chat.id,
        user_id=user_id,
        reply_to_id=ctx.get("reply_to_id"),
        action=action,
        ctx=ctx,
        status_msg=progress,
        bot=bot,
        session=session,
        settings=settings,
        fallback_msg=msg,
    )


feature = register(
    Feature(
        name="downloader",
        router=router,
        order=90,
    )
)
