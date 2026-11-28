"""Games & Entertainment feature router (Tic-Tac-Toe, Leaderboards, Leagues)."""

from __future__ import annotations

import asyncio
import contextlib
import html
import json
import random
from collections.abc import Sequence
from typing import Any, Literal

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.types import User as TelegramUser
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.fallback import FallbackCache
from app.cache.redis import get_redis
from app.callbacks import MenuCB, NoopCB
from app.callbacks.game import GameCB, SoloGameCB
from app.core.logging import get_logger
from app.core.units import fa_digits
from app.db.models.game_stat import GameStat
from app.db.models.user import User
from app.db.repo.game import GameRepo
from app.db.repo.group_game import GroupGameRepo
from app.db.repo.total_score import TotalScoreRepo
from app.db.repo.user import UserRepo
from app.features.games import solo_games
from app.features.games.group_rules import GAME_LABELS
from app.features.games.leagues import format_league_progress_bar, get_league
from app.features.games.xo_engine import (
    calculate_score_delta,
    check_winner,
    find_best_move,
)
from app.features.registry import Feature, register
from app.ui.emoji import EmojiRegistry, em, get_emoji_registry
from app.ui.kb import KB
from app.ui.labels import ReplyTextFilter
from app.ui.styles import Style
from app.ui.texts import t

log = get_logger(__name__)

router = Router(name="games")

DIFF_LABELS: dict[str, str] = {
    "easy": "آسان 🟢",
    "med": "متوسط 🟡",
    "hard": "سخت (ناممکن) 🔴",
}

_VALID_DIFFS = frozenset(DIFF_LABELS)
_XO_SESSION_TTL = 2 * 60 * 60
_XO_SESSION_CAP = 2048
# Fallback store when Redis is unavailable (tests / local runs).
_XO_SESSIONS = FallbackCache(_XO_SESSION_CAP, _XO_SESSION_TTL)
# Serialise moves on the same board: a double-tap must not process two updates
# against the same stale board.
_move_locks: dict[str, asyncio.Lock] = {}


def _move_lock(key: str) -> asyncio.Lock:
    lock = _move_locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _move_locks[key] = lock
    return lock


def _xo_key(chat_id: int, message_id: int) -> str:
    return f"mewvia:xo:{chat_id}:{message_id}"


async def load_xo_session(chat_id: int, message_id: int) -> dict[str, Any] | None:
    """Return the stored board for a game message, if any."""
    key = _xo_key(chat_id, message_id)
    try:
        raw = await get_redis().get(key)
        if raw:
            data: dict[str, Any] = json.loads(raw)
            return data
    except Exception as exc:
        log.warning("xo_session_redis_get_failed", error=str(exc))
    return _XO_SESSIONS.get(key)


async def save_xo_session(chat_id: int, message_id: int, board: str, diff: str) -> None:
    """Persist the authoritative board so callbacks cannot invent one."""
    key = _xo_key(chat_id, message_id)
    payload = {"board": board, "diff": diff}
    try:
        await get_redis().set(key, json.dumps(payload), ex=_XO_SESSION_TTL)
        _XO_SESSIONS.discard(key)
    except Exception as exc:
        log.warning("xo_session_redis_set_failed", error=str(exc))
        _XO_SESSIONS.put(key, payload)


async def drop_xo_session(chat_id: int, message_id: int) -> None:
    """Finish a game so its callbacks cannot be replayed for more points."""
    key = _xo_key(chat_id, message_id)
    _XO_SESSIONS.discard(key)
    try:
        await get_redis().delete(key)
    except Exception as exc:
        log.warning("xo_session_redis_delete_failed", error=str(exc))


# ---------------------------------------------------------------------------
# Gol ya Pooch (private, single player against the bot)
# ---------------------------------------------------------------------------
_PGOL_TTL = 2 * 60 * 60
_PGOL_SESSIONS = FallbackCache(1024, _PGOL_TTL)
_PGOL_ROUNDS = 3


def _pgol_key(chat_id: int, message_id: int) -> str:
    return f"mewvia:pgol:{chat_id}:{message_id}"


async def _load_pgol(chat_id: int, message_id: int) -> dict[str, Any] | None:
    key = _pgol_key(chat_id, message_id)
    try:
        raw = await get_redis().get(key)
        if raw:
            data: dict[str, Any] = json.loads(raw)
            return data
    except Exception as exc:
        log.warning("pgol_session_redis_get_failed", error=str(exc))
    return _PGOL_SESSIONS.get(key)


async def _save_pgol(chat_id: int, message_id: int, payload: dict[str, Any]) -> None:
    key = _pgol_key(chat_id, message_id)
    try:
        await get_redis().set(key, json.dumps(payload), ex=_PGOL_TTL)
        _PGOL_SESSIONS.discard(key)
    except Exception as exc:
        log.warning("pgol_session_redis_set_failed", error=str(exc))
        _PGOL_SESSIONS.put(key, payload)


async def _drop_pgol(chat_id: int, message_id: int) -> None:
    key = _pgol_key(chat_id, message_id)
    _PGOL_SESSIONS.discard(key)
    try:
        await get_redis().delete(key)
    except Exception as exc:
        log.warning("pgol_session_redis_delete_failed", error=str(exc))


