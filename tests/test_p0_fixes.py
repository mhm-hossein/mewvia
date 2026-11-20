"""Regression tests for P0 bug fixes:
- BUG-01: Group FSM leakage prevention
- BUG-02: Group silence enforcement (no response to plain text or PV commands)
- BUG-03: HTML escaping in text overlay and sticker packs
- BUG-04: Instagram cookies probe support
"""

from __future__ import annotations

from typing import Any

import pytest
from aiogram import Dispatcher, html
from aiogram.enums import ChatType
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.types import CallbackQuery, Chat, Message, Update
from aiogram.types import User as TgUser
from app.callbacks import MusicSearchCB, TextOverlayCB
from app.features.stickerpack.router import StickerPackCB
from app.features.stickerpack.service import make_pack_title
from app.features.tag_editor.router import TagFieldCB
from app.media.downloader import find_cookie_file
from app.middlewares.group_guard import store_panel
from tests.test_phase1_middleware import FakeBot

GROUP_ID = -100888777666
USER_ID = 444333222


def _group_msg(message_id: int, text: str) -> Message:
    return Message(
        message_id=message_id,
        date=100000,
        chat=Chat(id=GROUP_ID, type=ChatType.SUPERGROUP),
        from_user=TgUser(id=USER_ID, is_bot=False, first_name="TestUser"),
        text=text,
    )


# ---------------------------------------------------------------------------
# BUG-02: Group silence enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "راهنما",
        "🌟 راهنما",
        "شروع مجدد",
        "🔄 شروع مجدد",
        "/help",
        "/cancel",
        "/me",
        "/ping",
        "/start",
    ],
)
@pytest.mark.asyncio
async def test_group_silence_on_chatter_and_pv_commands(
    app_dispatcher: Dispatcher, text: str
) -> None:
    """The bot MUST stay 100% silent when Persian labels or PV commands are sent in groups."""
    bot = FakeBot()
    await app_dispatcher.feed_update(
        bot,
        Update(update_id=1, message=_group_msg(1, text)),
    )
    assert len(bot.calls) == 0, f"Expected 0 calls for {text!r} in group, got: {bot.calls}"


# ---------------------------------------------------------------------------
# BUG-01: Group FSM leakage prevention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_group_interactive_buttons_reject_with_alert_and_no_state(
    app_dispatcher: Dispatcher,
) -> None:
    """Tapping multi-step text-input buttons in a group panel must NOT set FSM state."""
    bot = FakeBot()
    key = StorageKey(bot_id=bot.id, chat_id=GROUP_ID, user_id=USER_ID)

    cases = [
        ("msearch:prompt", "music_search", None, 0),
        (TextOverlayCB(action="start").pack(), "photo", "FILE_ID_1", 2000),
        ("batch:text:", "photo", "FILE_ID_2", 2000),
        (StickerPackCB(action="create").pack(), "photo", "FILE_ID_3", 2000),
        (TagFieldCB(action="field", field="title").pack(), "audio", "FILE_ID_4", 2000),
    ]

    for idx, (cb_data, kind, file_id, file_size) in enumerate(cases, 100):
        await app_dispatcher.storage.set_state(key, None)
        await app_dispatcher.storage.set_data(key, {})

        await store_panel(
            chat_id=GROUP_ID,
            message_id=idx,
            owner_id=USER_ID,
            owner_name="TestUser",
            reply_to_message_id=1,
            kind=kind,
            file_id=file_id or "",
            file_size=file_size,
        )

        panel = Message(
            message_id=idx,
            date=100000,
            chat=Chat(id=GROUP_ID, type=ChatType.SUPERGROUP),
        )
        cb = CallbackQuery(
            id=f"cb-{idx}",
            from_user=TgUser(id=USER_ID, is_bot=False, first_name="TestUser"),
            chat_instance="inst",
            message=panel,
            data=cb_data,
        )

        await app_dispatcher.feed_update(bot, Update(update_id=idx, callback_query=cb))

        # Check: FSM state must NEVER be set for a group chat!
        state = await app_dispatcher.storage.get_state(key)
        assert state is None, f"Expected state=None after {cb_data} in group, got {state!r}"

        # Check: Callback was answered with an alert
        answers = [c for c in bot.calls if type(c).__name__ == "AnswerCallbackQuery"]
        assert len(answers) > 0, f"Expected AnswerCallbackQuery for {cb_data}"
        last_answer = answers[-1]
        assert getattr(last_answer, "show_alert", False) is True


# ---------------------------------------------------------------------------
# BUG-03: HTML injection prevention
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_textoverlay_html_escaping(app_dispatcher: Dispatcher) -> None:
    """User-supplied text with HTML tags and characters must be escaped before rendering."""
    bot = FakeBot()
    key = StorageKey(bot_id=bot.id, chat_id=USER_ID, user_id=USER_ID)
    await app_dispatcher.storage.set_state(key, "TextOverlayFSM:waiting_for_text")
    await app_dispatcher.storage.set_data(
        key,
        {
            "file_id": "TEST_FILE",
            "kind": "photo",
            "file_size": 1000,
            "font": "vazir",
            "color": "white",
            "pos": "bottom",
            "size": "medium",
        },
    )

    malicious_text = "<b>INJECTION</b> & <a href='evil.com'>link</a>"
    msg = Message(
        message_id=50,
        date=100000,
        chat=Chat(id=USER_ID, type=ChatType.PRIVATE),
        from_user=TgUser(id=USER_ID, is_bot=False, first_name="Tester"),
        text=malicious_text,
    )

    await app_dispatcher.feed_update(bot, Update(update_id=50, message=msg))

    edits = [c for c in bot.calls if type(c).__name__ in ("EditMessageText", "SendMessage")]
    assert len(edits) > 0
    sent_text = getattr(edits[0], "text", "")
    # The raw unescaped HTML tag must NOT appear in the rendered message
    assert "<b>INJECTION</b>" not in sent_text
    assert html.quote(malicious_text) in sent_text


def test_stickerpack_title_branding_and_escape() -> None:
    raw_title = "<b>Pwned</b> & Co"
    branded = make_pack_title(raw_title, "MewviaBot")
    escaped = html.quote(branded)
    assert "&lt;b&gt;Pwned&lt;/b&gt; &amp; Co | @MewviaBot" == escaped


# ---------------------------------------------------------------------------
# BUG-04: Instagram cookies probe support
# ---------------------------------------------------------------------------


def test_find_cookie_file_instagram(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """find_cookie_file('instagram') must check /data/cookies/instagram.txt if it exists."""
    cookie_dir = tmp_path / "data" / "cookies"
    cookie_dir.mkdir(parents=True)
    ig_cookie = cookie_dir / "instagram.txt"
    ig_cookie.write_text("# Netscape HTTP Cookie File\n.instagram.com TRUE / TRUE 0 sessionid test\n")

    monkeypatch.setenv("COOKIE_DIR", str(cookie_dir))

    found = find_cookie_file("instagram")
    assert found is not None
