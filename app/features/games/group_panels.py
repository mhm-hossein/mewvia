"""Persian game panels for Telegram groups."""

from __future__ import annotations

from html import escape
from typing import Any

from aiogram.types import InlineKeyboardMarkup

from app.callbacks import GroupGameCB
from app.core.units import fa_digits
from app.features.games.connect_four import COLS
from app.features.games.connect_four_panel import board_lines
from app.features.games.group_rules import (
    GAME_LABELS,
    GAME_RULES,
    GOL_LABELS,
    GOL_ROUNDS,
    RPS_LABELS,
    has_moves,
    pack_action,
)
from app.ui.emoji import EmojiRegistry
from app.ui.kb import KB
from app.ui.styles import Style

_ICONS = {"xo": "xo_x", "gol": "gol_ball", "rps": "game", "four": "game"}


def render_menu(registry: EmojiRegistry) -> tuple[str, InlineKeyboardMarkup]:
    lines = [
        f"{registry.text('game')} <b>بازی‌های گروهی</b>",
        "بازی رو انتخاب کن، منتظر نفر دوم بمون و بعد «شروع بازی» رو بزن.",
        "",
    ]
    keyboard = KB(registry)
    for game, label in GAME_LABELS.items():
        lines.append(f"• <b>{label}</b> — {GAME_RULES[game]}")
        keyboard.button(
            label,
            callback_data=GroupGameCB(action="create", game=game).pack(),
            style=Style.PRIMARY,
            emoji_slot=_ICONS[game],
        )
    keyboard.button(
        "رتبه‌بندی گروه",
        callback_data=GroupGameCB(action="lb", game="all", cell=0).pack(),
        emoji_slot="trophy",
    ).adjust(2, 2, 1)
    return "\n".join(lines), keyboard.as_markup()


def _lobby(lines: list[str], keyboard: KB, state: dict[str, Any], names: list[str]) -> None:
    lines.extend([f"👑 میزبان: <b>{names[0]}</b>", GAME_RULES[state["game"]], ""])
    if len(names) == 1:
        lines.append("👥 منتظر بازیکن دوم… برای ورود دکمهٔ زیر رو بزن.")
        keyboard.button(
            "ورود به بازی",
            callback_data=pack_action(state, "join"),
            style=Style.CONFIRM,
            emoji_slot="add",
        )
    else:
        lines.extend([f"👤 بازیکن دوم: <b>{names[1]}</b>", "میزبان می‌تونه بازی رو شروع کنه."])
        keyboard.button(
            "شروع بازی",
            callback_data=pack_action(state, "start"),
            style=Style.CONFIRM,
            emoji_slot="ok",
        ).button("خروج بازیکن دوم", callback_data=pack_action(state, "leave"))
    keyboard.button("لغو دعوت", callback_data=pack_action(state, "cancel"), style=Style.DESTRUCTIVE)
    keyboard.button(
        "جدول گروه", callback_data=pack_action(state, "lb", cell=0), emoji_slot="trophy"
    ).adjust(1, 2, 1)


def _xo(
    lines: list[str], keyboard: KB, state: dict[str, Any], names: list[str], finished: bool
) -> None:
    lines.append(f"❌ <b>{names[0]}</b> در برابر ⭕ <b>{names[1]}</b>")
    if not finished:
        turn = state["turn"]
        mark = ("❌", "⭕")[turn]
        lines.extend(["", f"نوبت {mark} <b>{names[turn]}</b>؛ یه خانهٔ خالی رو بزن."])
    for cell, mark in enumerate(state["board"]):
        empty = mark == "_"
        label = {"X": "❌", "O": "⭕", "_": "▫️"}[mark]
        playable = empty and not finished
        if playable:
            label = fa_digits(str(cell + 1))
        winning_cell = cell in state.get("winning_cells", ())
        keyboard.button(
            label,
            callback_data=pack_action(state, "move", cell=cell) if playable else "noop",
            style=Style.CONFIRM if winning_cell else Style.PRIMARY if playable else Style.NEUTRAL,
        )


def _gol(
    lines: list[str], keyboard: KB, state: dict[str, Any], names: list[str], finished: bool
) -> None:
    turn, phase = state["round"], state["phase"]
    hider = (turn - 1) % 2
    guesser = 1 - hider
    points = state["points"]
    lines.extend(
        [
            f"دست <b>{fa_digits(str(turn))}</b> از {fa_digits(str(GOL_ROUNDS))}",
            f"{names[0]}: <b>{fa_digits(str(points[0]))}</b> | "
            f"{names[1]}: <b>{fa_digits(str(points[1]))}</b>",
            "",
        ]
    )
    if phase == "reveal":
        hidden, guess = state["hidden"], state["guess"]
        result = (
            f"گل در <b>{GOL_LABELS[hidden]}</b> بود."
            if hidden != "empty"
            else "هر دو دست خالی بود."
        )
        lines.extend(
            [
                f"🌼 {result}",
                f"حدس {names[guesser]}: <b>{GOL_LABELS[guess]}</b>",
                f"✅ حدس درست؛ ۲ امتیاز برای <b>{names[guesser]}</b>."
                if hidden == guess
                else f"❌ حدس اشتباه؛ ۱ امتیاز برای <b>{names[hider]}</b>.",
            ]
        )
        if not finished:
            keyboard.button(
                "دست بعدی",
                callback_data=pack_action(state, "next"),
                style=Style.CONFIRM,
                emoji_slot="next",
            )
        return

    if finished:
        return
    if phase == "hide":
        lines.append(f"🔒 <b>{names[hider]}</b>، گل رو مخفی کن یا بلوف بزن.")
    else:
        lines.append(f"🎯 <b>{names[guesser]}</b>، حدس بزن گل کجاست؛ شاید هر دو دست خالی باشه.")
    for side, label in GOL_LABELS.items():
        if side == "empty":
            label = "دستم خالیه (بلوف)" if phase == "hide" else "پوچ! هر دو خالیه"
        keyboard.button(
            label,
            callback_data=pack_action(state, f"gol_{phase}", side=side),
            style=Style.PRIMARY,
            emoji_slot="fist" if side != "empty" else "gol_hidden",
        )