def _render_pgol(state: dict[str, Any], registry: EmojiRegistry) -> tuple[str, Any]:
    round_no = int(state.get("round", 1))
    points = int(state.get("points", 0))
    finished = bool(state.get("finished"))
    reveal = bool(state.get("reveal"))

    lines = [
        f"{em('fist')} <b>گل یا پوچ — تک نفره</b>",
        f"نوبت <b>{fa_digits(str(round_no))}</b> از {fa_digits(str(_PGOL_ROUNDS))}",
        f"امتیاز شما: <b>{fa_digits(str(points))}</b>",
    ]
    kb = KB(registry)
    if finished:
        result = state.get("result", "draw")
        if result == "win":
            lines.append(f"\n🎉 <b>برنده شدی!</b> {registry.text('trophy')}")
        elif result == "loss":
            lines.append("\n😔 باختی — دفعه بعد گل رو پیدا میکنی.")
        else:
            lines.append("\n🤝 مساوی!")
        lines.append(f"\nنتیجه نهایی: <b>{points}</b> از {_PGOL_ROUNDS}")
        kb.button(
            "بازی دوباره",
            callback_data=GameCB(action="gol").pack(),
            style=Style.CONFIRM,
            emoji_slot="refresh",
        )
        kb.button(
            "جدول گل یا پوچ",
            callback_data=GameCB(action="gol_lb").pack(),
            style=Style.PRIMARY,
            emoji_slot="fist",
        )
        kb.adjust(1, 1)
    elif reveal:
        side = state.get("gol_side", "left")
        good = state.get("last_correct", False)
        lines.append(
            f"\n{registry.text('ok') if good else registry.text('error')} "
            f"گل تو دست <b>{'چپ' if side == 'left' else 'راست'}</b> بود!"
        )
        kb.button(
            "نوبت بعدی",
            callback_data=GameCB(action="gol_next").pack(),
            style=Style.CONFIRM,
            emoji_slot="next",
        )
        kb.adjust(1)
    else:
        lines.append(f"\n{em('gol_ball')} گل تو کدوم مشته؟")
        kb.button(
            "دست چپ",
            callback_data=GameCB(action="gol_guess", side="left").pack(),
            style=Style.PRIMARY,
            emoji_slot="fist",
        )
        kb.button(
            "دست راست",
            callback_data=GameCB(action="gol_guess", side="right").pack(),
            style=Style.PRIMARY,
            emoji_slot="fist",
        )
        kb.adjust(2)
    return "\n".join(lines), kb.as_markup()


async def ready_registry() -> EmojiRegistry:
    registry = get_emoji_registry()
    await registry.refresh_premium_flag()
    await registry.load()
    return registry


# ---------------------------------------------------------------------------
# Renderers
# ---------------------------------------------------------------------------


def render_games_hub(
    user: User,
    stat: GameStat,
    rank: int,
    total_players: int,
    registry: EmojiRegistry,
    total_score: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    combined = stat.score if total_score is None else total_score
    league = get_league(combined)
    u_name = html.escape(user.full_name)

    text = (
        f"{registry.text('game')} <b>مرکز بازی و مسابقات میوویا</b>\n\n"
        f"👤 بازیکن: <b>{u_name}</b>\n"
        f"{league.get_badge(registry)} لیگ: <b>{league.name}</b> — <i>«{league.title}»</i>\n"
        f"{registry.text('trophy')} ریتینگ دوز: <b>{stat.score:,}</b> | "
        f"مجموع بازیها: <b>{combined:,}</b> امتیاز\n"
        f"{em('pin')} رتبه جهانی: <b>#{rank}</b> (از {max(1, total_players):,} بازیکن)\n\n"
        f"📊 <b>کارنامه مسابقات:</b>\n"
        f"• بردها: <b>{stat.wins}</b> | باخت‌ها: <b>{stat.losses}</b> | مساوی: <b>{stat.draws}</b>\n"
        f"• نرخ پیروزی: <b>{stat.win_rate}%</b>\n"
        f"• زنجیره برد فعلی: <b>{stat.current_streak}</b> {registry.text('streak_fire')} "
        f"(بهترین: {stat.max_streak})\n\n"
        f"یک بازی برای رقابت با <b>میوویا</b> انتخاب کنید:"
    )

    kb = KB(registry)
    # Two rows of games against Mewvia.
    kb.button(
        "بازی دوز (XO)",
        callback_data=GameCB(action="diff").pack(),
        style=Style.CONFIRM,
        emoji_slot="game",
    )
    kb.button(
        "گل یا پوچ (تک نفره)",
        callback_data=GameCB(action="gol").pack(),
        style=Style.CONFIRM,
        emoji_slot="fist",
    )
    for game in solo_games.GAMES:
        kb.button(
            GAME_LABELS[game],
            callback_data=SoloGameCB(action="start", game=game).pack(),
            style=Style.CONFIRM,
        )

    # Ratings and profile.
    kb.button(
        "برترین‌ها و جدول رده‌بندی",
        callback_data=GameCB(action="lb").pack(),
        style=Style.PRIMARY,
        emoji_slot="trophy",
    )
    kb.button(
        "نردبان و وضعیت لیگ من",
        callback_data=GameCB(action="profile").pack(),
        style=Style.PRIMARY,
        emoji_slot="medal",
    )
    kb.button(
        "قوانین و راهنمای امتیازدهی",
        callback_data=GameCB(action="rules").pack(),
        style=Style.NEUTRAL,
        emoji_slot="file",
    )
    kb.button(
        "بازگشت به منوی اصلی",
        callback_data=MenuCB(target="root").pack(),
        style=Style.NEUTRAL,
        emoji_slot="back",
    )
    kb.adjust(2, 2, 2, 1, 1)

    return text, kb.as_markup()


def render_diff_picker(registry: EmojiRegistry) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"{registry.text('game')} <b>بازی دوز هوشمند (Tic-Tac-Toe)</b>\n\n"
        "حریف شما <b>هوش مصنوعی میوویا</b> است!\n"
        "سطح دشواری بازی را انتخاب کنید:\n\n"
        "🟢 <b>آسان:</b> هوش مصنوعی ساده‌تر (+۱۵ امتیاز برد)\n"
        "🟡 <b>متوسط:</b> دفاع هوشمند و حمله استاندارد (+۲۵ امتیاز برد)\n"
        "🔴 <b>سخت (ناممکن):</b> الگوریتم Minimax بدون خطا (+۴۰ امتیاز برد)\n\n"
        "<i>هرچه سطح بالاتری را شکست دهید، امتیاز بیشتری به ریتینگ اضافه می‌شود!</i>"
    )

    kb = KB(registry)
    kb.button(
        "سطح آسان (+۱۵)",
        callback_data=GameCB(action="xo", diff="easy").pack(),
        style=Style.NEUTRAL,
        emoji_slot="on",
    )
    kb.button(
        "سطح متوسط (+۲۵)",
        callback_data=GameCB(action="xo", diff="med").pack(),
        style=Style.CONFIRM,
        emoji_slot="yellow_circle",
    )
    kb.button(
        "سطح سخت و ناممکن (+۴۰)",
        callback_data=GameCB(action="xo", diff="hard").pack(),
        style=Style.DESTRUCTIVE,
        emoji_slot="off",
    )
    kb.button(
        "بازگشت به مرکز بازی",
        callback_data=GameCB(action="menu").pack(),
        style=Style.NEUTRAL,
        emoji_slot="back",
    )
    kb.adjust(1, 1, 1, 1)

    return text, kb.as_markup()


