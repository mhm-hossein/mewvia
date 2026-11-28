"""Private game menu, Telegram interactions, and opponent rules."""

from datetime import UTC, datetime
from itertools import count
from unittest.mock import AsyncMock

import pytest
from aiogram import Dispatcher
from aiogram.enums import ChatType
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update, User
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy.ext.asyncio import AsyncSession

from app.callbacks.game import GameCB, SoloGameCB
from app.db.repo.game import GameRepo
from app.db.repo.group_game import GroupGameRepo
from app.features.games import solo_games
from app.features.games.connect_four import EMPTY_BOARD, choose_bot_column, drop_piece
from tests.test_phase1_middleware import FakeBot


@pytest.fixture
async def scene(app_dispatcher: Dispatcher):
    bot = FakeBot()
    player = User(id=942_416, is_bot=False, first_name="علی")
    private = Chat(id=player.id, type=ChatType.PRIVATE)
    updates = count(1101)

    def message(chat: Chat, message_id: int, text: str | None = None) -> Message:
        return Message(
            message_id=message_id, chat=chat, date=datetime.now(UTC), from_user=player, text=text
        )

    async def menu() -> SendMessage:
        update_id = next(updates)
        msg = message(private, update_id, "/game")
        await app_dispatcher.feed_update(bot, Update(update_id=update_id, message=msg))
        return next(call for call in reversed(bot.calls) if isinstance(call, SendMessage))

    async def tap(data: str, message_id: int, chat: Chat = private):
        update_id = next(updates)
        query = CallbackQuery(
            id=str(update_id),
            from_user=player,
            chat_instance="solo-games-test",
            data=data,
            message=message(chat, message_id),
        )
        before = len(bot.calls)
        await app_dispatcher.feed_update(bot, Update(update_id=update_id, callback_query=query))
        return bot.calls[before:]

    try:
        yield menu, tap, player.id
    finally:
        await bot.session.close()


def _button(markup: InlineKeyboardMarkup, label: str) -> str:
    for row in markup.inline_keyboard:
        for button in row:
            if label in button.text and button.callback_data is not None:
                return button.callback_data
    pytest.fail(f"No button contained {label!r}")


def _edit(calls) -> EditMessageText:
    (edit,) = [call for call in calls if isinstance(call, EditMessageText)]
    assert edit.reply_markup is not None
    return edit


async def test_private_game_menu_opens_existing_xo_and_gol(scene) -> None:
    menu, tap, _ = scene
    markup = (await menu()).reply_markup
    assert GameCB.unpack(_button(markup, "دوز")).action == "diff"
    diff = _edit(await tap(_button(markup, "دوز"), 101)).reply_markup
    assert "آسان" in _edit(await tap(_button(diff, "آسان"), 101)).text
    gol = _edit(await tap(_button(markup, "گل یا پوچ"), 102))
    assert "گل یا پوچ" in gol.text
    reveal = _edit(await tap(_button(gol.reply_markup, "دست چپ"), 102))
    assert "گل تو دست" in reveal.text and _button(reveal.reply_markup, "نوبت بعدی")


async def test_private_rps_hides_precommitted_choice_and_does_not_rank(
    scene, session: AsyncSession, monkeypatch
) -> None:
    menu, tap, uid = scene
    monkeypatch.setattr(solo_games, "choice", lambda _: "scissors")
    hub = (await menu()).reply_markup
    start = _button(hub, "سنگ، کاغذ، قیچی")
    private = _edit(await tap(start, 201))
    assert "میوویا انتخابش رو کرده" in private.text
    assert "میوویا: ✂️" not in private.text
    play = _button(private.reply_markup, "سنگ")
    assert len(play.encode()) <= 64
    end = _edit(await tap(play, 201))
    assert "میوویا: ✂️ قیچی" in end.text and "برنده شدی" in end.text
    assert "امتیاز رتبه‌بندی ندارد" in end.text
    assert not any(isinstance(call, EditMessageText) for call in await tap(play, 201))
    again = _button(end.reply_markup, "بازی دوباره")
    restarted = _edit(await tap(again, 201))
    assert SoloGameCB.unpack(_button(restarted.reply_markup, "سنگ")).match_id != (
        SoloGameCB.unpack(play).match_id
    )
    back = _edit(await tap(_button(restarted.reply_markup, "مرکز بازی‌ها"), 201))
    assert "مرکز بازی" in back.text
    stale = await tap(_button(restarted.reply_markup, "سنگ"), 201)
    assert any(isinstance(call, AnswerCallbackQuery) and "قدیمی" in call.text for call in stale)
    assert (await GameRepo(session).get_or_create(uid)).score == 1000
    assert await GroupGameRepo(session).total_players(0, "rps") == 0