def _rps(
    lines: list[str], keyboard: KB, state: dict[str, Any], names: list[str], finished: bool
) -> None:
    if finished:
        if "resigned" in state:
            return
        for name, player in zip(names, state["players"], strict=True):
            lines.append(f"<b>{name}</b> — {RPS_LABELS[state['picks'][str(player['id'])]]}")
    else:
        lines.extend(
            [
                f"<b>{names[0]}</b> در برابر <b>{names[1]}</b>",
                "",
                "هر نفر یه گزینه بزنه؛ تا هر دو انتخاب نکنن، هیچ انتخابی نمایش داده نمی‌شه.",
                f"انتخاب‌های ثبت‌شده: <b>{fa_digits(str(len(state['picks'])))}</b> از ۲",
            ]
        )
        for pick, label in RPS_LABELS.items():
            keyboard.button(
                label, callback_data=pack_action(state, "rps_pick", side=pick), style=Style.PRIMARY
            )


def _four(
    lines: list[str], keyboard: KB, state: dict[str, Any], names: list[str], finished: bool
) -> None:
    lines.append(f"🔴 <b>{names[0]}</b> در برابر 🔵 <b>{names[1]}</b>")
    if not finished:
        turn = state["turn"]
        lines.append(f"نوبت {('🔴', '🔵')[turn]} <b>{names[turn]}</b>؛ یک ستون انتخاب کن.")
        if "last_drop" in state:
            column = fa_digits(str(state["last_drop"] % COLS + 1))
            lines.append(f"آخرین حرکت: ستون {column}")
    board = state["board"]
    lines.extend(board_lines(board, state.get("winning_cells", ())))
    if not finished:
        for col in range(COLS):
            full = board[col] != "_"
            keyboard.button(
                "×" if full else fa_digits(str(col + 1)),
                callback_data="noop" if full else pack_action(state, "drop", cell=col),
                style=Style.NEUTRAL if full else Style.PRIMARY,
            )


def render_match(
    state: dict[str, Any], registry: EmojiRegistry, *, allow_rematch: bool = True
) -> tuple[str, InlineKeyboardMarkup]:
    game = state["game"]
    names = [escape(str(p["name"] or "بازیکن")) for p in state["players"]]
    lines = [f"{registry.text(_ICONS[game])} <b>{GAME_LABELS[game]}</b>", ""]
    keyboard = KB(registry)
    if state["status"] == "lobby":
        _lobby(lines, keyboard, state, names)
        return "\n".join(lines), keyboard.as_markup()

    finished = state["status"] == "ended"
    rows: tuple[int, ...]
    if game == "xo":
        _xo(lines, keyboard, state, names, finished)
        rows = (3, 3, 3)
    elif game == "four":
        _four(lines, keyboard, state, names, finished)
        rows = (COLS,) if not finished else ()
    elif game == "gol":
        _gol(lines, keyboard, state, names, finished)
        rows = () if finished else (1,) if state["phase"] == "reveal" else (2, 1)
    else:
        _rps(lines, keyboard, state, names, finished)
        rows = (3,) if not finished else ()
    if finished:
        winner = state["winner"]
        if "resigned" in state:
            lines.extend(["", f"🚩 <b>{names[state['resigned']]}</b> انصراف داد."])
        lines.extend(
            [
                "",
                "🤝 <b>مساوی شد!</b>" if winner is None else f"🏆 <b>{names[winner]}</b> برنده شد!",
            ]
        )
        for name, delta, total in zip(names, state["deltas"], state["ratings"], strict=True):
            change = fa_digits(f"{delta:+d}" if delta else "0")
            lines.append(f"• {name}: <b>{fa_digits(str(total))}</b> امتیاز گروهی ({change})")
        if allow_rematch:
            keyboard.button(
                "دوباره بازی کنیم",
                callback_data=pack_action(state, "rematch"),
                style=Style.CONFIRM,
                emoji_slot="repeat",
            )
        else:
            keyboard.button(
                "فهرست بازی‌ها", callback_data=GroupGameCB(action="menu", game="all").pack()
            )
        keyboard.button(
            "جدول گروه", callback_data=pack_action(state, "lb", cell=0), emoji_slot="trophy"
        )
        keyboard.adjust(*rows, 2)
    else:
        started = has_moves(state)
        keyboard.button(
            "انصراف (ثبت باخت)" if started else "لغو بازی",
            callback_data=pack_action(state, "resign" if started else "cancel"),
            style=Style.DESTRUCTIVE,
        )
        keyboard.adjust(*rows, 1)
    return "\n".join(lines), keyboard.as_markup()