def render_xo_board(
    user: User,
    stat: GameStat,
    diff: str,
    board: str,
    registry: EmojiRegistry,
    last_outcome: str | None = None,
    score_delta: int = 0,
    old_score: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    diff_title = DIFF_LABELS.get(diff, "متوسط 🟡")
    league = get_league(stat.score)

    header = (
        f"{registry.text('game')} <b>بازی دوز (XO) با میوویا</b>\n"
        f"سطح رقابت: <b>{diff_title}</b>\n"
        f"علامت شما: <b>{registry.text('xo_x')}</b> "
        f"({stat.score:,} امتیاز - {league.get_badge(registry)}) | "
        f"میوویا: <b>{registry.text('xo_o')}</b>\n"
    )

    base_score = stat.score if old_score is None else old_score
    change = f"<code>{base_score:,}</code> → <code>{stat.score:,}</code> (<b>{score_delta:+,}</b>)"
    if last_outcome == "win":
        status_line = (
            f"\n🎉 <b>تبریک فراوان! شما برنده شدید!</b>\n"
            f"{registry.text('streak_fire')} ریتینگ شما: {change}"
        )
    elif last_outcome == "loss":
        note = "" if score_delta else " (حاشیه امن برنز — کسر نشد)"
        status_line = (
            f"\n{em('bot_robot')} <b>میوویا پیروز شد!</b>\n{em('down')} ریتینگ شما: {change}{note}"
        )
    elif last_outcome == "draw":
        status_line = f"\n{em('handshake')} <b>بازی مساوی شد!</b>\n✨ ریتینگ شما: {change}"
    else:
        status_line = "\nنوبت شماست! یک خانه خالی را لمس کنید:"

    text = f"{header}{status_line}"

    is_finished = last_outcome is not None

    kb = KB(registry)

    # 3x3 Grid using customizable slots for X, O, and empty
    for i in range(9):
        char = board[i]
        if char == "X":
            kb.button(
                "",
                callback_data=NoopCB().pack(),
                style=Style.CONFIRM,
                emoji_slot="xo_x",
            )
        elif char == "O":
            kb.button(
                "",
                callback_data=NoopCB().pack(),
                style=Style.DESTRUCTIVE,
                emoji_slot="xo_o",
            )
        else:
            if is_finished:
                kb.button(
                    "",
                    callback_data=NoopCB().pack(),
                    style=Style.PRIMARY,
                    emoji_slot="xo_empty",
                )
            else:
                kb.button(
                    "",
                    callback_data=GameCB(action="move", diff=diff, cell=i, b=board).pack(),
                    style=Style.PRIMARY,
                    emoji_slot="xo_empty",
                )

    widths = [3, 3, 3]

    if is_finished:
        kb.button(
            "بازی مجدد",
            callback_data=GameCB(action="reset", diff=diff).pack(),
            style=Style.CONFIRM,
            emoji_slot="refresh",
        )
        kb.button(
            "تغییر سطح",
            callback_data=GameCB(action="diff").pack(),
            style=Style.PRIMARY,
            emoji_slot="tool",
        )
        kb.button(
            "جدول برترین‌ها",
            callback_data=GameCB(action="lb").pack(),
            style=Style.PRIMARY,
            emoji_slot="trophy",
        )
        kb.button(
            "مرکز بازی‌ها",
            callback_data=GameCB(action="menu").pack(),
            style=Style.NEUTRAL,
            emoji_slot="back",
        )
        widths.extend([2, 1, 1])
    else:
        kb.button(
            "شروع مجدد",
            callback_data=GameCB(action="reset", diff=diff).pack(),
            style=Style.DESTRUCTIVE,
            emoji_slot="refresh",
        )
        kb.button(
            "تغییر سطح",
            callback_data=GameCB(action="diff").pack(),
            style=Style.NEUTRAL,
            emoji_slot="tool",
        )
        kb.button(
            "مرکز بازی‌ها",
            callback_data=GameCB(action="menu").pack(),
            style=Style.NEUTRAL,
            emoji_slot="back",
        )
        widths.extend([2, 1])

    kb.adjust(*widths)
    return text, kb.as_markup()


def render_leaderboard(
    user_id: int,
    top_stats: Sequence[GameStat],
    user_rank: int,
    total_players: int,
    registry: EmojiRegistry,
    totals: dict[int, int] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    lines = [
        f"{registry.text('trophy')} <b>جدول رتبه‌بندی برترین بازیکنان میوویا</b>\n",
        "۱۰ بازیکن برتر بر اساس بالاترین ریتینگ لیگ:\n",
    ]

    medals = ["🥇", "🥈", "🥉"]

    if not top_stats:
        lines.append("<i>هنوز هیچ مسابقه‌ای ثبت نشده است. اولین قهرمان باشید!</i>\n")
    else:
        for idx, s in enumerate(top_stats, start=1):
            badge = medals[idx - 1] if idx <= 3 else f"<b>{idx}.</b>"
            league_obj = get_league(totals.get(s.telegram_id, s.score) if totals else s.score)
            p_name = "بازیکن ناشناس"
            if s.user:
                p_name = s.user.full_name
            is_me = s.telegram_id == user_id
            me_tag = " (شما)" if is_me else ""
            name_escaped = html.escape(p_name)
            lines.append(
                f"{badge} <b>{name_escaped}</b>{me_tag} — "
                f"<b>{s.score:,}</b> امتیاز {league_obj.get_badge(registry)} "
                f"[برد: {s.wins} | باخت: {s.losses}]"
            )

    lines.append("\n──────────────")
    lines.append(f"{em('pin')} <b>رتبه شما:</b> #{user_rank} از {max(1, total_players):,} بازیکن")

    kb = KB(registry)
    kb.button(
        "بازی دوز (XO)",
        callback_data=GameCB(action="diff").pack(),
        style=Style.CONFIRM,
        emoji_slot="game",
    )
    kb.button(
        "به‌روزرسانی",
        callback_data=GameCB(action="lb").pack(),
        style=Style.PRIMARY,
        emoji_slot="refresh",
    )
    kb.button(
        "نردبان لیگ من",
        callback_data=GameCB(action="profile").pack(),
        style=Style.PRIMARY,
        emoji_slot="medal",
    )
    kb.button(
        "جدول گل یا پوچ",
        callback_data=GameCB(action="gol_lb").pack(),
        style=Style.PRIMARY,
        emoji_slot="fist",
    )
    kb.button(
        "جدول کلی (جمع بازیها)",
        callback_data=GameCB(action="total_lb").pack(),
        style=Style.CONFIRM,
        emoji_slot="trophy",
    )
    kb.button(
        "مرکز بازیها",
        callback_data=GameCB(action="menu").pack(),
        style=Style.NEUTRAL,
        emoji_slot="back",
    )
    kb.adjust(1, 2, 1, 1, 1)

    return "\n".join(lines), kb.as_markup()


def render_my_league(
    user: User,
    stat: GameStat,
    rank: int,
    total_players: int,
    registry: EmojiRegistry,
    total_score: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    combined = stat.score if total_score is None else total_score
    league = get_league(combined)
    bar_str = format_league_progress_bar(combined)
    u_name = html.escape(user.full_name)

    text = (
        f"{registry.text('medal')} <b>شناسنامه لیگ و وضعیت شما</b>\n\n"
        f"👤 بازیکن: <b>{u_name}</b>\n"
        f"{league.get_badge(registry)} لیگ فعلی: <b>{league.name}</b>\n"
        f"{em('label')} لقب افتخاری: <b>«{league.title}»</b>\n"
        f"{registry.text('trophy')} ریتینگ: <b>{stat.score:,}</b> امتیاز "
        f"(رتبه #{rank} از {max(1, total_players):,})\n\n"
        f"{em('stats')} <b>مسیر ارتقا به لیگ بعدی:</b>\n"
        f"<code>{bar_str}</code>\n\n"
        f"📊 <b>آمار تفصیلی:</b>\n"
        f"• کل مسابقات: <b>{stat.total_games}</b>\n"
        f"• پیروزی‌ها: <b>{stat.wins}</b> (نرخ برد: {stat.win_rate}%)\n"
        f"• شکست‌ها: <b>{stat.losses}</b> | تساوی‌ها: <b>{stat.draws}</b>\n"
        f"• زنجیره برد پی‌درپی فعلی: <b>{stat.current_streak}</b> {registry.text('streak_fire')}\n"
        f"• بالاترین رکورد پیروزی متوالی: <b>{stat.max_streak}</b> ✨\n\n"
        f"{em('medal')} <b>سطوح نردبان لیگ‌های میوویا:</b>\n"
        f"🥉 <b>لیگ برنز:</b> ۱,۰۰۰ تا ۱,۱۴۹ امتیاز\n"
        f"🥈 <b>لیگ نقره:</b> ۱,۱۵۰ تا ۱,۳۴۹ امتیاز\n"
        f"🥇 <b>لیگ طلا:</b> ۱,۳۵۰ تا ۱,۶۴۹ امتیاز\n"
        f"💎 <b>لیگ الماس:</b> ۱,۶۵۰ تا ۱,۹۹۹ امتیاز\n"
        f"👑 <b>لیگ افسانه‌ای مستر:</b> ۲,۰۰۰+ امتیاز"
    )

    kb = KB(registry)
    kb.button(
        "بازی دوز (XO)",
        callback_data=GameCB(action="diff").pack(),
        style=Style.CONFIRM,
        emoji_slot="game",
    )
    kb.button(
        "جدول برترین‌ها",
        callback_data=GameCB(action="lb").pack(),
        style=Style.PRIMARY,
        emoji_slot="trophy",
    )
    kb.button(
        "مرکز بازی‌ها",
        callback_data=GameCB(action="menu").pack(),
        style=Style.NEUTRAL,
        emoji_slot="back",
    )
    kb.adjust(1, 1, 1)

    return text, kb.as_markup()


def render_rules(registry: EmojiRegistry) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        f"{registry.text('file')} <b>راهنما و قوانین امتیازدهی بازی دوز میوویا</b>\n\n"
        "سیستم رتبه‌بندی بازی‌ها بر پایه ریتینگ هوشمند طراحی شده است:\n\n"
        f"{em('target')} <b>ضرایب امتیاز بر اساس سطح دشواری:</b>\n"
        "• <b>سطح آسان 🟢:</b> برد +۱۵ | مساوی +۲ | باخت -۵\n"
        "• <b>سطح متوسط 🟡:</b> برد +۲۵ | مساوی +۵ | باخت -۱۰\n"
        "• <b>سطح سخت 🔴:</b> برد +۴۰ | مساوی +۱۰ | باخت -۱۵\n\n"
        f"{registry.text('streak_fire')} <b>پاداش زنجیره برد (Streak Bonus):</b>\n"
        "با هر برد متوالی، به ازای هر برد +۲ امتیاز پاداش ویژه دریافت می‌کنید!\n\n"
        f"{em('shield')} <b>حاشیه امن برنز:</b>\n"
        "امتیاز بازیکنان تازه وارد زیر ۱,۰۰۰ امتیاز کسر نخواهد شد."
    )

    kb = KB(registry)
    kb.button(
        "شروع بازی دوز",
        callback_data=GameCB(action="diff").pack(),
        style=Style.CONFIRM,
        emoji_slot="game",
    )
    kb.button(
        "مرکز بازی‌ها",
        callback_data=GameCB(action="menu").pack(),
        style=Style.NEUTRAL,
        emoji_slot="back",
    )
    kb.adjust(1, 1)

    return text, kb.as_markup()


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------


async def _games_hub(
    actor: TelegramUser, session: AsyncSession
) -> tuple[str, InlineKeyboardMarkup]:
    registry = await ready_registry()
    user = await UserRepo(session).upsert_from_telegram(actor)
    game_repo = GameRepo(session)
    stat = await game_repo.get_or_create(user.telegram_id)
    rank = await game_repo.get_user_rank(user.telegram_id)
    total = await game_repo.get_total_players()
    combined = await TotalScoreRepo(session).total_for(user.telegram_id)

    return render_games_hub(user, stat, rank, total, registry, combined)


@router.message(F.chat.type == ChatType.PRIVATE, Command("game"))
@router.message(F.chat.type == ChatType.PRIVATE, ReplyTextFilter(lambda: t("menu.games")))
async def reply_games(message: Message, session: AsyncSession) -> None:
    if message.from_user:
        text, reply_markup = await _games_hub(message.from_user, session)
        await message.reply(text, reply_markup=reply_markup)


@router.callback_query(MenuCB.filter(F.target == "games"))
@router.callback_query(GameCB.filter(F.action == "menu"))
async def cb_games_menu(query: CallbackQuery, session: AsyncSession) -> None:
    if not query.from_user or not isinstance(query.message, Message):
        return
    await query.answer()
    text, reply_markup = await _games_hub(query.from_user, session)
    try:
        await query.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        pass


@router.callback_query(F.message.chat.type == ChatType.PRIVATE, SoloGameCB.filter())
async def cb_solo_game(
    query: CallbackQuery, callback_data: SoloGameCB, session: AsyncSession
) -> None:
    message, cb = query.message, callback_data
    if not isinstance(message, Message) or message.chat.id != query.from_user.id:
        await query.answer()
        return

    key = solo_games.key_for(message.chat.id, message.message_id)
    async with solo_games.lock(key):
        state = await solo_games.load(key)
        expired = "این دکمه قدیمی شده. برای بازی دوباره /game رو بفرست."
        if cb.action == "start" and not cb.match_id and cb.game in solo_games.GAMES:
            if state and not state.get("closed"):
                await query.answer(expired)
                return
            updated = solo_games.new_match(cb.game)
        elif (
            not state
            or state.get("closed")
            or (cb.game, cb.match_id, cb.revision)
            != (state["game"], state["match_id"], state["revision"])
        ):
            await query.answer(expired)
            return
        elif cb.action == "menu":
            await query.answer()
            text, markup = await _games_hub(query.from_user, session)
            await message.edit_text(text, reply_markup=markup)
            await solo_games.save(key, {**state, "closed": True})
            return
        elif cb.action == "again" and state["finished"]:
            updated = solo_games.new_match(cb.game)
        elif cb.action == "move" and (moved := solo_games.play(state, cb.move)) is not None:
            updated = moved
        else:
            await query.answer("این حرکت قابل انجام نیست.")
            return
        await query.answer()
        text, markup = solo_games.render(updated, await ready_registry())
        await message.edit_text(text, reply_markup=markup)
        await solo_games.save(key, updated)


@router.callback_query(GameCB.filter(F.action == "diff"))
async def cb_diff_picker(query: CallbackQuery) -> None:
    """Show the difficulty chooser. Telegram no longer supports media widgets."""
    if not query.from_user or not isinstance(query.message, Message):
        return
    await query.answer()
    registry = await ready_registry()
    text, reply_markup = render_diff_picker(registry)
    try:
        await query.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        pass


@router.callback_query(GameCB.filter(F.action.in_({"xo", "reset"})))
async def cb_start_xo(query: CallbackQuery, callback_data: GameCB, session: AsyncSession) -> None:
    """Start (or restart) a board game using the classic inline keyboard."""
    if not query.from_user or not isinstance(query.message, Message):
        return
    await query.answer()
    diff = callback_data.diff if callback_data.diff in _VALID_DIFFS else "med"
    try:
        registry = await ready_registry()
        user = await UserRepo(session).upsert_from_telegram(query.from_user)
        stat = await GameRepo(session).get_or_create(user.telegram_id)
        text, reply_markup = render_xo_board(user, stat, diff, "_" * 9, registry)
        await query.message.edit_text(text, reply_markup=reply_markup)
        await save_xo_session(query.message.chat.id, query.message.message_id, "_" * 9, diff)
    except Exception as exc:
        log.error("xo_board_start_failed", error=str(exc))
        await query.answer(
            f"{em('warning')} شروع بازی ناموفق بود. دوباره تلاش کنید.", show_alert=True
        )


@router.callback_query(GameCB.filter(F.action == "move"))
async def cb_handle_move(
    query: CallbackQuery, callback_data: GameCB, session: AsyncSession
) -> None:
    if not query.from_user or not isinstance(query.message, Message):
        return

    cell = callback_data.cell
    if not 0 <= cell <= 8:
        await query.answer("حرکت نامعتبر است.")
        return

    chat_id = query.message.chat.id
    message_id = query.message.message_id
    async with _move_lock(f"{chat_id}:{message_id}"):
        # The stored board is the only authority. `callback_data.b` is a hint
        # from the button that was rendered; using it directly let a stale
        # double-tap (or an edited message) produce the "page changed" alert.
        xo_state = await load_xo_session(chat_id, message_id)
        if xo_state is None:
            # No authoritative state: a pristine board is the only payload we
            # may accept. A fabricated almost-won board must still be rejected.
            if callback_data.b != "_" * 9:
                await query.answer(
                    "این بازی منقضی یا نامعتبر شده است. لطفاً از منوی بازی شروع کنید.",
                    show_alert=True,
                )
                return
            board = "_" * 9
            diff = callback_data.diff
        else:
            # A stale hint (double-tap / edited message) is ignored; the
            # server's board is the sole authority, so no alert is needed.
            board = str(xo_state.get("board", "_" * 9))
            diff = str(xo_state.get("diff", callback_data.diff))
        if diff not in _VALID_DIFFS:
            diff = "med"
        if len(board) != 9 or any(char not in "_XO" for char in board):
            board = "_" * 9

        winner_now, _ = check_winner(board)
        if winner_now is not None:
            await query.answer("بازی به پایان رسیده است.")
            return
        if board[cell] != "_":
            await query.answer("این خانه قبلاً انتخاب شده است!")
            return

        user_repo = UserRepo(session)
        user = await user_repo.upsert_from_telegram(query.from_user)
        game_repo = GameRepo(session)
        stat = await game_repo.get_or_create(user.telegram_id)

        b_list = list(board)
        b_list[cell] = "X"
        board_after_player = "".join(b_list)

        winner, _ = check_winner(board_after_player)
        registry = await ready_registry()
        old_score = stat.score

        if winner == "X":
            delta = calculate_score_delta("win", diff, stat.score, stat.current_streak)
            stat = await game_repo.record_result(user.telegram_id, "win", delta)
            await drop_xo_session(chat_id, message_id)
            await query.answer("🎉 تبریک! برنده شدید!")
            text, reply_markup = render_xo_board(
                user, stat, diff, board_after_player, registry, "win", delta, old_score
            )
            with contextlib.suppress(Exception):
                await query.message.edit_text(text, reply_markup=reply_markup)
            return

        if winner == "draw":
            delta = calculate_score_delta("draw", diff, stat.score, stat.current_streak)
            stat = await game_repo.record_result(user.telegram_id, "draw", delta)
            await drop_xo_session(chat_id, message_id)
            await query.answer(f"{em('handshake')} بازی مساوی شد!")
            text, reply_markup = render_xo_board(
                user, stat, diff, board_after_player, registry, "draw", delta, old_score
            )
            with contextlib.suppress(Exception):
                await query.message.edit_text(text, reply_markup=reply_markup)
            return

        bot_move = find_best_move(board_after_player, diff)
        if bot_move != -1:
            b_list[bot_move] = "O"
        board_after_bot = "".join(b_list)
        winner_bot, _ = check_winner(board_after_bot)

        if winner_bot == "O":
            delta = calculate_score_delta("loss", diff, stat.score, stat.current_streak)
            stat = await game_repo.record_result(user.telegram_id, "loss", delta)
            await drop_xo_session(chat_id, message_id)
            await query.answer(f"{em('bot_robot')} میوویا برنده شد!")
            text, reply_markup = render_xo_board(
                user, stat, diff, board_after_bot, registry, "loss", delta, old_score
            )
            with contextlib.suppress(Exception):
                await query.message.edit_text(text, reply_markup=reply_markup)
            return

        if winner_bot == "draw":
            delta = calculate_score_delta("draw", diff, stat.score, stat.current_streak)
            stat = await game_repo.record_result(user.telegram_id, "draw", delta)
            await drop_xo_session(chat_id, message_id)
            await query.answer(f"{em('handshake')} بازی مساوی شد!")
            text, reply_markup = render_xo_board(
                user, stat, diff, board_after_bot, registry, "draw", delta, old_score
            )
            with contextlib.suppress(Exception):
                await query.message.edit_text(text, reply_markup=reply_markup)
            return

        await save_xo_session(chat_id, message_id, board_after_bot, diff)
        await query.answer()
        text, reply_markup = render_xo_board(user, stat, diff, board_after_bot, registry, None, 0)
        with contextlib.suppress(Exception):
            await query.message.edit_text(text, reply_markup=reply_markup)


@router.callback_query(GameCB.filter(F.action == "lb"))
async def cb_leaderboard(query: CallbackQuery, session: AsyncSession) -> None:
    if not query.from_user or not isinstance(query.message, Message):
        return
    await query.answer()
    registry = await ready_registry()
    game_repo = GameRepo(session)

    top_stats = await game_repo.get_leaderboard(limit=10)
    user_rank = await game_repo.get_user_rank(query.from_user.id)
    total_players = await game_repo.get_total_players()
    totals = await TotalScoreRepo(session).totals()

    text, reply_markup = render_leaderboard(
        user_id=query.from_user.id,
        top_stats=top_stats,
        user_rank=user_rank,
        total_players=total_players,
        registry=registry,
        totals=totals,
    )
    try:
        await query.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        pass


@router.callback_query(GameCB.filter(F.action == "profile"))
async def cb_my_league(query: CallbackQuery, session: AsyncSession) -> None:
    if not query.from_user or not isinstance(query.message, Message):
        return
    await query.answer()
    registry = await ready_registry()
    user_repo = UserRepo(session)
    user = await user_repo.upsert_from_telegram(query.from_user)

    game_repo = GameRepo(session)
    stat = await game_repo.get_or_create(user.telegram_id)
    rank = await game_repo.get_user_rank(user.telegram_id)
    total_players = await game_repo.get_total_players()
    combined = await TotalScoreRepo(session).total_for(user.telegram_id)

    text, reply_markup = render_my_league(
        user=user,
        stat=stat,
        rank=rank,
        total_players=total_players,
        registry=registry,
        total_score=combined,
    )
    try:
        await query.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        pass


@router.callback_query(GameCB.filter(F.action == "rules"))
async def cb_rules(query: CallbackQuery) -> None:
    if not isinstance(query.message, Message):
        return
    await query.answer()
    registry = await ready_registry()
    text, reply_markup = render_rules(registry)
    try:
        await query.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Feature registration
# ---------------------------------------------------------------------------

feature = register(
    Feature(
        name="games",
        router=router,
        menu_label_key="menu.games",
        menu_emoji_slot="game",
        menu_style=Style.PRIMARY,
        order=140,
    )
)


@router.callback_query(GameCB.filter(F.action == "gol"))
async def cb_private_gol_start(
    query: CallbackQuery, callback_data: GameCB, session: AsyncSession
) -> None:
    if not isinstance(query.message, Message) or not query.from_user:
        return
    if query.message.chat.type != ChatType.PRIVATE:
        await query.answer("این بازی فقط در پیوی است.", show_alert=True)
        return
    state = {
        "round": 1,
        "points": 0,
        "gol_side": random.choice(("left", "right")),
        "reveal": False,
        "finished": False,
        "last_correct": False,
        "owner": query.from_user.id,
    }
    await _save_pgol(query.message.chat.id, query.message.message_id, state)
    registry = await ready_registry()
    text, kb = _render_pgol(state, registry)
    await query.message.edit_text(text, reply_markup=kb)
    await query.answer("👊 گل یا پوچ شروع شد!")


@router.callback_query(GameCB.filter(F.action == "gol_guess"))
async def cb_private_gol_guess(
    query: CallbackQuery, callback_data: GameCB, session: AsyncSession
) -> None:
    if not isinstance(query.message, Message) or not query.from_user:
        return
    if query.message.chat.type != ChatType.PRIVATE:
        await query.answer()
        return
    state = await _load_pgol(query.message.chat.id, query.message.message_id)
    if not state or state.get("finished") or state.get("reveal"):
        await query.answer("این بازی تمام شده یا نوبت دیگهای است.", show_alert=True)
        return
    if state.get("owner") != query.from_user.id:
        await query.answer("فقط بازیکن بازی میتواند حدس بزند.", show_alert=True)
        return
    side = callback_data.side
    if side not in ("left", "right"):
        return
    correct = side == state.get("gol_side")
    state["last_correct"] = correct
    state["reveal"] = True
    if correct:
        state["points"] = int(state.get("points", 0)) + 1
    await _save_pgol(query.message.chat.id, query.message.message_id, state)
    registry = await ready_registry()
    text, kb = _render_pgol(state, registry)
    await query.message.edit_text(text, reply_markup=kb)
    await query.answer("🎉 درست!" if correct else "❌ پوچ بود!")


@router.callback_query(GameCB.filter(F.action == "gol_next"))
async def cb_private_gol_next(
    query: CallbackQuery, callback_data: GameCB, session: AsyncSession
) -> None:
    if not isinstance(query.message, Message) or not query.from_user:
        return
    if query.message.chat.type != ChatType.PRIVATE:
        await query.answer()
        return
    state = await _load_pgol(query.message.chat.id, query.message.message_id)
    if not state or not state.get("reveal") or state.get("finished"):
        await query.answer("نوبت جدید آماده نیست.", show_alert=True)
        return
    if state.get("owner") != query.from_user.id:
        await query.answer("فقط بازیکن بازی میتواند ادامه دهد.", show_alert=True)
        return
    round_no = int(state.get("round", 1))
    if round_no < _PGOL_ROUNDS:
        state.update(
            {
                "round": round_no + 1,
                "gol_side": random.choice(("left", "right")),
                "reveal": False,
                "last_correct": False,
            }
        )
        await _save_pgol(query.message.chat.id, query.message.message_id, state)
        registry = await ready_registry()
        text, kb = _render_pgol(state, registry)
        await query.message.edit_text(text, reply_markup=kb)
        await query.answer("👊 نوبت بعدی!")
        return

    # Finish — record into the global gol ladder (chat_id = 0) plus the
    # ordinary ratings table so the existing leaderboard stays coherent.
    points = int(state.get("points", 0))
    outcome: Literal["win", "loss", "draw"] = (
        "win" if points > _PGOL_ROUNDS / 2 else "loss" if points < _PGOL_ROUNDS / 2 else "draw"
    )
    uid = query.from_user.id
    pstat = await GroupGameRepo(session).get_or_create(0, "gol", uid)
    pdelta = calculate_score_delta(outcome, "med", pstat.score, pstat.current_streak)
    await GroupGameRepo(session).record_result(0, "gol", uid, outcome, pdelta)
    state.update({"finished": True, "result": outcome})
    await _save_pgol(query.message.chat.id, query.message.message_id, state)
    registry = await ready_registry()
    text, kb = _render_pgol(state, registry)
    await query.message.edit_text(text, reply_markup=kb)
    await query.answer("🏁 بازی تمام شد!")


def render_gol_leaderboard(
    user_id: int,
    top_stats: Sequence[Any],
    names: dict[int, str],
    user_rank: int,
    total_players: int,
    registry: EmojiRegistry,
    totals: dict[int, int] | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    lines = [
        f"{registry.text('trophy')} <b>جدول رتبهبندی گل یا پوچ</b>\n",
        "۱۰ بازیکن برتر بر اساس بالاترین ریتینگ لیگ:\n",
    ]
    medals = ["🥇", "🥈", "🥉"]

    if not top_stats:
        lines.append("<i>هنوز هیچ مسابقهای ثبت نشده است. اولین قهرمان باشید!</i>\n")
    else:
        for idx, s in enumerate(top_stats, start=1):
            badge = medals[idx - 1] if idx <= 3 else f"<b>{idx}.</b>"
            league_obj = get_league(totals.get(s.telegram_id, s.score) if totals else s.score)
            p_name = names.get(s.telegram_id, "بازیکن ناشناس")
            is_me = s.telegram_id == user_id
            me_tag = " (شما)" if is_me else ""
            name_escaped = html.escape(p_name)
            lines.append(
                f"{badge} <b>{name_escaped}</b>{me_tag} — "
                f"<b>{s.score:,}</b> امتیاز {league_obj.get_badge(registry)} "
                f"[برد: {s.wins} | باخت: {s.losses}]"
            )

    lines.append("\n──────────────")
    lines.append(f"{em('pin')} <b>رتبه شما:</b> #{user_rank} از {max(1, total_players):,} بازیکن")

    kb = KB(registry)
    kb.button(
        "بازی گل یا پوچ",
        callback_data=GameCB(action="gol").pack(),
        style=Style.CONFIRM,
        emoji_slot="fist",
    )
    kb.button(
        "بهروزرسانی",
        callback_data=GameCB(action="gol_lb").pack(),
        style=Style.PRIMARY,
        emoji_slot="refresh",
    )
    kb.button(
        "نردبان لیگ من",
        callback_data=GameCB(action="profile").pack(),
        style=Style.PRIMARY,
        emoji_slot="medal",
    )
    kb.button(
        "جدول دوز (XO)",
        callback_data=GameCB(action="lb").pack(),
        style=Style.PRIMARY,
        emoji_slot="trophy",
    )
    kb.button(
        "جدول کلی (جمع بازیها)",
        callback_data=GameCB(action="total_lb").pack(),
        style=Style.CONFIRM,
        emoji_slot="trophy",
    )
    kb.button(
        "مرکز بازیها",
        callback_data=GameCB(action="menu").pack(),
        style=Style.NEUTRAL,
        emoji_slot="back",
    )
    kb.adjust(1, 2, 1, 1, 1, 1)
    return "\n".join(lines), kb.as_markup()


@router.callback_query(GameCB.filter(F.action == "gol_lb"))
async def cb_gol_leaderboard(
    query: CallbackQuery, callback_data: GameCB, session: AsyncSession
) -> None:
    if not isinstance(query.message, Message) or not query.from_user:
        return
    repo = GroupGameRepo(session)
    top = await repo.leaderboard(0, "gol", 10)
    uid = query.from_user.id
    rank = await repo.rank(0, "gol", uid)
    total = await repo.total_players(0, "gol")
    users = UserRepo(session)
    names: dict[int, str] = {}
    for stat in top:
        user = await users.get(stat.telegram_id)
        if user:
            names[stat.telegram_id] = user.full_name
    me = await users.get(uid)
    if me:
        names[uid] = me.full_name
    registry = await ready_registry()
    totals = await TotalScoreRepo(session).totals()
    text, kb = render_gol_leaderboard(uid, top, names, rank, total, registry, totals)
    await query.message.edit_text(text, reply_markup=kb)
    await query.answer()


def render_total_leaderboard(
    user_id: int,
    top: Sequence[tuple[int, int]],
    names: dict[int, str],
    user_rank: int,
    total_players: int,
    registry: EmojiRegistry,
) -> tuple[str, InlineKeyboardMarkup]:
    lines = [
        f"{registry.text('trophy')} <b>جدول کلی میوویا — جمع تمام بازیها</b>\n",
        "لیگبندی بر اساس مجموع ریتینگ دوز + گل یا پوچ + بازیهای گروهی:\n",
    ]
    medals = ["🥇", "🥈", "🥉"]
    if not top:
        lines.append("<i>هنوز هیچ مسابقهای ثبت نشده است.</i>")
    else:
        for idx, (uid, score) in enumerate(top, start=1):
            badge = medals[idx - 1] if idx <= 3 else f"<b>{idx}.</b>"
            league_obj = get_league(score)
            name = html.escape(names.get(uid, "بازیکن ناشناس"))
            me_tag = " (شما)" if uid == user_id else ""
            lines.append(
                f"{badge} <b>{name}</b>{me_tag} — <b>{score:,}</b> امتیاز "
                f"{league_obj.get_badge(registry)}"
            )
    lines.append("\n──────────────")
    lines.append(f"{em('pin')} <b>رتبه شما:</b> #{user_rank} از {max(1, total_players):,} بازیکن")
    kb = KB(registry)
    kb.button(
        "بازی دوز (XO)",
        callback_data=GameCB(action="diff").pack(),
        style=Style.CONFIRM,
        emoji_slot="game",
    )
    kb.button(
        "گل یا پوچ",
        callback_data=GameCB(action="gol").pack(),
        style=Style.CONFIRM,
        emoji_slot="fist",
    )
    kb.button(
        "جدول دوز (XO)",
        callback_data=GameCB(action="lb").pack(),
        style=Style.PRIMARY,
        emoji_slot="trophy",
    )
    kb.button(
        "جدول گل یا پوچ",
        callback_data=GameCB(action="gol_lb").pack(),
        style=Style.PRIMARY,
        emoji_slot="fist",
    )
    kb.button(
        "مرکز بازیها",
        callback_data=GameCB(action="menu").pack(),
        style=Style.NEUTRAL,
        emoji_slot="back",
    )
    kb.adjust(2, 2, 1)
    return "\n".join(lines), kb.as_markup()


@router.callback_query(GameCB.filter(F.action == "total_lb"))
async def cb_total_leaderboard(
    query: CallbackQuery, callback_data: GameCB, session: AsyncSession
) -> None:
    if not isinstance(query.message, Message) or not query.from_user:
        return
    repo = TotalScoreRepo(session)
    top = await repo.top(10)
    uid = query.from_user.id
    rank = await repo.rank_of(uid)
    total = len(await repo.totals())
    users = UserRepo(session)
    names: dict[int, str] = {}
    for player_id, _ in top:
        user = await users.get(player_id)
        if user:
            names[player_id] = user.full_name
    me = await users.get(uid)
    if me:
        names[uid] = me.full_name
    registry = await ready_registry()
    text, kb = render_total_leaderboard(uid, top, names, rank, total, registry)
    await query.message.edit_text(text, reply_markup=kb)
    await query.answer()
