"""Admin broadcast message engine."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import Message
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.logging import get_logger
from app.db.models.user import User

log = get_logger(__name__)


@dataclass
class BroadcastResult:
    total: int = 0
    successful: int = 0
    failed: int = 0
    blocked: int = 0


async def send_broadcast(
    bot: Bot,
    session: AsyncSession,
    source_message: Message,
    *,
    rate_limit: float = 0.04,  # ~25 messages / sec
) -> BroadcastResult:
    """Broadcast source_message to all active users."""
    stmt = (
        select(User.telegram_id)
        .where(User.is_banned.is_(False), User.has_blocked_bot.is_(False))
        .order_by(User.telegram_id.asc())
    )
    ceiling = await session.scalar(select(func.max(User.telegram_id)))
    last: int | None = None
    result = BroadcastResult()
    while ceiling is not None:
        page = stmt.where(User.telegram_id <= ceiling).limit(256)
        if last is not None:
            page = page.where(User.telegram_id > last)
        ids = (await session.scalars(page)).all()
        await session.commit()  # release the connection before Telegram calls/sleeps
        if not ids:
            break
        blocked = []
        for uid in ids:
            result.total += 1
            try:
                for attempt in range(2):
                    try:
                        await bot.copy_message(
                            chat_id=uid,
                            from_chat_id=source_message.chat.id,
                            message_id=source_message.message_id,
                        )
                        break
                    except TelegramRetryAfter as exc:
                        if attempt:
                            raise
                        await asyncio.sleep(exc.retry_after)
                result.successful += 1
            except (TelegramForbiddenError, TelegramAPIError) as exc:
                err = str(exc).lower()
                if "blocked" in err or "user is deactivated" in err:
                    blocked.append(uid)
                    result.blocked += 1
                else:
                    result.failed += 1
            except Exception:
                result.failed += 1
            await asyncio.sleep(rate_limit)
        if blocked:
            await session.execute(
                update(User).where(User.telegram_id.in_(blocked)).values(has_blocked_bot=True)
            )
            await session.commit()
        last = ids[-1]
    return result
