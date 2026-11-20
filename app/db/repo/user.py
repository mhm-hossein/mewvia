"""User queries."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from aiogram.types import User as TgUser
from sqlalchemy import Select, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.db.models import User


class UserRepo:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert_from_telegram(self, tg_user: TgUser) -> User:
        """Insert or refresh a user from an incoming update.

        One statement with RETURNING: the tracking middleware runs on every
        update, so an extra SELECT round-trip would double the cost of the
        hottest query in the bot.

        `is_banned` / `has_blocked_bot` are deliberately not touched — those are
        our own state, not Telegram's. Seeing an update does clear
        `has_blocked_bot`, because a user who messages us has evidently unblocked.
        """
        now = datetime.now(UTC)
        values = {
            "telegram_id": tg_user.id,
            "username": tg_user.username,
            "first_name": tg_user.first_name,
            "last_name": tg_user.last_name,
            "language_code": tg_user.language_code,
            "is_premium": bool(tg_user.is_premium),
            "updates_seen": 1,
            "created_at": now,
            "last_seen_at": now,
        }

        stmt = (
            insert(User)
            .values(**values)
            .on_conflict_do_update(
                index_elements=[User.telegram_id],
                set_={
                    "username": tg_user.username,
                    "first_name": tg_user.first_name,
                    "last_name": tg_user.last_name,
                    "language_code": tg_user.language_code,
                    "is_premium": bool(tg_user.is_premium),
                    "updates_seen": User.updates_seen + 1,
                    "last_seen_at": now,
                    "has_blocked_bot": False,
                },
            )
            .returning(User)
        )
        # populate_existing: if this user is already in the session's identity
        # map, the ORM would hand back the stale instance and silently discard
        # what RETURNING just gave us.
        result = await self.session.execute(stmt, execution_options={"populate_existing": True})
        return result.scalar_one()

    async def get(self, telegram_id: int) -> User | None:
        return await self.session.get(User, telegram_id)

    async def set_banned(self, telegram_id: int, banned: bool) -> bool:
        user = await self.get(telegram_id)
        if user is None:
            return False
        user.is_banned = banned
        return True

    async def mark_blocked(self, telegram_id: int) -> None:
        """Called when Telegram answers 403 — the user blocked the bot."""
        user = await self.get(telegram_id)
        if user is not None:
            user.has_blocked_bot = True

    # ---- listings & stats (used by the admin panel in phase 5) ----

    def _search_stmt(self, query: str | None) -> Select[tuple[User]]:
        stmt = select(User)
        if query:
            like = f"%{query}%"
            conditions: list[ColumnElement[bool]] = [
                User.username.ilike(like),
                User.first_name.ilike(like),
                User.last_name.ilike(like),
            ]
            digits = query.removeprefix("-")
            if len(digits) <= 19 and digits.isdecimal():
                candidate = int(query)
                if -(2**63) <= candidate < 2**63:
                    conditions.append(User.telegram_id == candidate)
            stmt = stmt.where(or_(*conditions))
        return stmt

    async def list_page(
        self, *, offset: int = 0, limit: int = 10, query: str | None = None
    ) -> Sequence[User]:
        stmt = (
            self._search_stmt(query).order_by(User.last_seen_at.desc()).offset(offset).limit(limit)
        )
        return (await self.session.execute(stmt)).scalars().all()

    async def count(self, query: str | None = None) -> int:
        stmt = self._search_stmt(query).with_only_columns(func.count(User.telegram_id))
        return (await self.session.execute(stmt)).scalar_one()

    async def count_active_since(self, since: datetime) -> int:
        stmt = select(func.count(User.telegram_id)).where(User.last_seen_at >= since)
        return (await self.session.execute(stmt)).scalar_one()

    async def count_new_since(self, since: datetime) -> int:
        stmt = select(func.count(User.telegram_id)).where(User.created_at >= since)
        return (await self.session.execute(stmt)).scalar_one()

    async def stats(self) -> dict[str, int]:
        """Counters for the admin dashboard, in one round trip."""
        now = datetime.now(UTC)
        day_ago = now - timedelta(days=1)
        week_ago = now - timedelta(days=7)

        stmt = select(
            func.count(User.telegram_id).label("total"),
            func.count(User.telegram_id).filter(User.last_seen_at >= day_ago).label("active_24h"),
            func.count(User.telegram_id).filter(User.last_seen_at >= week_ago).label("active_7d"),
            func.count(User.telegram_id).filter(User.created_at >= day_ago).label("new_24h"),
            func.count(User.telegram_id).filter(User.created_at >= week_ago).label("new_7d"),
            func.count(User.telegram_id).filter(User.is_banned).label("banned"),
            func.count(User.telegram_id).filter(User.has_blocked_bot).label("blocked_bot"),
            func.count(User.telegram_id).filter(User.is_premium).label("premium"),
        )
        row = (await self.session.execute(stmt)).one()
        return dict(row._mapping)

    async def iter_broadcast_targets(self) -> Sequence[int]:
        """Ids eligible for a broadcast: not banned, has not blocked the bot."""
        stmt = select(User.telegram_id).where(
            User.is_banned.is_(False),
            User.has_blocked_bot.is_(False),
        )
        return (await self.session.execute(stmt)).scalars().all()
