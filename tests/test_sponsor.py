"""Sponsor system unit tests.

Covers every layer of the forced-membership feature:

1. sponsor_link   — parse_sponsor_input (pure, no DB/network)
2. SponsorRepo    — full CRUD via the rolled-back DB transaction
3. sponsor_service — is_user_in_channel / get_missing_sponsors (bot mocked)
4. SponsorGateMiddleware — middleware blocks / passes through / respects admins
5. build_sponsor_join_keyboard — keyboard builder (no DB/network)
6. SponsorChannel model — repr and field defaults

DB tests skip automatically when PostgreSQL is unreachable (same pattern as the
rest of the suite — see conftest.py).
"""

from __future__ import annotations

import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import TelegramMethod
from aiogram.types import (
    CallbackQuery,
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    ChatMemberOwner,
    ChatMemberRestricted,
    Message,
    Update,
)
from aiogram.types import User as TgUser
from app.config import get_settings
from app.core.sponsor_link import (
    NUMERIC_ID_PATTERN,
    TME_PATTERN,
    USERNAME_PATTERN,
    parse_sponsor_input,
)
from app.core.sponsor_service import get_missing_sponsors, is_user_in_channel
from app.db.models.sponsor_channel import SponsorChannel
from app.db.repo.sponsor import SponsorRepo
from app.middlewares.sponsor_gate import SponsorGateMiddleware, build_sponsor_join_keyboard
from sqlalchemy.ext.asyncio import AsyncSession


def make_group_update(
    update_id: int,
    text: str | None,
    user_id: int,
    chat_type: str = "supergroup",
) -> Update:
    user = TgUser(id=user_id, is_bot=False, first_name="Test", username="tester")
    message = Message(
        message_id=update_id,
        date=datetime.datetime.now(datetime.UTC),
        chat=Chat(id=-100123456789, type=chat_type),
        from_user=user,
        text=text,
    )
    return Update(update_id=update_id, message=message)


ADMIN_ID = 1  # from conftest TEST_ENV ADMIN_IDS="1,2"


@pytest.fixture(autouse=True)
def _clear_membership_cache() -> object:
    from app.core.sponsor_service import clear_membership_cache

    clear_membership_cache()
    yield
    clear_membership_cache()


USER_ID = 999_000_001_001


