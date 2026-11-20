"""GroupPanelGuardMiddleware and panel context store.

Enforces panel ownership and persists media metadata.

Prevents unauthorized group members from tapping inline buttons on media panels,
and stores media metadata so CallbackQuery handlers can reliably access file_id,
kind, and file_size (since Telegram Bot API omits reply_to_message in callback updates).
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message, TelegramObject

from app.cache.fallback import FallbackCache
from app.cache.redis import get_redis
from app.core.logging import get_logger

log = get_logger(__name__)

# Fallback in-memory store if Redis is unavailable (capped: evict oldest first).
_IN_MEMORY_PANELS = FallbackCache(2048, 86400)


async def store_panel(
    chat_id: int,
    message_id: int,
    owner_id: int,
    owner_name: str,
    reply_to_message_id: int | None = None,
    kind: str = "",
    file_id: str = "",
    file_size: int = 0,
) -> None:
    """Register a message as an owned media panel with cached media metadata."""
    key = f"mewvia:group_panel:{chat_id}:{message_id}"
    payload = {
        "owner_id": owner_id,
        "owner_name": owner_name,
        "reply_to_message_id": reply_to_message_id,
        "kind": kind,
        "file_id": file_id,
        "file_size": file_size,
        "opts": {},
    }
    try:
        redis = get_redis()
        await redis.set(key, json.dumps(payload), ex=86400)
        _IN_MEMORY_PANELS.discard(key)
    except Exception as exc:
        log.warning("panel_store_redis_failed", error=str(exc))
        _IN_MEMORY_PANELS.put(key, payload)


async def get_panel(chat_id: int, message_id: int) -> dict[str, Any] | None:
    """Retrieve panel record if it exists."""
    key = f"mewvia:group_panel:{chat_id}:{message_id}"
    try:
        redis = get_redis()
        raw = await redis.get(key)
        if raw:
            data: dict[str, Any] = json.loads(raw)
            return data
    except Exception as exc:
        log.warning("panel_store_redis_get_failed", error=str(exc))

    return _IN_MEMORY_PANELS.get(key)


async def get_panel_opts(chat_id: int, message_id: int) -> dict[str, Any]:
    panel = await get_panel(chat_id, message_id)
    if panel and "opts" in panel:
        return panel["opts"]
    return {}


async def update_panel_opts(chat_id: int, message_id: int, **kwargs: Any) -> dict[str, Any]:
    panel = await get_panel(chat_id, message_id)
    if not panel:
        return {}

    opts = panel.get("opts", {})
    opts.update(kwargs)
    panel["opts"] = opts

    key = f"mewvia:group_panel:{chat_id}:{message_id}"
    try:
        redis = get_redis()
        await redis.set(key, json.dumps(panel), ex=86400)
        _IN_MEMORY_PANELS.discard(key)
    except Exception as exc:
        log.warning("panel_opts_redis_update_failed", error=str(exc))
        _IN_MEMORY_PANELS.put(key, panel)

    return opts


# Backward compatibility aliases
store_panel_owner = store_panel
get_panel_owner = get_panel


class GroupPanelGuardMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        inner = getattr(event, "event", event)

        if not isinstance(inner, CallbackQuery):
            return await handler(event, data)

        query: CallbackQuery = inner
        if not query.message or not isinstance(query.message, Message):
            return await handler(event, data)

        chat = query.message.chat
        if chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
            return await handler(event, data)

        panel = await get_panel(chat.id, query.message.message_id)
        if not panel:
            return await handler(event, data)

        owner_id = panel.get("owner_id")
        user_id = query.from_user.id if query.from_user else 0

        if owner_id and user_id != owner_id:
            owner_name = panel.get("owner_name", "شخص دیگری")
            await query.answer(
                f"⚠️ این پنل فقط برای کاربر «{owner_name}» فعال است!",
                show_alert=True,
            )
            return None

        return await handler(event, data)