async def test_private_connect_four_replies_and_rejects_stale_and_group_taps(scene) -> None:
    menu, tap, _ = scene
    start = _button((await menu()).reply_markup, "چهار در خط")
    group = Chat(id=-19912341, type=ChatType.SUPERGROUP)
    assert not any(isinstance(call, EditMessageText) for call in await tap(start, 301, group))
    game = _edit(await tap(start, 301))
    play = _button(game.reply_markup, "۱")
    assert "1️⃣2️⃣3️⃣4️⃣5️⃣6️⃣7️⃣" in game.text
    edit = _edit(await tap(play, 301))
    assert "آخرین حرکت میوویا: ستون ۴" in edit.text
    assert "🔴⚪⚪🔵⚪⚪⚪" in edit.text
    assert not any(isinstance(call, EditMessageText) for call in await tap(play, 301))
    assert not any(isinstance(call, EditMessageText) for call in await tap(play, 302))
    invalid = SoloGameCB.unpack(_button(edit.reply_markup, "۱")).model_copy(update={"move": 7})
    assert not any(isinstance(call, EditMessageText) for call in await tap(invalid.pack(), 301))


def test_connect_four_bot_wins_first_and_blocks_otherwise() -> None:
    assert choose_bot_column("_" * 35 + "OOO_XXX") == 3
    assert choose_bot_column("_" * 35 + "XXX____") == 3
    assert choose_bot_column("X" * 42) is None
    assert choose_bot_column(EMPTY_BOARD) == 3


def test_connect_four_finish_stops_other_moves() -> None:
    winning = solo_games.new_match("four")
    winning["board"] = "_" * 35 + "XXX_OOO"
    result = solo_games.play(winning, 3)
    assert result is not None and result["finished"] and result["winner"] == 0
    assert result["line"] == [35, 36, 37, 38]
    assert solo_games.play(result, 4) is None

    losing = solo_games.new_match("four")
    losing["board"] = "_" * 35 + "OOO_XX_"
    result = solo_games.play(losing, 6)
    assert result is not None and result["finished"] and result["winner"] == 1
    assert result["line"] == [35, 36, 37, 38]


def test_connect_four_bots_last_move_can_draw() -> None:
    state = solo_games.new_match("four")
    board = EMPTY_BOARD
    for turn, col in enumerate("5460066113341401303365236511420605524542"):
        dropped = drop_piece(board, int(col), turn % 2)
        assert dropped is not None
        board, _ = dropped
    assert board.count("_") == 2
    state["board"] = board
    draw = solo_games.play(state, 2)
    assert draw is not None and draw["finished"] and draw["winner"] is None
    assert draw["line"] == [] and "_" not in draw["board"]


async def test_solo_match_recovers_from_redis_and_retains_latest_during_failure(
    monkeypatch,
) -> None:
    redis = AsyncMock()
    monkeypatch.setattr(solo_games, "get_redis", lambda: redis)
    key = solo_games.key_for(123, 456)
    initial = solo_games.new_match("rps")
    await solo_games.save(key, initial)
    assert redis.set.await_args.kwargs["ex"] == 7200
    redis.get.return_value = redis.set.await_args.args[1]
    solo_games._cache.discard(key)
    assert await solo_games.load(key) == initial

    completed = solo_games.play(initial, 0)
    assert completed is not None
    redis.set.side_effect = RedisConnectionError("Redis unavailable")
    await solo_games.save(key, completed)
    assert await solo_games.load(key) == completed
    solo_games._cache.discard(key)
