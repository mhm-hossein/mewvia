"""Bot and Dispatcher construction.

Kept separate from `__main__` so tests can build a dispatcher without starting
polling, and so the session choice (public vs local Bot API) lives in one place.
"""

from __future__ import annotations

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.base import BaseStorage
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import ErrorEvent
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import Settings
from app.core.logging import get_logger
from app.features import load_features
from app.media.telegram import MediaBot
from app.middlewares import setup_middlewares
from app.middlewares.admission import MediaAdmission
from app.ui.premium_guard import PremiumEmojiGuard

log = get_logger(__name__)


def build_bot(settings: Settings) -> Bot:
    """Create the Bot, pointing at the local API server when configured."""
    session: AiohttpSession | None = None
    if settings.uses_local_api:
        base = settings.telegram_api_url.rstrip("/")
        session = AiohttpSession(
            api=TelegramAPIServer(
                base=f"{base}/bot{{token}}/{{method}}",
                file=f"{base}/file/bot{{token}}/{{path}}",
                is_local=True,
            ),
            timeout=300,
        )
        log.info("using_local_bot_api", base=base)
    else:
        session = AiohttpSession(timeout=300)

    bot = MediaBot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    # Retries any request Telegram refuses because of a custom emoji, with the
    # premium emoji stripped out. Registered on the session so every send in the
    # application is covered without per-call-site guards.
    bot.session.middleware(PremiumEmojiGuard())
    return bot


def build_storage(settings: Settings) -> BaseStorage:
    """Redis-backed FSM so state survives restarts; memory as a fallback."""
    if settings.redis_url:
        return RedisStorage.from_url(
            settings.redis_url,
            connection_kwargs={"max_connections": 50},
            state_ttl=86400,
            data_ttl=86400,
        )
    log.warning("redis_url_missing_using_memory_storage")
    return MemoryStorage()


async def _global_error_handler(event: ErrorEvent) -> bool:
    """Fallback error catcher for unhandled exceptions across the application."""
    exc = event.exception
    if isinstance(exc, TelegramAPIError):
        # Let Telegram API errors bubble up so session middlewares and tests can handle them
        raise exc

    log.error("unhandled_dispatcher_error", error=str(exc), exc_info=exc)
    update = event.update
    if update.callback_query:
        try:
            await update.callback_query.answer(
                "⚠️ متأسفانه خطایی در پردازش رخ داد. لطفاً دوباره تلاش کنید.",
                show_alert=True,
            )
        except Exception:
            pass
    elif update.message:
        # In groups/supergroups, stay silent on background errors to avoid spamming the group
        if update.message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP):
            return True
        try:
            await update.message.reply(
                "⚠️ متأسفانه در انجام این درخواست خطایی رخ داد. لطفاً چند لحظه بعد مجدداً تلاش کنید."
            )
        except Exception:
            pass
    return True


def build_dispatcher(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
) -> Dispatcher:
    """Wire storage, middlewares, global error handler and every registered feature router.

    `sessionmaker` is optional so tests can build a dispatcher without a
    database; middlewares that need one are then skipped.
    """
    dp = Dispatcher(storage=build_storage(settings), settings=settings)

    dp.errors.register(_global_error_handler)

    if sessionmaker is not None:
        setup_middlewares(dp, settings, sessionmaker)

    features = load_features()
    admission = MediaAdmission(settings)
    for feature in features:
        if feature.router.name in {
            "audio",
            "batch",
            "convert",
            "downloader",
            "filters",
            "music_search",
            "shazam",
            "stickerpack",
            "tag_editor",
            "textoverlay",
            "videotools",
            "wide",
        }:
            feature.router.message.middleware(admission)
            feature.router.callback_query.middleware(admission)
        dp.include_router(feature.router)
    log.info("features_loaded", count=len(features), names=[f.name for f in features])

    return dp
