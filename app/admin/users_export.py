"""Admin exports: fetch and serialize at most one batch of users at a time."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.user import User


def _json_row(u: User) -> str:
    return json.dumps(
        {
            "telegram_id": u.telegram_id,
            "username": u.username,
            "first_name": u.first_name,
            "last_name": u.last_name,
            "language_code": u.language_code,
            "is_premium": u.is_premium,
            "is_banned": u.is_banned,
            "has_blocked_bot": u.has_blocked_bot,
            "has_used_bot": u.has_used_bot,
            "updates_seen": u.updates_seen,
            "created_at": u.created_at.isoformat(),
            "last_seen_at": u.last_seen_at.isoformat(),
        },
        ensure_ascii=False,
    )


def _txt_row(u: User) -> str:
    uname = f"@{u.username}" if u.username else "-"
    cells = (
        str(u.telegram_id),
        uname,
        u.first_name or "-",
        u.last_name or "-",
        "Yes" if u.is_premium else "No",
        "Yes" if u.is_banned else "No",
        u.created_at.strftime("%Y-%m-%d"),
    )
    return "\t".join(
        cell.replace("\t", " ").replace("\r", " ").replace("\n", " ") for cell in cells
    )


async def _export(
    session: AsyncSession, path: Path, render: Callable[[User], str], start: str, sep: str, end: str
) -> Path:
    stmt = select(User).order_by(User.created_at, User.telegram_id).execution_options(yield_per=256)
    result = await session.stream_scalars(stmt)
    try:
        with path.open("w", encoding="utf-8") as out:
            out.write(start)
            first = True
            users: Sequence[User]
            async for users in result.partitions(256):
                chunk = ("" if first else sep) + sep.join(map(render, users))
                await asyncio.to_thread(out.write, chunk)
                first = False
            out.write(end)
    finally:
        await result.close()
    return path


async def export_users_json(session: AsyncSession, out_path: Path) -> Path:
    return await _export(session, out_path, _json_row, "[", ",\n", "]")


async def export_users_txt(session: AsyncSession, out_path: Path) -> Path:
    header = "ID\tUsername\tFirst Name\tLast Name\tPremium\tBanned\tJoined\n" + "-" * 80 + "\n"
    return await _export(session, out_path, _txt_row, header, "\n", "")
