"""Games & Entertainment feature router (Tic-Tac-Toe, Leaderboards, Leagues)."""

from __future__ import annotations

import html
import json
from collections.abc import Sequence
from typing import Any

from aiogram import F, Router
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.cache.fallback import FallbackCache
from app.cache.redis import get_redis
from app.callbacks import MenuCB, NoopCB
from app.callbacks.game import GameCB
from app.config import get_settings
from app.core.logging import get_logger
from app.db.models.game_stat import GameStat
from app.db.models.user import User
from app.db.repo.game import GameRepo
from app.db.repo.user import UserRepo
from app.features.games.leagues import format_league_progress_bar, get_league
from app.features.games.widget import send_xo_widget
from app.features.games.xo_engine import (
    calculate_score_delta,
    check_winner,
    find_best_move,
)
from app.features.registry import Feature, register
from app.ui.emoji import EmojiRegistry, get_emoji_registry
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
) -> tuple[str, InlineKeyboardMarkup]:
    league = get_league(stat.score)
    u_name = html.escape(user.full_name)

    text = (
        f"{registry.text('game')} <b>مرکز بازی و مسابقات میوویا</b>\n\n"
        f"👤 بازیکن: <b>{u_name}</b>\n"
        f"{league.get_badge(registry)} لیگ: <b>{league.name}</b> — <i>«{league.title}»</i>\n"
        f"{registry.text('trophy')} ریتینگ: <b>{stat.score:,}</b> امتیاز\n"
        f"📍 رتبه جهانی: <b>#{rank}</b> (از {max(1, total_players):,} بازیکن)\n\n"
        f"📊 <b>کارنامه مسابقات:</b>\n"
        f"• بردها: <b>{stat.wins}</b> | باخت‌ها: <b>{stat.losses}</b> | مساوی: <b>{stat.draws}</b>\n"
        f"• نرخ پیروزی: <b>{stat.win_rate}%</b>\n"
        f"• زنجیره برد فعلی: <b>{stat.current_streak}</b> {registry.text('streak_fire')} "
        f"(بهترین: {stat.max_streak})\n\n"
        f"یک گزینه را انتخاب کنید:"
    )

    kb = KB(registry)
    # Row 1: Main Game
    kb.button(
        "بازی دوز (XO)",
        callback_data=GameCB(action="diff").pack(),
        style=Style.CONFIRM,
        emoji_slot="game",
    )
    kb.adjust(1)

    # Row 2: Competition
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
    kb.adjust(1, 2)

    # Row 3: Rules & Math
    kb.button(
        "قوانین و راهنمای امتیازدهی",
        callback_data=GameCB(action="rules").pack(),
        style=Style.NEUTRAL,
        emoji_slot="file",
    )
    # Row 4: Back to Main Menu
    kb.button(
        "بازگشت به منوی اصلی",
        callback_data=MenuCB(target="root").pack(),
        style=Style.NEUTRAL,
        emoji_slot="back",
    )
    kb.adjust(1, 2, 1, 1)

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

    if last_outcome == "win":
        status_line = (
            f"\n🎉 <b>تبریک فراوان! شما برنده شدید!</b>\n"
            f"{registry.text('streak_fire')} <b>+{score_delta} امتیاز</b> به ریتینگ شما اضافه شد!"
        )
    elif last_outcome == "loss":
        delta_str = (
            f"{score_delta} امتیاز"
            if score_delta != 0
            else "بدون کسر امتیاز (حاشیه امن برنز)"
        )
        status_line = (
            f"\n🤖 <b>میوویا پیروز شد!</b>\n"
            f"🔻 <b>{delta_str}</b> از ریتینگ شما ثبت شد."
        )
    elif last_outcome == "draw":
        status_line = (
            f"\n🤝 <b>بازی مساوی شد!</b>\n"
            f"✨ <b>+{score_delta} امتیاز</b> ثبت شد."
        )
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
                registry.fallback("xo_x"),
                callback_data=NoopCB().pack(),
                style=Style.PRIMARY,
                emoji_slot="xo_x",
            )
        elif char == "O":
            kb.button(
                registry.fallback("xo_o"),
                callback_data=NoopCB().pack(),
                style=Style.DESTRUCTIVE,
                emoji_slot="xo_o",
            )
        else:
            if is_finished:
                kb.button(
                    registry.fallback("xo_empty"),
                    callback_data=NoopCB().pack(),
                    style=Style.NEUTRAL,
                    emoji_slot="xo_empty",
                )
            else:
                kb.button(
                    registry.fallback("xo_empty"),
                    callback_data=GameCB(action="move", diff=diff, cell=i, b=board).pack(),
                    style=Style.NEUTRAL,
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
            league_obj = get_league(s.score)
            p_name = "بازیکن ناشناس"
            if s.user:
                p_name = s.user.full_name
            is_me = (s.telegram_id == user_id)
            me_tag = " (شما)" if is_me else ""
            name_escaped = html.escape(p_name)
            lines.append(
                f"{badge} <b>{name_escaped}</b>{me_tag} — "
                f"<b>{s.score:,}</b> امتیاز {league_obj.get_badge(registry)} "
                f"[برد: {s.wins} | باخت: {s.losses}]"
            )

    lines.append("\n──────────────")
    lines.append(f"📍 <b>رتبه شما:</b> #{user_rank} از {max(1, total_players):,} بازیکن")

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
        "مرکز بازی‌ها",
        callback_data=GameCB(action="menu").pack(),
        style=Style.NEUTRAL,
        emoji_slot="back",
    )
    kb.adjust(1, 2, 1)

    return "\n".join(lines), kb.as_markup()


