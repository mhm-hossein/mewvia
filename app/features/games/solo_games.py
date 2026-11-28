"""Private, unranked Rock–Paper–Scissors and Connect Four."""

from __future__ import annotations

import asyncio
import json
from secrets import choice
from typing import NotRequired, TypedDict, cast
from uuid import uuid4
from weakref import WeakValueDictionary

from aiogram.types import InlineKeyboardMarkup
from redis.exceptions import RedisError

from app.cache.fallback import FallbackCache
from app.cache.redis import get_redis
from app.callbacks.game import SoloGameCB
from app.core.logging import get_logger
from app.core.units import fa_digits
from app.features.games.connect_four import (
    COLS,
    EMPTY_BOARD,
    choose_bot_column,
    drop_piece,
    winning_cells,
)
from app.features.games.connect_four_panel import board_lines
from app.features.games.group_rules import GAME_LABELS, GAME_RULES, RPS_LABELS, rps_winner
from app.ui.emoji import EmojiRegistry
from app.ui.kb import KB
from app.ui.styles import Style

GAMES = ("rps", "four")
_PICKS = tuple(RPS_LABELS)
_TTL = 2 * 60 * 60
_cache = FallbackCache(1024, _TTL)
_locks: WeakValueDictionary[str, asyncio.Lock] = WeakValueDictionary()
log = get_logger(__name__)


class Match(TypedDict):
    game: str
    match_id: str
    revision: int
    finished: bool
    winner: int | None
    closed: NotRequired[bool]
    bot_pick: NotRequired[str]
    player_pick: NotRequired[str]
    board: NotRequired[str]
    line: NotRequired[list[int]]
    bot_col: NotRequired[int]


def key_for(chat_id: int, message_id: int) -> str:
    return f"mewvia:solo-game:{chat_id}:{message_id}"


def lock(key: str) -> asyncio.Lock:
    return _locks.setdefault(key, asyncio.Lock())


async def load(key: str) -> Match | None:
    cached = _cache.get(key)
    if cached is not None:
        return cast(Match, cached)
    try:
        raw = await get_redis().get(key)
        return cast(Match, json.loads(raw)) if raw else None
    except (RedisError, RuntimeError):
        return None


async def save(key: str, state: Match) -> None:
    _cache.put(key, dict(state))
    try:
        await get_redis().set(key, json.dumps(state), ex=_TTL)
    except (RedisError, RuntimeError) as exc:
        log.warning("solo_game_redis_save_failed", error=str(exc))


def new_match(game: str) -> Match:
    state = Match(game=game, match_id=uuid4().hex, revision=0, finished=False, winner=None)
    if game == "rps":
        # Commit the bot's choice before the player sees the buttons.
        state["bot_pick"] = choice(_PICKS)
    else:
        state["board"] = EMPTY_BOARD
    return state


def _drop(state: Match, col: int, player: int) -> bool:
    move = drop_piece(state["board"], col, player)
    if move is None:
        return False
    board, pos = move
    line = winning_cells(board, pos)
    state["board"] = board
    if line or "_" not in board:
        state.update({"finished": True, "winner": player if line else None, "line": line})
    return True


def play(state: Match, move: int) -> Match | None:
    if state["finished"]:
        return None
    updated = state.copy()
    if state["game"] == "rps":
        if not 0 <= move < len(_PICKS):
            return None
        pick = _PICKS[move]
        updated.update(
            {
                "player_pick": pick,
                "finished": True,
                "winner": rps_winner(pick, state["bot_pick"]),
            }
        )
    else:
        if not _drop(updated, move, 0):
            return None
        if not updated["finished"] and (col := choose_bot_column(updated["board"])) is not None:
            _drop(updated, col, 1)
            updated["bot_col"] = col
    updated["revision"] += 1
    return updated


def render(state: Match, registry: EmojiRegistry) -> tuple[str, InlineKeyboardMarkup]:
    game = state["game"]
    finished = state["finished"]
    lines = [f"{registry.text('game')} <b>{GAME_LABELS[game]} — مقابل میوویا</b>", ""]
    kb = KB(registry)
    rows: tuple[int, ...] = ()

    def callback(action: str, move: int = -1) -> str:
        return SoloGameCB(
            action=action,
            game=game,
            match_id=state["match_id"],
            revision=state["revision"],
            move=move,
        ).pack()

    if game == "rps":
        if finished:
            lines.extend(
                [
                    f"شما: {RPS_LABELS[state['player_pick']]}",
                    f"میوویا: {RPS_LABELS[state['bot_pick']]}",
                ]
            )
        else:
            lines.extend([GAME_RULES[game], "میوویا انتخابش رو کرده؛ حالا نوبت توست."])
            for move, label in enumerate(RPS_LABELS.values()):
                kb.button(label, callback_data=callback("move", move), style=Style.PRIMARY)
            rows = (3,)
    else:
        board = state["board"]
        lines.append("🔴 شما در برابر 🔵 میوویا")
        if "bot_col" in state:
            lines.append(f"آخرین حرکت میوویا: ستون {fa_digits(str(state['bot_col'] + 1))}")
        if not finished:
            lines.append("نوبت توست؛ یک ستون انتخاب کن.")
            if state["revision"] == 0:
                lines.append(GAME_RULES[game])
            for col in range(COLS):
                full = board[col] != "_"
                kb.button(
                    "×" if full else fa_digits(str(col + 1)),
                    callback_data="noop" if full else callback("move", col),
                    style=Style.NEUTRAL if full else Style.PRIMARY,
                )
            rows = (COLS,)
        lines.extend(board_lines(board, state.get("line", ())))

    if finished:
        result = {None: "🤝 مساوی شد!", 0: "🏆 برنده شدی!", 1: "🤖 میوویا برد!"}
        lines.extend(["", f"<b>{result[state['winner']]}</b>"])
        kb.button("بازی دوباره", callback_data=callback("again"), style=Style.CONFIRM)
    lines.append("<i>این بازی تمرینی است و امتیاز رتبه‌بندی ندارد.</i>")
    kb.button("مرکز بازی‌ها", callback_data=callback("menu"), emoji_slot="back")
    kb.adjust(*rows, 2 if finished else 1)
    return "\n".join(lines), kb.as_markup()