class RecordingBot(Bot):
    """Minimal bot that records calls without hitting the network."""

    def __init__(self) -> None:
        super().__init__(
            token="111111:TEST-TOKEN-FOR-PYTEST",
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.calls: list[TelegramMethod[Any]] = []

    async def __call__(
        self, method: TelegramMethod[Any], request_timeout: int | None = None
    ) -> Any:
        self.calls.append(method)
        return None


def make_update(
    update_id: int,
    text: str,
    user_id: int,
) -> Update:
    user = TgUser(id=user_id, is_bot=False, first_name="Test", username="tester")
    message = Message(
        message_id=update_id,
        date=datetime.datetime.now(datetime.UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=user,
        text=text,
    )
    return Update(update_id=update_id, message=message)


def make_callback_update(
    update_id: int,
    data: str,
    user_id: int,
) -> Update:
    user = TgUser(id=user_id, is_bot=False, first_name="Test", username="tester")
    msg = Message(
        message_id=update_id,
        date=datetime.datetime.now(datetime.UTC),
        chat=Chat(id=user_id, type="private"),
        from_user=user,
        text="stub",
    )
    cb = CallbackQuery(
        id=str(update_id),
        from_user=user,
        chat_instance="ci",
        data=data,
        message=msg,
    )
    return Update(update_id=update_id, callback_query=cb)


def _make_restricted(*, is_member: bool) -> ChatMemberRestricted:
    """A RESTRICTED ChatMember. Every permission field is required by aiogram."""
    return ChatMemberRestricted.model_validate(
        {
            "status": ChatMemberStatus.RESTRICTED,
            "user": TgUser(id=42, is_bot=False, first_name="X"),
            "is_member": is_member,
            "can_send_messages": False,
            "can_send_audios": False,
            "can_send_documents": False,
            "can_send_photos": False,
            "can_send_videos": False,
            "can_send_video_notes": False,
            "can_send_voice_notes": False,
            "can_send_polls": False,
            "can_send_other_messages": False,
            "can_add_web_page_previews": False,
            "can_react_to_messages": False,
            "can_edit_tag": False,
            "can_change_info": False,
            "can_invite_users": False,
            "can_pin_messages": False,
            "can_manage_topics": False,
            "until_date": datetime.datetime.now(datetime.UTC),
        }
    )


class FakeRegistry:
    """Emoji registry stand-in.

    A MagicMock is unusable here: `custom_id()` and `fallback()` are called
    synchronously by `inline_button`, and an AsyncMock would hand back a
    coroutine that pydantic then rejects.
    """

    def __init__(self, *, premium_id: str | None = None) -> None:
        self._premium_id = premium_id
        self.loaded = False
        self.premium_refreshed = False

    async def refresh_premium_flag(self) -> bool:
        self.premium_refreshed = True
        return True

    async def load(self) -> dict[str, tuple[str | None, str]]:
        self.loaded = True
        return {}

    def custom_id(self, slot: str) -> str | None:
        return self._premium_id

    def fallback(self, slot: str) -> str:
        return "📣"

    def text(self, slot: str) -> str:
        return "🔒"

    def label(self, slot: str, text: str) -> str:
        return f"🔒 {text}"


# ===========================================================================
# 1. parse_sponsor_input — pure unit tests, no DB
# ===========================================================================


class TestParseSponsorInput:
    """parse_sponsor_input parses every expected input form correctly."""

    # --- numeric chat IDs ---

    def test_positive_numeric_id(self) -> None:
        username, chat_id = parse_sponsor_input("123456789")
        assert username is None
        assert chat_id == 123456789

    def test_negative_numeric_id(self) -> None:
        username, chat_id = parse_sponsor_input("-1001234567890")
        assert username is None
        assert chat_id == -1001234567890

    def test_numeric_id_with_whitespace(self) -> None:
        _username, chat_id = parse_sponsor_input("  -1001234567890  ")
        assert chat_id == -1001234567890

    # --- t.me public links ---

    def test_tme_link_https(self) -> None:
        username, chat_id = parse_sponsor_input("https://t.me/mychannel")
        assert username == "mychannel"
        assert chat_id is None

    def test_tme_link_http(self) -> None:
        username, chat_id = parse_sponsor_input("http://t.me/mychannel")
        assert username == "mychannel"
        assert chat_id is None

    def test_tme_link_without_scheme(self) -> None:
        username, chat_id = parse_sponsor_input("t.me/mychannel")
        assert username == "mychannel"
        assert chat_id is None

    # --- t.me invite / joinchat links ---

    def test_tme_joinchat_link(self) -> None:
        username, chat_id = parse_sponsor_input("https://t.me/joinchat/AbCdEfGhIj")
        assert username == "AbCdEfGhIj"
        assert chat_id is None

    def test_tme_plus_invite_link(self) -> None:
        username, chat_id = parse_sponsor_input("https://t.me/+AbCdEfGhIj1234")
        assert username == "+AbCdEfGhIj1234"
        assert chat_id is None

    # --- @username / bare username ---

    def test_at_username(self) -> None:
        username, chat_id = parse_sponsor_input("@mychannel")
        assert username == "mychannel"
        assert chat_id is None

    def test_bare_username(self) -> None:
        username, chat_id = parse_sponsor_input("mychannel")
        assert username == "mychannel"
        assert chat_id is None

    def test_username_with_underscores(self) -> None:
        username, chat_id = parse_sponsor_input("@my_channel_name")
        assert username == "my_channel_name"
        assert chat_id is None

    def test_username_max_length(self) -> None:
        long_name = "a" * 32  # max 32 chars
        username, _chat_id = parse_sponsor_input(f"@{long_name}")
        assert username == long_name

    # --- invalid inputs ---

    def test_too_short_username_returns_none(self) -> None:
        # USERNAME_PATTERN requires 5–32 chars
        username, chat_id = parse_sponsor_input("abcd")
        assert username is None
        assert chat_id is None

    def test_empty_string_returns_none(self) -> None:
        username, chat_id = parse_sponsor_input("")
        assert username is None
        assert chat_id is None

    def test_whitespace_only_returns_none(self) -> None:
        username, chat_id = parse_sponsor_input("   ")
        assert username is None
        assert chat_id is None

    def test_random_garbage_returns_none(self) -> None:
        username, chat_id = parse_sponsor_input("!!!not valid!!!")
        assert username is None
        assert chat_id is None

    # --- regex pattern smoke tests ---

    def test_tme_pattern_matches_expected_urls(self) -> None:
        assert TME_PATTERN.search("https://t.me/channelname")
        assert TME_PATTERN.search("t.me/channelname")
        assert not TME_PATTERN.search("https://telegram.me/channelname")
        assert not TME_PATTERN.search("telegram.me/channelname")

    def test_username_pattern_accepts_valid_names(self) -> None:
        assert USERNAME_PATTERN.match("hello_world")
        assert USERNAME_PATTERN.match("Chan12345")
        assert not USERNAME_PATTERN.match("hi")  # too short

    def test_numeric_id_pattern_matches_signed_integers(self) -> None:
        assert NUMERIC_ID_PATTERN.match("-1001234567890")
        assert NUMERIC_ID_PATTERN.match("999")
        assert not NUMERIC_ID_PATTERN.match("99abc")


# ===========================================================================
# 2. SponsorChannel model
# ===========================================================================


class TestSponsorChannelModel:
    def test_repr(self) -> None:
        ch = SponsorChannel()
        ch.id = 1
        ch.title = "My Channel"
        ch.is_active = True
        r = repr(ch)
        assert "My Channel" in r

    def test_defaults(self) -> None:
        """Column defaults are applied by the database, not by __init__."""
        ch = SponsorChannel()
        ch.channel_id = -100111
        ch.title = "Test"
        # is_active carries `default=True` / `server_default="true"`, both of
        # which resolve at INSERT time — an unflushed instance sees None.
        assert ch.is_active is None
        assert ch.username is None
        assert ch.invite_link is None


# ===========================================================================
# 3. SponsorRepo — DB tests (auto-skip when PostgreSQL is unreachable)
# ===========================================================================


async def test_repo_add_and_get_by_channel_id(session: AsyncSession) -> None:
    repo = SponsorRepo(session)
    ch = await repo.add_or_update(
        channel_id=-1001111111111,
        title="Test Channel A",
        username="testchanA",
        invite_link=None,
        is_active=True,
    )
    await session.flush()

    assert ch.channel_id == -1001111111111
    assert ch.title == "Test Channel A"
    assert ch.username == "testchanA"
    assert ch.is_active is True

    fetched = await repo.get_by_channel_id(-1001111111111)
    assert fetched is not None
    assert fetched.title == "Test Channel A"


async def test_repo_add_or_update_is_idempotent(session: AsyncSession) -> None:
    repo = SponsorRepo(session)
    await repo.add_or_update(channel_id=-1001222222222, title="Original", username="orig")
    await session.flush()

    updated = await repo.add_or_update(channel_id=-1001222222222, title="Updated", username="upd")
    await session.flush()

    assert updated.title == "Updated"
    assert updated.username == "upd"

    all_rows = await repo.all()
    matching = [r for r in all_rows if r.channel_id == -1001222222222]
    assert len(matching) == 1, "upsert must not create a duplicate row"


async def test_repo_get_active_channels_only_returns_active(session: AsyncSession) -> None:
    repo = SponsorRepo(session)
    await repo.add_or_update(channel_id=-1001333333333, title="Active", is_active=True)
    ch_inactive = await repo.add_or_update(
        channel_id=-1001333333334, title="Inactive", is_active=False
    )
    await session.flush()

    active = await repo.get_active_channels()
    channel_ids = [c.channel_id for c in active]
    assert -1001333333333 in channel_ids
    assert ch_inactive.channel_id not in channel_ids


async def test_repo_set_active_toggles_flag(session: AsyncSession) -> None:
    repo = SponsorRepo(session)
    ch = await repo.add_or_update(channel_id=-1001444444444, title="Toggle Me", is_active=True)
    await session.flush()

    result = await repo.set_active(ch.id, False)
    assert result is True
    assert ch.is_active is False

    result2 = await repo.set_active(ch.id, True)
    assert result2 is True
    assert ch.is_active is True


async def test_repo_set_active_returns_false_for_missing_id(session: AsyncSession) -> None:
    repo = SponsorRepo(session)
    result = await repo.set_active(999_999_999, True)
    assert result is False


async def test_repo_delete_removes_channel(session: AsyncSession) -> None:
    repo = SponsorRepo(session)
    ch = await repo.add_or_update(channel_id=-1001555555555, title="To Delete")
    await session.flush()

    deleted = await repo.delete(ch.id)
    assert deleted is True

    fetched = await repo.get_by_id(ch.id)
    assert fetched is None


async def test_repo_delete_returns_false_for_missing(session: AsyncSession) -> None:
    repo = SponsorRepo(session)
    result = await repo.delete(999_999_998)
    assert result is False


async def test_repo_count_reflects_all_rows(session: AsyncSession) -> None:
    repo = SponsorRepo(session)
    before = await repo.count()

    await repo.add_or_update(channel_id=-1001666666661, title="Count1")
    await repo.add_or_update(channel_id=-1001666666662, title="Count2")
    await session.flush()

    after = await repo.count()
    assert after == before + 2


async def test_repo_list_page_paginates(session: AsyncSession) -> None:
    repo = SponsorRepo(session)
    for i in range(5):
        await repo.add_or_update(channel_id=-(1001777777700 + i), title=f"Page{i}")
    await session.flush()

    page0 = await repo.list_page(offset=0, limit=3)
    page1 = await repo.list_page(offset=3, limit=3)
    assert len(page0) == 3
    assert len(page1) >= 2

    ids0 = {c.id for c in page0}
    ids1 = {c.id for c in page1}
    assert ids0.isdisjoint(ids1), "pages must not overlap"


async def test_repo_get_by_id_returns_none_for_missing(session: AsyncSession) -> None:
    repo = SponsorRepo(session)
    assert await repo.get_by_id(888_888_888) is None


async def test_repo_add_with_invite_link(session: AsyncSession) -> None:
    repo = SponsorRepo(session)
    ch = await repo.add_or_update(
        channel_id=-1001888888888,
        title="Invite Link Channel",
        username=None,
        invite_link="https://t.me/+SecretHash12345",
    )
    await session.flush()
    assert ch.invite_link == "https://t.me/+SecretHash12345"
    assert ch.username is None


# ===========================================================================
# 4. sponsor_service — is_user_in_channel (mocked bot)
# ===========================================================================


class TestIsUserInChannel:
    """All paths of the fail-open membership check."""

    async def _call(self, member_or_exc: Any) -> bool:
        bot = AsyncMock(spec=Bot)
        if isinstance(member_or_exc, Exception):
            bot.get_chat_member.side_effect = member_or_exc
        else:
            bot.get_chat_member.return_value = member_or_exc
        return await is_user_in_channel(bot, channel_id=-100123, user_id=42)

    async def test_member_status_returns_true(self) -> None:
        member = ChatMemberMember(
            status=ChatMemberStatus.MEMBER,
            user=TgUser(id=42, is_bot=False, first_name="X"),
        )
        assert await self._call(member) is True

    async def test_creator_status_returns_true(self) -> None:
        owner = ChatMemberOwner(
            status=ChatMemberStatus.CREATOR,
            user=TgUser(id=42, is_bot=False, first_name="X"),
            is_anonymous=False,
        )
        assert await self._call(owner) is True

    async def test_left_status_returns_false(self) -> None:
        left = ChatMemberLeft(
            status=ChatMemberStatus.LEFT,
            user=TgUser(id=42, is_bot=False, first_name="X"),
        )
        assert await self._call(left) is False

    async def test_restricted_is_member_true_returns_true(self) -> None:
        assert await self._call(_make_restricted(is_member=True)) is True

    async def test_restricted_is_member_false_returns_false(self) -> None:
        assert await self._call(_make_restricted(is_member=False)) is False

    async def test_telegram_api_error_fail_open(self) -> None:
        fake_method = MagicMock()
        exc = TelegramBadRequest(method=fake_method, message="Forbidden: bot is not a member")
        assert await self._call(exc) is True

    async def test_generic_exception_fail_open(self) -> None:
        assert await self._call(RuntimeError("unexpected")) is True


# ===========================================================================
# 5. sponsor_service — get_missing_sponsors (mocked)
# ===========================================================================


class TestGetMissingSponsors:
    """get_missing_sponsors filters correctly based on membership results."""

    async def _run(
        self,
        channels: list[SponsorChannel],
        membership_map: dict[int, bool],
    ) -> list[SponsorChannel]:
        bot = AsyncMock(spec=Bot)
        session = AsyncMock(spec=AsyncSession)
        session.execute.return_value = MagicMock()
        session.execute.return_value.scalar_one_or_none.return_value = None

        repo_mock = AsyncMock()
        repo_mock.get_active_channels.return_value = channels

        async def fake_membership(bot_arg: Any, channel_id: int, user_id: int) -> bool:
            return membership_map.get(channel_id, True)

        with (
            patch("app.core.sponsor_service.SponsorRepo", return_value=repo_mock),
            patch(
                "app.core.sponsor_service.is_user_in_channel",
                side_effect=fake_membership,
            ),
        ):
            return await get_missing_sponsors(bot, session, user_id=99)

    @staticmethod
    def _make_channel(channel_id: int, title: str) -> SponsorChannel:
        ch = SponsorChannel()
        ch.id = abs(channel_id) % 10000
        ch.channel_id = channel_id
        ch.title = title
        ch.is_active = True
        ch.username = None
        ch.invite_link = None
        return ch

    async def test_no_active_channels_returns_empty(self) -> None:
        result = await self._run([], {})
        assert result == []

    async def test_user_in_all_channels_returns_empty(self) -> None:
        ch1 = self._make_channel(-100111, "Chan1")
        ch2 = self._make_channel(-100222, "Chan2")
        result = await self._run([ch1, ch2], {-100111: True, -100222: True})
        assert result == []

    async def test_user_missing_some_channels(self) -> None:
        ch1 = self._make_channel(-100111, "Chan1")
        ch2 = self._make_channel(-100222, "Chan2")
        ch3 = self._make_channel(-100333, "Chan3")
        result = await self._run(
            [ch1, ch2, ch3],
            {-100111: True, -100222: False, -100333: False},
        )
        assert len(result) == 2
        missing_ids = {c.channel_id for c in result}
        assert missing_ids == {-100222, -100333}

    async def test_user_missing_all_channels(self) -> None:
        ch1 = self._make_channel(-100111, "Chan1")
        ch2 = self._make_channel(-100222, "Chan2")
        result = await self._run([ch1, ch2], {-100111: False, -100222: False})
        assert len(result) == 2


class TestLeaveTracking:
    """TTL + leave detection + exactly-once claim of warm invites."""

    async def _run_raw(
        self,
        channels: list[SponsorChannel],
        membership_map: dict[int, bool],
        session: AsyncSession,
        user_id: int = 99,
    ) -> list[SponsorChannel]:
        from app.core.sponsor_service import get_missing_sponsors

        bot = AsyncMock(spec=Bot)
        repo_mock = AsyncMock()
        repo_mock.get_active_channels.return_value = channels

        async def fake_membership(bot_arg: Any, channel_id: int, user_id_: int) -> bool:
            return membership_map.get(channel_id, True)

        with (
            patch("app.core.sponsor_service.SponsorRepo", return_value=repo_mock),
            patch(
                "app.core.sponsor_service.is_user_in_channel",
                side_effect=fake_membership,
            ),
        ):
            return await get_missing_sponsors(bot, session, user_id=user_id)

    @staticmethod
    def _make_channel(channel_id: int, title: str) -> SponsorChannel:
        ch = SponsorChannel()
        ch.id = abs(channel_id) % 10000
        ch.channel_id = channel_id
        ch.title = title
        ch.is_active = True
        ch.username = None
        ch.invite_link = None
        return ch

    def test_membership_ttl_is_30_seconds(self) -> None:
        from app.core.sponsor_service import _MEMBERSHIP_TTL

        assert _MEMBERSHIP_TTL == 30.0

    async def test_leave_detected_after_seen_member(self, session: AsyncSession) -> None:
        from app.core.sponsor_service import (
            _MEMBERSHIP_CACHE,
            claim_recent_leaves,
        )

        ch = self._make_channel(-100444, "LeaveChan")
        # First seen as member, then missing -> leave event recorded.
        assert await self._run_raw([ch], {-100444: True}, session) == []
        # Simulate TTL expiry: the second check must be fresh, not cached.
        _MEMBERSHIP_CACHE.clear()
        assert await self._run_raw([ch], {-100444: False}, session) == [ch]
        leaves = claim_recent_leaves(99)
        assert [c.channel_id for c in leaves] == [-100444]
        # Exactly-once: second claim is empty.
        assert claim_recent_leaves(99) == []

    async def test_fresh_bypass_skips_stale_negative_cache(self, session: AsyncSession) -> None:
        """The «عضو شدم» button must not read the gate's stale verdict.

        Regression: gate blocks (False cached 30s) -> user joins -> taps
        check -> without fresh=True the cached False wins and the button
        lies "still not a member".
        """
        from app.core.sponsor_service import _MEMBERSHIP_CACHE

        ch = self._make_channel(-100666, "FreshChan")
        assert await self._run_raw([ch], {-100666: True}, session) == []
        _MEMBERSHIP_CACHE.clear()
        # Gate blocks and caches False.
        assert await self._run_raw([ch], {-100666: False}, session) == [ch]
        # User joins; the button's fresh re-check must see it immediately.
        assert await self._run_raw_fresh([ch], {-100666: True}, session) == []

    async def _run_raw_fresh(
        self,
        channels: list[SponsorChannel],
        membership_map: dict[int, bool],
        session: AsyncSession,
        user_id: int = 99,
    ) -> list[SponsorChannel]:
        from app.core.sponsor_service import get_missing_sponsors

        bot = AsyncMock(spec=Bot)
        repo_mock = AsyncMock()
        repo_mock.get_active_channels.return_value = channels

        async def fake_membership(bot_arg: Any, channel_id: int, user_id_: int) -> bool:
            return membership_map.get(channel_id, True)

        with (
            patch("app.core.sponsor_service.SponsorRepo", return_value=repo_mock),
            patch(
                "app.core.sponsor_service.is_user_in_channel",
                side_effect=fake_membership,
            ),
        ):
            return await get_missing_sponsors(bot, session, user_id=user_id, fresh=True)

    async def test_never_joined_is_not_a_leave(self, session: AsyncSession) -> None:
        from app.core.sponsor_service import claim_recent_leaves

        ch = self._make_channel(-100555, "FreshChan")
        assert await self._run_raw([ch], {-100555: False}, session) == [ch]
        assert claim_recent_leaves(99) == []


# ===========================================================================
# 6. build_sponsor_join_keyboard
# ===========================================================================


class TestBuildSponsorJoinKeyboard:
    """The keyboard always contains one join button per channel + a check button."""

    @staticmethod
    def _make_channel(
        title: str,
        username: str | None = None,
        invite_link: str | None = None,
    ) -> SponsorChannel:
        ch = SponsorChannel()
        ch.id = 1
        ch.channel_id = -100999
        ch.title = title
        ch.username = username
        ch.invite_link = invite_link
        ch.is_active = True
        return ch

    @staticmethod
    def _make_registry() -> Any:
        return FakeRegistry()

    def test_keyboard_has_channel_button_and_check_button(self) -> None:
        ch = self._make_channel("My Channel", username="mychannel")
        kb = build_sponsor_join_keyboard([ch], self._make_registry())
        rows = kb.as_markup().inline_keyboard
        assert len(rows) == 2  # 1 channel + 1 check button

    def test_keyboard_check_button_has_correct_callback(self) -> None:
        ch = self._make_channel("Chan", username="chan123")
        kb = build_sponsor_join_keyboard([ch], self._make_registry())
        check_btn = kb.as_markup().inline_keyboard[-1][0]
        assert check_btn.callback_data == "check_sponsors"
        assert check_btn.url is None

    def test_channel_button_uses_invite_link_over_username(self) -> None:
        ch = self._make_channel(
            "Private Chan",
            username="somename",
            invite_link="https://t.me/+SecretInvite",
        )
        kb = build_sponsor_join_keyboard([ch], self._make_registry())
        channel_btn = kb.as_markup().inline_keyboard[0][0]
        assert channel_btn.url == "https://t.me/+SecretInvite"

    def test_channel_button_falls_back_to_username_url(self) -> None:
        ch = self._make_channel("Public Chan", username="publicchan")
        kb = build_sponsor_join_keyboard([ch], self._make_registry())
        channel_btn = kb.as_markup().inline_keyboard[0][0]
        assert channel_btn.url == "https://t.me/publicchan"

    def test_channel_button_skipped_when_no_username_or_link(self) -> None:
        ch = self._make_channel("ID Only Chan", username=None, invite_link=None)
        kb = build_sponsor_join_keyboard([ch], self._make_registry())
        rows = kb.as_markup().inline_keyboard
        # No join button (a https://t.me button would mislead); only "I joined".
        assert len(rows) == 1
        assert rows[0][0].callback_data == "check_sponsors"

    def test_channel_button_carries_premium_icon_when_available(self) -> None:
        """With a premium id the icon moves to icon_custom_emoji_id, not the label."""
        ch = self._make_channel("Premium Chan", username="premiumchan")
        registry = FakeRegistry(premium_id="5368324170671202286")
        kb = build_sponsor_join_keyboard([ch], registry)
        channel_btn = kb.as_markup().inline_keyboard[0][0]
        assert channel_btn.icon_custom_emoji_id == "5368324170671202286"
        assert "📣" not in channel_btn.text

    def test_channel_button_prefixes_fallback_without_premium(self) -> None:
        ch = self._make_channel("Plain Chan", username="plainchan")
        kb = build_sponsor_join_keyboard([ch], FakeRegistry(premium_id=None))
        channel_btn = kb.as_markup().inline_keyboard[0][0]
        assert channel_btn.icon_custom_emoji_id is None
        assert "📣" in channel_btn.text

    def test_multiple_channels_produce_multiple_rows(self) -> None:
        channels = [self._make_channel(f"Chan{i}", username=f"chan{i}000") for i in range(3)]
        kb = build_sponsor_join_keyboard(channels, self._make_registry())
        rows = kb.as_markup().inline_keyboard
        assert len(rows) == 4  # 3 channels + 1 check button

    def test_empty_channel_list_still_has_check_button(self) -> None:
        kb = build_sponsor_join_keyboard([], self._make_registry())
        rows = kb.as_markup().inline_keyboard
        assert len(rows) == 1
        assert rows[0][0].callback_data == "check_sponsors"


# ===========================================================================
# 7. SponsorGateMiddleware — patched get_missing_sponsors
# ===========================================================================


class TestSponsorGateMiddleware:
    """Middleware routes correctly based on missing-sponsor list."""

    @staticmethod
    def _make_dispatcher() -> tuple[Dispatcher, list[str]]:
        """A dispatcher carrying only the gate middleware and a probe router.

        `session` and `bot` are injected by a middleware registered *before* the
        gate, mirroring how DbSessionMiddleware publishes them in production
        (see app/middlewares/setup.py).
        """
        reached: list[str] = []

        probe_router = Router(name="probe")

        @probe_router.message()
        async def msg_handler(message: Message) -> None:
            reached.append("message")

        @probe_router.callback_query()
        async def cb_handler(query: CallbackQuery) -> None:
            reached.append("callback")

        dp = Dispatcher(storage=MemoryStorage(), settings=get_settings())

        class InjectMiddleware:
            async def __call__(self, handler: Any, event: Any, data: dict[str, Any]) -> Any:
                data["session"] = AsyncMock(spec=AsyncSession)
                data["bot"] = AsyncMock(spec=Bot)
                return await handler(event, data)

        dp.update.outer_middleware(InjectMiddleware())
        dp.update.outer_middleware(SponsorGateMiddleware())
        dp.include_router(probe_router)
        return dp, reached

    async def test_no_missing_sponsors_lets_message_through(self) -> None:
        dp, reached = self._make_dispatcher()
        bot = RecordingBot()
        try:
            with patch(
                "app.middlewares.sponsor_gate.get_missing_sponsors",
                new=AsyncMock(return_value=[]),
            ):
                await dp.feed_update(bot, make_update(1, "hello", USER_ID))
        finally:
            await bot.session.close()

        assert "message" in reached

    async def test_missing_sponsors_blocks_message_handler(self) -> None:
        dp, reached = self._make_dispatcher()
        bot = RecordingBot()

        ch = SponsorChannel()
        ch.id = 1
        ch.channel_id = -100111
        ch.title = "Sponsor Chan"
        ch.is_active = True
        ch.username = "sponsorchan"
        ch.invite_link = None

        try:
            with (
                patch(
                    "app.middlewares.sponsor_gate.get_missing_sponsors",
                    new=AsyncMock(return_value=[ch]),
                ),
                patch(
                    "app.middlewares.sponsor_gate.get_emoji_registry",
                    return_value=FakeRegistry(),
                ),
            ):
                await dp.feed_update(bot, make_update(1, "hello", USER_ID))
        finally:
            await bot.session.close()

        assert "message" not in reached, "the gate must swallow the update"

        # The user is told what to do instead of being met with silence.
        sends = [c for c in bot.calls if type(c).__name__ == "SendMessage"]
        assert len(sends) == 1
        assert "اسپانسر" in sends[0].text
        assert sends[0].reply_markup is not None
        join_btn = sends[0].reply_markup.inline_keyboard[0][0]
        assert join_btn.url == "https://t.me/sponsorchan"

    async def test_admin_bypasses_sponsor_gate(self) -> None:
        """An admin is never locked out, even with every sponsor unjoined."""
        dp, reached = self._make_dispatcher()
        bot = RecordingBot()

        ch = SponsorChannel()
        ch.id = 2
        ch.channel_id = -100999
        ch.title = "Admin Chan"
        ch.is_active = True
        ch.username = None
        ch.invite_link = None

        missing_mock = AsyncMock(return_value=[ch])
        try:
            with patch(
                "app.middlewares.sponsor_gate.get_missing_sponsors",
                new=missing_mock,
            ):
                await dp.feed_update(bot, make_update(1, "hello", ADMIN_ID))
        finally:
            await bot.session.close()

        assert "message" in reached
        assert missing_mock.await_count == 0, "an admin must not cost a membership check"

    async def test_missing_sponsors_blocks_callback_with_alert(self) -> None:
        """A blocked callback gets an alert plus the prompt, so no spinner hangs."""
        dp, reached = self._make_dispatcher()
        bot = RecordingBot()

        ch = SponsorChannel()
        ch.id = 4
        ch.channel_id = -100555
        ch.title = "CB Chan"
        ch.is_active = True
        ch.username = "cbchan"
        ch.invite_link = None

        try:
            with (
                patch(
                    "app.middlewares.sponsor_gate.get_missing_sponsors",
                    new=AsyncMock(return_value=[ch]),
                ),
                patch(
                    "app.middlewares.sponsor_gate.get_emoji_registry",
                    return_value=FakeRegistry(),
                ),
            ):
                await dp.feed_update(bot, make_callback_update(5, "menu:root", USER_ID))
        finally:
            await bot.session.close()

        assert "callback" not in reached
        call_names = [type(c).__name__ for c in bot.calls]
        assert "AnswerCallbackQuery" in call_names
        assert "SendMessage" in call_names

    async def test_check_sponsors_callback_passes_through_gate(self) -> None:
        dp, reached = self._make_dispatcher()
        bot = RecordingBot()

        ch = SponsorChannel()
        ch.id = 3
        ch.channel_id = -100777
        ch.title = "Block Chan"
        ch.is_active = True
        ch.username = "blockchan"
        ch.invite_link = None

        try:
            with patch(
                "app.middlewares.sponsor_gate.get_missing_sponsors",
                new=AsyncMock(return_value=[ch]),
            ):
                await dp.feed_update(bot, make_callback_update(2, "check_sponsors", USER_ID))
        finally:
            await bot.session.close()

        assert "callback" in reached

    async def test_no_user_in_data_passes_through(self) -> None:
        dp, reached = self._make_dispatcher()
        bot = RecordingBot()

        try:
            with patch(
                "app.middlewares.sponsor_gate.get_missing_sponsors",
                new=AsyncMock(return_value=[]),
            ):
                await dp.feed_update(bot, make_update(3, "hi", USER_ID))
        finally:
            await bot.session.close()

        assert "message" in reached

    async def test_group_regular_message_bypasses_sponsor_gate(self) -> None:
        """Regular non-command chat messages in groups bypass sponsor gate completely."""
        dp, reached = self._make_dispatcher()
        bot = RecordingBot()

        ch = SponsorChannel()
        ch.id = 1
        ch.channel_id = -100111
        ch.title = "Sponsor Chan"
        ch.is_active = True
        ch.username = "sponsorchan"

        try:
            with patch(
                "app.middlewares.sponsor_gate.get_missing_sponsors",
                new=AsyncMock(return_value=[ch]),
            ):
                # Send regular chat message without / command in supergroup
                await dp.feed_update(bot, make_group_update(10, "hello group!", USER_ID))
        finally:
            await bot.session.close()

        assert "message" in reached, "regular group chat message must bypass sponsor gate"
        sends = [c for c in bot.calls if type(c).__name__ == "SendMessage"]
        assert len(sends) == 0, (
            "no sponsor prompt must be sent into group chat for regular messages"
        )

    async def test_anonymous_group_sender_is_denied_with_guidance(self) -> None:
        """Admins posting 'as group' can never pass a membership check."""
        from aiogram.types import Chat as TgChat

        dp, reached = self._make_dispatcher()
        bot = RecordingBot()

        anon = TgUser(id=1087968824, is_bot=True, first_name="GroupAnonymousBot")
        msg = Message(
            message_id=20,
            date=datetime.datetime.now(datetime.UTC),
            chat=TgChat(id=-100123456789, type="supergroup"),
            from_user=anon,
            sender_chat=TgChat(id=-100123456789, type="supergroup", title="G"),
            text="/media",
        )
        try:
            with patch(
                "app.middlewares.sponsor_gate.get_missing_sponsors",
                new=AsyncMock(return_value=[]),
            ):
                await dp.feed_update(bot, Update(update_id=20, message=msg))
        finally:
            await bot.session.close()

        assert "message" not in reached, "anonymous /media must not reach handlers"
        sends = [c for c in bot.calls if type(c).__name__ == "SendMessage"]
        assert len(sends) == 1
        assert "ناشناس" in sends[0].text

    async def test_anonymous_group_admin_regular_chatter_is_ignored(self) -> None:
        """Admins posting 'as group' on regular messages or non-Mewvia commands must be completely ignored."""
        from aiogram.types import Chat as TgChat

        dp, _reached = self._make_dispatcher()
        bot = RecordingBot()

        anon = TgUser(id=1087968824, is_bot=True, first_name="GroupAnonymousBot")
        # 1. Plain chat from group admin
        msg1 = Message(
            message_id=21,
            date=datetime.datetime.now(datetime.UTC),
            chat=TgChat(id=-100123456789, type="supergroup"),
            from_user=anon,
            sender_chat=TgChat(id=-100123456789, type="supergroup", title="G"),
            text="سلام بچه‌ها جلسه ساعت چنده؟",
        )
        # 2. Non-Mewvia command (e.g. /ban, /pin)
        msg2 = Message(
            message_id=22,
            date=datetime.datetime.now(datetime.UTC),
            chat=TgChat(id=-100123456789, type="supergroup"),
            from_user=anon,
            sender_chat=TgChat(id=-100123456789, type="supergroup", title="G"),
            text="/ban @bad_user",
        )
        try:
            await dp.feed_update(bot, Update(update_id=21, message=msg1))
            await dp.feed_update(bot, Update(update_id=22, message=msg2))
        finally:
            await bot.session.close()

        assert len(bot.calls) == 0, (
            "bot must stay 100% silent on regular admin chatter and commands"
        )

    async def test_group_command_enforces_sponsor_gate(self) -> None:
        """Bot commands like /media in groups must enforce sponsor gate."""
        dp, reached = self._make_dispatcher()
        bot = RecordingBot()

        ch = SponsorChannel()
        ch.id = 1
        ch.channel_id = -100111
        ch.title = "Sponsor Chan"
        ch.is_active = True
        ch.username = "sponsorchan"

        try:
            with (
                patch(
                    "app.middlewares.sponsor_gate.get_missing_sponsors",
                    new=AsyncMock(return_value=[ch]),
                ),
                patch(
                    "app.middlewares.sponsor_gate.get_emoji_registry",
                    return_value=FakeRegistry(),
                ),
            ):
                # Send /media command in supergroup
                await dp.feed_update(bot, make_group_update(11, "/media", USER_ID))
        finally:
            await bot.session.close()

        assert "message" not in reached, (
            "bot commands in group must be blocked if sponsor channels unjoined"
        )
        sends = [c for c in bot.calls if type(c).__name__ == "SendMessage"]
        assert len(sends) == 1
        assert "اسپانسر" in sends[0].text

    async def test_group_media_url_enforces_sponsor_gate(self) -> None:
        """Media URLs sent in groups must enforce sponsor gate to prevent free bypass."""
        dp, reached = self._make_dispatcher()
        bot = RecordingBot()

        ch = SponsorChannel()
        ch.id = 1
        ch.channel_id = -100111
        ch.title = "Sponsor Chan"
        ch.is_active = True
        ch.username = "sponsorchan"

        media_url = "https://www.instagram.com/reel/C8xyz123"
        try:
            with (
                patch(
                    "app.middlewares.sponsor_gate.get_missing_sponsors",
                    new=AsyncMock(return_value=[ch]),
                ),
                patch(
                    "app.middlewares.sponsor_gate.get_emoji_registry",
                    return_value=FakeRegistry(),
                ),
            ):
                await dp.feed_update(bot, make_group_update(12, media_url, USER_ID))
        finally:
            await bot.session.close()

        assert "message" not in reached, "media download in group must be blocked when unjoined"
        sends = [c for c in bot.calls if type(c).__name__ == "SendMessage"]
        assert len(sends) == 1
        assert "اسپانسر" in sends[0].text

    async def test_group_media_url_passes_when_sponsored(self) -> None:
        """Media URLs in groups pass through to download handlers when user joined all sponsors."""
        dp, reached = self._make_dispatcher()
        bot = RecordingBot()

        media_url = "https://www.instagram.com/reel/C8xyz123"
        try:
            with patch(
                "app.middlewares.sponsor_gate.get_missing_sponsors",
                new=AsyncMock(return_value=[]),
            ):
                await dp.feed_update(bot, make_group_update(13, media_url, USER_ID))
        finally:
            await bot.session.close()

        assert "message" in reached, "media download in group must reach handler when sponsored"
        sends = [c for c in bot.calls if type(c).__name__ == "SendMessage"]
        assert len(sends) == 0, "no sponsor gate prompt should be sent when user is subscribed"


# ===========================================================================
# 8. SponsorGateMiddleware — direct call (DB-backed)
# ===========================================================================


async def test_gate_middleware_blocks_with_real_active_channel(
    session: AsyncSession,
) -> None:
    """Gate blocks when a real active channel exists and the user is not a member."""
    repo = SponsorRepo(session)
    await repo.add_or_update(
        channel_id=-1001999999001,
        title="Gate Test Channel",
        username="gatetestchan",
        is_active=True,
    )
    await session.commit()

    middleware = SponsorGateMiddleware()

    async def fake_handler(event: Any, data: dict[str, Any]) -> str:
        return "reached"

    bot_mock = AsyncMock(spec=Bot)
    bot_mock.get_chat_member.return_value = ChatMemberLeft(
        status=ChatMemberStatus.LEFT,
        user=TgUser(id=USER_ID, is_bot=False, first_name="Test"),
    )

    user = TgUser(id=USER_ID, is_bot=False, first_name="Test")
    msg = Message(
        message_id=1,
        date=datetime.datetime.now(datetime.UTC),
        chat=Chat(id=USER_ID, type="private"),
        from_user=user,
        text="test",
    )
    bot = RecordingBot()
    update = Update(update_id=1, message=msg.as_(bot))

    data: dict[str, Any] = {
        "event_from_user": user,
        "session": session,
        "bot": bot_mock,
    }

    try:
        with patch(
            "app.middlewares.sponsor_gate.get_emoji_registry",
            return_value=FakeRegistry(),
        ):
            result = await middleware(fake_handler, update, data)
    finally:
        await bot.session.close()

    assert result is None, "the handler must not run"


async def test_gate_middleware_passes_when_channel_is_inactive(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deactivated sponsor channel stops gating immediately."""
    _ensure_emoji_registry(monkeypatch, session)
    repo = SponsorRepo(session)
    for row in await repo.all():
        await repo.set_active(row.id, False)
    await session.flush()
    await repo.add_or_update(
        channel_id=-1001999999002,
        title="Inactive Gate Channel",
        username="inactivegate",
        is_active=False,
    )
    await session.commit()

    middleware = SponsorGateMiddleware()

    async def fake_handler(event: Any, data: dict[str, Any]) -> str:
        return "reached"

    bot_mock = AsyncMock(spec=Bot)
    bot_mock.get_chat_member.return_value = ChatMemberLeft(
        status=ChatMemberStatus.LEFT,
        user=TgUser(id=USER_ID, is_bot=False, first_name="Test"),
    )

    user = TgUser(id=USER_ID + 7, is_bot=False, first_name="Test7")
    msg = Message(
        message_id=2,
        date=datetime.datetime.now(datetime.UTC),
        chat=Chat(id=user.id, type="private"),
        from_user=user,
        text="test",
    )
    data: dict[str, Any] = {
        "event_from_user": user,
        "session": session,
        "bot": bot_mock,
    }

    result = await middleware(fake_handler, Update(update_id=2, message=msg), data)
    assert result == "reached"
    assert bot_mock.get_chat_member.await_count == 0, "inactive channels are never checked"


# ===========================================================================
# 9. Admin sponsor router — add flow and mutations
# ===========================================================================


def _fsm_context(bot_id: int = 111111, user_id: int = ADMIN_ID) -> FSMContext:
    return FSMContext(
        storage=MemoryStorage(),
        key=StorageKey(bot_id=bot_id, chat_id=user_id, user_id=user_id),
    )


def _ensure_emoji_registry(monkeypatch: object, session: object) -> None:
    """Some router units call ready_registry() without the app dispatcher.

    Points the stores' DB scope at this test's rolled-back session
    (auto-restored by monkeypatch), so registry reads never touch the
    global engine or a stale session from another test.
    """
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    import app.core.settings_store as store_module
    import app.ui.emoji as emoji_module
    from app.core.settings_store import init_settings_store
    from app.ui.emoji import init_emoji_registry

    @asynccontextmanager
    async def _scope() -> AsyncIterator[object]:
        yield session

    monkeypatch.setattr(store_module, "session_scope", _scope)  # type: ignore[arg-type,attr-defined]
    monkeypatch.setattr(emoji_module, "session_scope", _scope)  # type: ignore[arg-type,attr-defined]
    init_settings_store(None)
    init_emoji_registry(None)


def _bind_tree(obj: object, bot: Bot) -> None:
    """aiogram's .as_() binds only the parent; reply/answer need children bound."""
    from aiogram.types import TelegramObject

    seen: set[int] = set()

    def _walk(o: object) -> None:
        if id(o) in seen:
            return
        seen.add(id(o))
        if isinstance(o, TelegramObject):
            o.as_(bot)
        if isinstance(o, (list, tuple)):
            for v in o:
                _walk(v)
        elif isinstance(o, dict):
            for v in o.values():
                _walk(v)
        elif hasattr(o, "__dict__"):
            for v in vars(o).values():
                if isinstance(v, (TelegramObject, list, tuple, dict)):
                    _walk(v)

    _walk(obj)


def _admin_message(bot: Bot, text: str | None, message_id: int = 900) -> Message:
    msg = Message(
        message_id=message_id,
        date=datetime.datetime.now(datetime.UTC),
        chat=Chat(id=ADMIN_ID, type="private"),
        from_user=TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin"),
        text=text,
    )
    msg.as_(bot)
    _bind_tree(msg, bot)
    return msg


async def test_admin_unresolvable_input_reports_error() -> None:
    """An unresolvable channel reports an error to the admin."""
    from app.admin.sponsor_router import process_sponsor_input

    bot = RecordingBot()  # get_chat returns None, so resolution "fails"
    state = _fsm_context()
    session = AsyncMock(spec=AsyncSession)

    try:
        with patch(
            "app.admin.sponsor_router.get_emoji_registry",
            return_value=FakeRegistry(),
        ):
            await process_sponsor_input(_admin_message(bot, "-1001234509876"), session, state, bot)
    finally:
        await bot.session.close()

    sends = [c for c in bot.calls if type(c).__name__ == "SendMessage"]
    assert len(sends) == 1
    assert "شناسایی نشد" in sends[0].text or "نامعتبر" in sends[0].text


async def test_admin_private_link_without_forward_asks_for_forward() -> None:
    from app.admin.sponsor_router import process_sponsor_input

    bot = RecordingBot()
    state = _fsm_context()
    session = AsyncMock(spec=AsyncSession)

    try:
        with patch(
            "app.admin.sponsor_router.get_emoji_registry",
            return_value=FakeRegistry(),
        ):
            await process_sponsor_input(
                _admin_message(bot, "https://t.me/+PrivateHash99"), session, state, bot
            )
    finally:
        await bot.session.close()

    sends = [c for c in bot.calls if type(c).__name__ == "SendMessage"]
    assert len(sends) == 1
    assert "خصوصی" in sends[0].text


async def test_admin_invalid_input_keeps_state_and_reports_error() -> None:
    from app.admin.sponsor_router import process_sponsor_input

    bot = RecordingBot()
    state = _fsm_context()
    await state.set_state("SponsorAddSG:waiting_for_input")
    session = AsyncMock(spec=AsyncSession)

    try:
        with patch(
            "app.admin.sponsor_router.get_emoji_registry",
            return_value=FakeRegistry(),
        ):
            await process_sponsor_input(_admin_message(bot, "!!!"), session, state, bot)
    finally:
        await bot.session.close()

    sends = [c for c in bot.calls if type(c).__name__ == "SendMessage"]
    assert len(sends) == 1
    assert "شناسایی نشد" in sends[0].text or "نامعتبر" in sends[0].text


async def test_admin_title_step_persists_channel(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The title step writes the row assembled across both FSM steps."""
    _ensure_emoji_registry(monkeypatch, session)
    from app.admin.sponsor_router import process_sponsor_title

    bot = RecordingBot()
    state = _fsm_context()
    await state.update_data(
        sponsor_channel_id=-1001357913579,
        sponsor_username="fsmchan",
        sponsor_invite_link=None,
    )

    try:
        await process_sponsor_title(_admin_message(bot, "کانال تست FSM"), session, state)
    finally:
        await bot.session.close()

    stored = await SponsorRepo(session).get_by_channel_id(-1001357913579)
    assert stored is not None
    assert stored.title == "کانال تست FSM"
    assert stored.username == "fsmchan"
    assert stored.is_active is True
    assert await state.get_state() is None, "the flow must end cleanly"


async def test_admin_title_step_rejects_non_text(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ensure_emoji_registry(monkeypatch, session)
    from app.admin.sponsor_router import process_sponsor_title

    bot = RecordingBot()
    state = _fsm_context()
    await state.update_data(sponsor_channel_id=-1001111000111)

    try:
        await process_sponsor_title(_admin_message(bot, None), session, state)
    finally:
        await bot.session.close()

    assert await SponsorRepo(session).get_by_channel_id(-1001111000111) is None


async def test_admin_toggle_flips_active_flag(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ensure_emoji_registry(monkeypatch, session)
    from app.admin.sponsor_router import toggle_sponsor

    repo = SponsorRepo(session)
    ch = await repo.add_or_update(
        channel_id=-1001246802468, title="Toggle Via Router", is_active=True
    )
    await session.commit()

    bot = RecordingBot()
    query = CallbackQuery(
        id="tg1",
        from_user=TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin"),
        chat_instance="ci",
        data=f"sponsor_act:toggle:{ch.id}",
        message=Message(
            message_id=10,
            date=datetime.datetime.now(datetime.UTC),
            chat=Chat(id=ADMIN_ID, type="private"),
            text="list",
        ),
    )
    query.as_(bot)
    _bind_tree(query, bot)

    try:
        with patch(
            "app.admin.sponsor_router.get_emoji_registry",
            return_value=FakeRegistry(),
        ):
            await toggle_sponsor(query, session)
    finally:
        await bot.session.close()

    assert ch.is_active is False


async def test_admin_delete_removes_row(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _ensure_emoji_registry(monkeypatch, session)
    from app.admin.sponsor_router import delete_sponsor

    repo = SponsorRepo(session)
    ch = await repo.add_or_update(channel_id=-1001369036903, title="Delete Via Router")
    await session.commit()

    bot = RecordingBot()
    query = CallbackQuery(
        id="dl1",
        from_user=TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin"),
        chat_instance="ci",
        data=f"sponsor_act:delete:{ch.id}",
        message=Message(
            message_id=11,
            date=datetime.datetime.now(datetime.UTC),
            chat=Chat(id=ADMIN_ID, type="private"),
            text="list",
        ),
    )
    query.as_(bot)
    _bind_tree(query, bot)

    try:
        with patch(
            "app.admin.sponsor_router.get_emoji_registry",
            return_value=FakeRegistry(),
        ):
            await delete_sponsor(query, session)
    finally:
        await bot.session.close()

    assert await repo.get_by_channel_id(-1001369036903) is None


async def test_admin_list_renders_channel_rows(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Each channel gets an info / toggle / delete triplet plus add + back rows."""
    _ensure_emoji_registry(monkeypatch, session)
    from app.admin.sponsor_router import list_sponsors

    repo = SponsorRepo(session)
    await repo.add_or_update(channel_id=-1001482048204, title="Listed Chan", username="listedchan")
    await session.commit()

    bot = RecordingBot()
    query = CallbackQuery(
        id="ls1",
        from_user=TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin"),
        chat_instance="ci",
        data="sponsor_act:list",
        message=Message(
            message_id=12,
            date=datetime.datetime.now(datetime.UTC),
            chat=Chat(id=ADMIN_ID, type="private"),
            text="admin panel",
        ),
    )
    query.as_(bot)
    _bind_tree(query, bot)

    try:
        with patch(
            "app.admin.sponsor_router.get_emoji_registry",
            return_value=FakeRegistry(),
        ):
            await list_sponsors(query, session)
    finally:
        await bot.session.close()

    edits = [c for c in bot.calls if type(c).__name__ == "EditMessageText"]
    assert len(edits) == 1
    assert "Listed Chan" in edits[0].text

    rows = edits[0].reply_markup.inline_keyboard
    channel_row = rows[0]
    assert len(channel_row) == 3
    assert channel_row[0].callback_data.startswith("sponsor_act:info:")
    assert channel_row[1].callback_data.startswith("sponsor_act:toggle:")
    assert channel_row[2].callback_data.startswith("sponsor_act:delete:")
    assert rows[-2][0].callback_data == "sponsor_act:add"
    assert rows[-1][0].callback_data == "sponsor_act:list"


async def test_admin_list_handles_empty_table(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With no channels the screen still offers add + back."""
    _ensure_emoji_registry(monkeypatch, session)
    from app.admin.sponsor_router import list_sponsors

    for row in await SponsorRepo(session).all():
        await session.delete(row)
    await session.flush()

    bot = RecordingBot()
    query = CallbackQuery(
        id="ls2",
        from_user=TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin"),
        chat_instance="ci",
        data="sponsor_act:list",
        message=Message(
            message_id=13,
            date=datetime.datetime.now(datetime.UTC),
            chat=Chat(id=ADMIN_ID, type="private"),
            text="admin panel",
        ),
    )
    query.as_(bot)
    _bind_tree(query, bot)

    try:
        with patch(
            "app.admin.sponsor_router.get_emoji_registry",
            return_value=FakeRegistry(),
        ):
            await list_sponsors(query, session)
    finally:
        await bot.session.close()

    edits = [c for c in bot.calls if type(c).__name__ == "EditMessageText"]
    assert len(edits) == 1
    assert "ثبت نشده" in edits[0].text
    rows = edits[0].reply_markup.inline_keyboard
    assert len(rows) == 2  # add + back only


# ===========================================================================
# 10. Wiring: the gate and the admin router are actually installed
# ===========================================================================


def test_sponsor_gate_is_registered_on_the_dispatcher() -> None:
    """A gate that is never wired in protects nothing."""
    from app.config import Settings
    from app.middlewares import setup_middlewares

    dp = Dispatcher(storage=MemoryStorage())
    settings = Settings(bot_token="1:x")
    setup_middlewares(dp, settings, MagicMock())

    installed = [type(m).__name__ for m in dp.update.outer_middleware]
    assert "SponsorGateMiddleware" in installed
    # The gate needs `session` published by DbSessionMiddleware, so it must run after.
    assert installed.index("DbSessionMiddleware") < installed.index("SponsorGateMiddleware")


def test_admin_router_includes_sponsor_router() -> None:
    from app.admin.router import router as admin_router

    names = [r.name for r in admin_router.sub_routers]
    assert "admin_sponsor" in names


# ===========================================================================
# 11. Emoji slots used by the sponsor system
# ===========================================================================


def test_sponsor_emoji_slots_registered() -> None:
    """Every slot the sponsor screens ask for must exist in the seed."""
    from app.db.seed import EMOJI_SLOTS

    slot_names = {s.slot for s in EMOJI_SLOTS}
    # middleware prompt / admin list / add + delete buttons / confirm + cancel
    for slot in ("lock", "sponsor", "add", "delete", "ok", "error", "back", "broadcast"):
        assert slot in slot_names, f"emoji slot {slot!r} is used but not seeded"


# ===========================================================================
# 12. Sponsor migration
# ===========================================================================


def test_sponsor_channels_migration_exists() -> None:
    """The table must be created by a migration, not only by the ORM metadata."""
    from pathlib import Path

    versions_dir = Path(__file__).parent.parent / "migrations" / "versions"
    content = "\n".join(f.read_text(encoding="utf-8") for f in versions_dir.glob("*.py"))
    assert "sponsor_channels" in content, (
        "No migration creates sponsor_channels — run `./dev.sh revision 'add sponsor_channels'`."
    )


def test_sponsor_migration_covers_every_model_column() -> None:
    """Guards against a column added to the model but never migrated."""
    from pathlib import Path

    versions_dir = Path(__file__).parent.parent / "migrations" / "versions"
    content = "\n".join(f.read_text(encoding="utf-8") for f in versions_dir.glob("*.py"))
    for column in SponsorChannel.__table__.columns:
        assert f'"{column.name}"' in content, (
            f"column {column.name!r} of sponsor_channels is missing from the migrations"
        )


# ===========================================================================
# 10. sponsor_leavewatch — instant leave DM
# ===========================================================================


def _make_member_update(
    update_id: int,
    user_id: int,
    old_status: object,
    new_status: object,
    chat_id: int = -1003989313192,
) -> Update:
    from aiogram.enums import ChatMemberStatus as _CMS
    from aiogram.types import ChatMemberUpdated

    def _member(status: object) -> object:
        user = TgUser(id=user_id, is_bot=False, first_name="Leaver")
        if status == _CMS.LEFT:
            return ChatMemberLeft(status=status, user=user)
        if status == _CMS.KICKED:
            from aiogram.types import ChatMemberBanned

            return ChatMemberBanned(status=status, user=user, until_date=None)
        return ChatMemberMember(status=status, user=user)

    cm = ChatMemberUpdated(
        chat=Chat(id=chat_id, type="channel"),
        from_user=TgUser(id=user_id, is_bot=False, first_name="Leaver"),
        date=datetime.datetime.now(datetime.UTC),
        old_chat_member=_member(old_status),
        new_chat_member=_member(new_status),
    )
    return Update(update_id=update_id, chat_member=cm)


async def test_leave_watcher_dms_on_leave(session: AsyncSession) -> None:
    """Leaving a sponsor channel triggers an instant warm DM."""
    from app.features.sponsor_leavewatch.router import on_sponsor_member_update

    repo = SponsorRepo(session)
    await repo.add_or_update(channel_id=-1003989313192, title="Mewvia")
    await session.commit()

    update = _make_member_update(9001, 424242, ChatMemberStatus.MEMBER, ChatMemberStatus.LEFT)
    event = update.chat_member
    assert event is not None

    class DMBot(RecordingBot):
        pass

    dmbot = DMBot()
    try:
        with patch(
            "app.features.sponsor_leavewatch.router.get_emoji_registry",
            return_value=FakeRegistry(),
        ):
            await on_sponsor_member_update(event, dmbot, session, None)
    finally:
        await dmbot.session.close()

    dms = [m for m in dmbot.calls if type(m).__name__ == "SendMessage"]
    assert len(dms) == 1
    assert dms[0].chat_id == 424242
    assert "خارج شدی" in dms[0].text
    assert dms[0].reply_markup is not None


async def test_leave_watcher_silent_for_non_member_change(
    session: AsyncSession,
) -> None:
    """Join-request declines and non-sponsor chats stay silent."""
    from app.features.sponsor_leavewatch.router import on_sponsor_member_update

    repo = SponsorRepo(session)
    await repo.add_or_update(channel_id=-1003989313192, title="Mewvia")
    await session.commit()

    class DMBot(RecordingBot):
        pass

    dmbot = DMBot()
    try:
        with patch(
            "app.features.sponsor_leavewatch.router.get_emoji_registry",
            return_value=FakeRegistry(),
        ):
            # Different channel entirely.
            other = _make_member_update(
                9002,
                434343,
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.LEFT,
                chat_id=-1000000000000,
            )
            assert other.chat_member is not None
            await on_sponsor_member_update(other.chat_member, dmbot, session, None)
            # Kicked->left noise: new status not a leave.
            joined = _make_member_update(
                9003, 434344, ChatMemberStatus.LEFT, ChatMemberStatus.MEMBER
            )
            assert joined.chat_member is not None
            await on_sponsor_member_update(joined.chat_member, dmbot, session, None)
    finally:
        await dmbot.session.close()

    assert dmbot.calls == []