def render_my_league(
    user: User,
    stat: GameStat,
    rank: int,
    total_players: int,
    registry: EmojiRegistry,
) -> tuple[str, InlineKeyboardMarkup]:
    league = get_league(stat.score)
    bar_str = format_league_progress_bar(stat.score)
    u_name = html.escape(user.full_name)

    text = (
        f"{registry.text('medal')} <b>شناسنامه لیگ و وضعیت شما</b>\n\n"
        f"👤 بازیکن: <b>{u_name}</b>\n"
        f"{league.get_badge(registry)} لیگ فعلی: <b>{league.name}</b>\n"
        f"🏷 لقب افتخاری: <b>«{league.title}»</b>\n"
        f"{registry.text('trophy')} ریتینگ: <b>{stat.score:,}</b> امتیاز "
        f"(رتبه #{rank} از {max(1, total_players):,})\n\n"
        f"📈 <b>مسیر ارتقا به لیگ بعدی:</b>\n"
        f"<code>{bar_str}</code>\n\n"
        f"📊 <b>آمار تفصیلی:</b>\n"
        f"• کل مسابقات: <b>{stat.total_games}</b>\n"
        f"• پیروزی‌ها: <b>{stat.wins}</b> (نرخ برد: {stat.win_rate}%)\n"
        f"• شکست‌ها: <b>{stat.losses}</b> | تساوی‌ها: <b>{stat.draws}</b>\n"
        f"• زنجیره برد پی‌درپی فعلی: <b>{stat.current_streak}</b> {registry.text('streak_fire')}\n"
        f"• بالاترین رکورد پیروزی متوالی: <b>{stat.max_streak}</b> ✨\n\n"
        f"🏅 <b>سطوح نردبان لیگ‌های میوویا:</b>\n"
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
        "🎯 <b>ضرایب امتیاز بر اساس سطح دشواری:</b>\n"
        "• <b>سطح آسان 🟢:</b> برد +۱۵ | مساوی +۲ | باخت -۵\n"
        "• <b>سطح متوسط 🟡:</b> برد +۲۵ | مساوی +۵ | باخت -۱۰\n"
        "• <b>سطح سخت 🔴:</b> برد +۴۰ | مساوی +۱۰ | باخت -۱۵\n\n"
        f"{registry.text('streak_fire')} <b>پاداش زنجیره برد (Streak Bonus):</b>\n"
        "با هر برد متوالی، به ازای هر برد +۲ امتیاز پاداش ویژه دریافت می‌کنید!\n\n"
        "🛡 <b>حاشیه امن برنز:</b>\n"
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


@router.message(
    F.chat.type == ChatType.PRIVATE,
    ReplyTextFilter(lambda: t("menu.games")),
)
async def reply_games(message: Message, session: AsyncSession) -> None:
    if not message.from_user:
        return
    registry = await ready_registry()
    user_repo = UserRepo(session)
    user = await user_repo.upsert_from_telegram(message.from_user)

    game_repo = GameRepo(session)
    stat = await game_repo.get_or_create(user.telegram_id)
    rank = await game_repo.get_user_rank(user.telegram_id)
    total = await game_repo.get_total_players()

    text, reply_markup = render_games_hub(user, stat, rank, total, registry)
    await message.reply(text, reply_markup=reply_markup)


@router.callback_query(MenuCB.filter(F.target == "games"))
async def menu_games(query: CallbackQuery, session: AsyncSession) -> None:
    if not query.from_user or not isinstance(query.message, Message):
        return
    await query.answer()
    registry = await ready_registry()
    user_repo = UserRepo(session)
    user = await user_repo.upsert_from_telegram(query.from_user)

    game_repo = GameRepo(session)
    stat = await game_repo.get_or_create(user.telegram_id)
    rank = await game_repo.get_user_rank(user.telegram_id)
    total = await game_repo.get_total_players()

    text, reply_markup = render_games_hub(user, stat, rank, total, registry)
    try:
        await query.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        pass


@router.callback_query(GameCB.filter(F.action == "menu"))
async def cb_games_menu(query: CallbackQuery, session: AsyncSession) -> None:
    if not query.from_user or not isinstance(query.message, Message):
        return
    await query.answer()
    registry = await ready_registry()
    user_repo = UserRepo(session)
    user = await user_repo.upsert_from_telegram(query.from_user)

    game_repo = GameRepo(session)
    stat = await game_repo.get_or_create(user.telegram_id)
    rank = await game_repo.get_user_rank(user.telegram_id)
    total = await game_repo.get_total_players()

    text, reply_markup = render_games_hub(user, stat, rank, total, registry)
    try:
        await query.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        pass


@router.callback_query(GameCB.filter(F.action.in_({"diff", "xo", "reset"})))
async def cb_start_xo_widget(
    query: CallbackQuery, callback_data: GameCB, session: AsyncSession
) -> None:
    if not query.from_user or not isinstance(query.message, Message):
        return
    await query.answer("🎮 در حال ارسال ویجت گرافیکی بازی دوز…")
    settings = get_settings()
    sent = await send_xo_widget(
        query.message.chat.id, settings.bot_token, settings.telegram_api_url
    )
    if sent:
        return

    # The graphical widget is unavailable (old client / Bot API without
    # sendWidget): fall back to the classic inline board instead of leaving the
    # user with an error. The board is tracked server-side by cb_handle_move.
    log.warning("xo_widget_unavailable_using_inline_board", chat_id=query.message.chat.id)
    diff = callback_data.diff if callback_data.diff in _VALID_DIFFS else "med"
    try:
        registry = await ready_registry()
        user = await UserRepo(session).upsert_from_telegram(query.from_user)
        stat = await GameRepo(session).get_or_create(user.telegram_id)
        text, reply_markup = render_xo_board(user, stat, diff, "_" * 9, registry)
        board_msg = await query.message.answer(text, reply_markup=reply_markup)
        if board_msg is not None:
            await save_xo_session(
                query.message.chat.id, board_msg.message_id, "_" * 9, diff
            )
    except Exception as exc:
        log.error("xo_inline_fallback_failed", error=str(exc))
        await query.message.answer(
            "⚠️ ارسال ویجت تلگرام ناموفق بود. مطمئن شوید از نسخه جدید تلگرام استفاده می‌کنید."
        )


@router.callback_query(GameCB.filter(F.action == "move"))
async def cb_handle_move(
    query: CallbackQuery, callback_data: GameCB, session: AsyncSession
) -> None:
    if not query.from_user or not isinstance(query.message, Message):
        return

    cell = callback_data.cell
    board = callback_data.b
    diff = callback_data.diff

    # Validate the callback payload. The board lives in a server-side session:
    # trusting `callback_data.b` let anyone fabricate an almost-won board and
    # farm leaderboard points without ever playing.
    if (
        diff not in _VALID_DIFFS
        or len(board) != 9
        or any(char not in "_XO" for char in board)
    ):
        await query.answer("درخواست نامعتبر است.", show_alert=True)
        return

    if not 0 <= cell <= 8:
        await query.answer("حرکت نامعتبر است.")
        return

    if board[cell] != "_":
        await query.answer("این خانه قبلاً انتخاب شده است!")
        return

    # Check if game already ended
    w_now, _ = check_winner(board)
    if w_now is not None:
        await query.answer("بازی به پایان رسیده است.")
        return

    chat_id = query.message.chat.id
    message_id = query.message.message_id
    xo_state = await load_xo_session(chat_id, message_id)
    if xo_state is None:
        # Boards rendered before the server-side session existed (or games
        # started from the inline menu) have no stored state. A pristine board
        # is the only payload we can accept without verification.
        if board != "_" * 9:
            await query.answer(
                "این بازی منقضی یا نامعتبر شده است. لطفاً از منوی بازی شروع کنید.",
                show_alert=True,
            )
            return
    elif xo_state.get("board") != board or xo_state.get("diff") != diff:
        await query.answer(
            "این حرکت معتبر نیست؛ صفحه بازی تغییر کرده است.", show_alert=True
        )
        return

    user_repo = UserRepo(session)
    user = await user_repo.upsert_from_telegram(query.from_user)
    game_repo = GameRepo(session)
    stat = await game_repo.get_or_create(user.telegram_id)

    # 1. Player move
    b_list = list(board)
    b_list[cell] = "X"
    board_after_player = "".join(b_list)

    # Check player win/draw
    winner, _ = check_winner(board_after_player)
    registry = await ready_registry()

    if winner == "X":
        delta = calculate_score_delta("win", diff, stat.score, stat.current_streak)
        stat = await game_repo.record_result(user.telegram_id, "win", delta)
        await drop_xo_session(chat_id, message_id)
        await query.answer("🎉 تبریک! برنده شدید!")
        text, reply_markup = render_xo_board(
            user, stat, diff, board_after_player, registry, "win", delta
        )
        try:
            await query.message.edit_text(text, reply_markup=reply_markup)
        except Exception:
            pass
        return

    if winner == "draw":
        delta = calculate_score_delta("draw", diff, stat.score, stat.current_streak)
        stat = await game_repo.record_result(user.telegram_id, "draw", delta)
        await drop_xo_session(chat_id, message_id)
        await query.answer("🤝 بازی مساوی شد!")
        text, reply_markup = render_xo_board(
            user, stat, diff, board_after_player, registry, "draw", delta
        )
        try:
            await query.message.edit_text(text, reply_markup=reply_markup)
        except Exception:
            pass
        return

    # 2. Bot move
    bot_move = find_best_move(board_after_player, diff)
    if bot_move != -1:
        b_list[bot_move] = "O"

    board_after_bot = "".join(b_list)
    winner_bot, _ = check_winner(board_after_bot)

    if winner_bot == "O":
        delta = calculate_score_delta("loss", diff, stat.score, stat.current_streak)
        stat = await game_repo.record_result(user.telegram_id, "loss", delta)
        await drop_xo_session(chat_id, message_id)
        await query.answer("🤖 میوویا برنده شد!")
        text, reply_markup = render_xo_board(
            user, stat, diff, board_after_bot, registry, "loss", delta
        )
        try:
            await query.message.edit_text(text, reply_markup=reply_markup)
        except Exception:
            pass
        return

    if winner_bot == "draw":
        delta = calculate_score_delta("draw", diff, stat.score, stat.current_streak)
        stat = await game_repo.record_result(user.telegram_id, "draw", delta)
        await drop_xo_session(chat_id, message_id)
        await query.answer("🤝 بازی مساوی شد!")
        text, reply_markup = render_xo_board(
            user, stat, diff, board_after_bot, registry, "draw", delta
        )
        try:
            await query.message.edit_text(text, reply_markup=reply_markup)
        except Exception:
            pass
        return

    # Still ongoing
    await save_xo_session(chat_id, message_id, board_after_bot, diff)
    await query.answer()
    text, reply_markup = render_xo_board(
        user, stat, diff, board_after_bot, registry, None, 0
    )
    try:
        await query.message.edit_text(text, reply_markup=reply_markup)
    except Exception:
        pass


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

    text, reply_markup = render_leaderboard(
        user_id=query.from_user.id,
        top_stats=top_stats,
        user_rank=user_rank,
        total_players=total_players,
        registry=registry,
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

    text, reply_markup = render_my_league(
        user=user,
        stat=stat,
        rank=rank,
        total_players=total_players,
        registry=registry,
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
