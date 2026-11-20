"""Phase-5 acceptance tests: admin panel (stats, broadcast, user export, backup, router).

Tests run against the PostgreSQL database in the container.
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

from aiogram import Dispatcher
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message
from aiogram.types import User as TgUser
from app.admin.backup import create_db_backup
from app.admin.broadcast import send_broadcast
from app.admin.router import _render_user_detail, _render_users_page
from app.admin.sponsor_router import render_sponsor_panel
from app.admin.stats import (
    format_stats_media,
    format_stats_overview,
    format_stats_sponsors,
    format_stats_text,
    get_admin_stats,
)
from app.admin.users_export import export_users_json, export_users_txt
from app.callbacks import MenuCB
from app.config import Settings
from app.db.models.sponsor_seen import SponsorSeenMember
from app.db.models.sticker_set import StickerSet
from app.db.repo import ConversionRepo, EmojiRepo, SponsorRepo, UserRepo
from sqlalchemy.ext.asyncio import AsyncSession

from tests.conftest import REAL_DB
from tests.test_phase2_menu import RecordingBot
from tests.test_sponsor import FakeRegistry

ADMIN_ID = 1
USER_1_ID = 999_000_000_101
USER_2_ID = 999_000_000_102


# ---------------------------------------------------------------------------
# Statistics & User Exports
# ---------------------------------------------------------------------------


async def test_admin_stats_calculation(session: AsyncSession) -> None:
    user_repo = UserRepo(session)
    await user_repo.upsert_from_telegram(
        TgUser(id=USER_1_ID, is_bot=False, first_name="u1", username="usr1")
    )
    await user_repo.upsert_from_telegram(
        TgUser(id=USER_2_ID, is_bot=False, first_name="u2", username="usr2")
    )

    conv_repo = ConversionRepo(session)
    await conv_repo.log_conversion(
        telegram_id=USER_1_ID,
        source_kind="photo",
        target_id="webp",
        in_bytes=1000,
        out_bytes=500,
        duration_ms=200,
        status="success",
    )
    await session.commit()

    stats = await get_admin_stats(session)
    assert stats.total_users >= 2
    assert stats.total_conversions >= 1
    assert stats.successful_conversions >= 1
    assert stats.total_in_bytes >= 1000
    assert stats.total_out_bytes >= 500
    assert stats.success_rate > 0.0

    formatted = format_stats_text(stats)
    assert "آمار جامع" in formatted


async def test_export_users_json_and_txt(session: AsyncSession, tmp_path: Path) -> None:
    user_repo = UserRepo(session)
    await user_repo.upsert_from_telegram(
        TgUser(id=USER_1_ID, is_bot=False, first_name="Exporter", username="exporter_usr")
    )
    await session.commit()

    json_path = tmp_path / "export.json"
    txt_path = tmp_path / "export.txt"

    await export_users_json(session, json_path)
    await export_users_txt(session, txt_path)

    assert json_path.is_file()
    assert txt_path.is_file()

    content = json.loads(json_path.read_text(encoding="utf-8"))
    assert isinstance(content, list)
    assert any(u["telegram_id"] == USER_1_ID for u in content)

    txt_content = txt_path.read_text(encoding="utf-8")
    assert str(USER_1_ID) in txt_content


# ---------------------------------------------------------------------------
# Database Backup
# ---------------------------------------------------------------------------


async def test_create_db_backup(settings: Settings, tmp_path: Path, session: AsyncSession) -> None:
    """pg_dump fixture test inside container."""
    # Like other DB integration tests, require a reachable test database. Use
    # its full connection details rather than just copying its password.
    real_settings = settings.model_copy(update=REAL_DB)
    backup_file = await create_db_backup(real_settings, tmp_path)
    assert backup_file.is_file()
    assert backup_file.stat().st_size > 0
    assert backup_file.name.endswith(".dump")


# ---------------------------------------------------------------------------
# Broadcast Engine
# ---------------------------------------------------------------------------


async def test_broadcast_engine_delivers_and_tracks(
    app_dispatcher: Dispatcher, session: AsyncSession
) -> None:
    user_repo = UserRepo(session)
    await user_repo.upsert_from_telegram(
        TgUser(id=USER_1_ID, is_bot=False, first_name="b1", username="b_usr1")
    )
    await user_repo.upsert_from_telegram(
        TgUser(id=USER_2_ID, is_bot=False, first_name="b2", username="b_usr2")
    )
    await session.commit()

    bot = RecordingBot()
    try:
        src_msg = Message(
            message_id=50,
            date=datetime.datetime.now(datetime.UTC),
            chat=Chat(id=ADMIN_ID, type="private"),
            from_user=TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin"),
            text="Broadcast Test Message",
        )

        res = await send_broadcast(bot, session, src_msg, rate_limit=0.001)
        assert res.total >= 2
        assert res.successful >= 2
    finally:
        await bot.session.close()


# ---------------------------------------------------------------------------
# Admin Panel Router Navigation
# ---------------------------------------------------------------------------


async def test_admin_panel_router_actions(
    app_dispatcher: Dispatcher, session: AsyncSession
) -> None:
    bot = RecordingBot()
    try:
        # Step 1: Click "admin" menu
        cb_menu = CallbackQuery(
            id="c1",
            from_user=TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin"),
            message=Message(
                message_id=1,
                date=datetime.datetime.now(datetime.UTC),
                chat=Chat(id=ADMIN_ID, type="private"),
                text="Main Menu",
            ),
            chat_instance="ci",
            data=MenuCB(target="admin").pack(),
        )
        from aiogram.types import Update

        await app_dispatcher.feed_update(bot, Update(update_id=1, callback_query=cb_menu))
        assert len(bot.sent) >= 1
        assert any("پنل مدیریت" in str(m) for m in bot.sent)

        # Step 2: Clear premium emojis action
        repo = EmojiRepo(session)
        await repo.set_custom_emoji("ok", "5368324170671202286")
        await session.commit()

        cb_clear = CallbackQuery(
            id="c2",
            from_user=TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin"),
            message=Message(
                message_id=2,
                date=datetime.datetime.now(datetime.UTC),
                chat=Chat(id=ADMIN_ID, type="private"),
                text="Admin Panel",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
            ),
            chat_instance="ci",
            data="admin_act:clear_emojis",
        )
        await app_dispatcher.feed_update(bot, Update(update_id=2, callback_query=cb_clear))

    finally:
        await bot.session.close()

    slot = await repo.get("ok")
    assert slot is not None
    assert slot.custom_emoji_id is None, "clear_emojis action must reset premium ids"


async def test_admin_advanced_stats_and_formatting(session: AsyncSession) -> None:
    """Verify advanced metrics computation and tab formatters."""
    user_repo = UserRepo(session)
    u_adv = await user_repo.upsert_from_telegram(
        TgUser(
            id=999_888_777_001,
            is_bot=False,
            first_name="VIP",
            username="vip_user",
        )
    )
    u_adv.is_premium = True
    await session.commit()

    conv_repo = ConversionRepo(session)
    await conv_repo.log_conversion(
        telegram_id=999_888_777_001,
        source_kind="video",
        target_id="gif",
        in_bytes=2_000_000,
        out_bytes=1_000_000,
        duration_ms=1500,
        status="success",
    )
    await conv_repo.log_conversion(
        telegram_id=999_888_777_001,
        source_kind="photo",
        target_id="webp",
        in_bytes=100_000,
        out_bytes=50_000,
        duration_ms=300,
        status="failed",
    )
    await conv_repo.log_conversion(
        telegram_id=999_888_777_001,
        source_kind="audio",
        target_id="atool_tag_edit",
        in_bytes=3_000_000,
        out_bytes=2_900_000,
        duration_ms=800,
        status="success",
    )
    await conv_repo.log_conversion(
        telegram_id=999_888_777_001,
        source_kind="voice",
        target_id="atool_to_mp3",
        in_bytes=500_000,
        out_bytes=480_000,
        duration_ms=400,
        status="success",
    )

    # Seed sponsor and seen member
    sp_repo = SponsorRepo(session)
    sp_chan = await sp_repo.add_or_update(
        channel_id=-100888999111,
        title="Test Channel",
        username="test_chan",
        is_active=True,
    )
    await session.flush()
    session.add(
        SponsorSeenMember(
            channel_id=sp_chan.channel_id,
            user_id=999_888_777_001,
        )
    )

    # Seed sticker set
    session.add(
        StickerSet(
            telegram_id=999_888_777_001,
            name="test_pack_by_mewvia",
            title="My Test Pack",
            sticker_type="regular",
            stickers_count=5,
        )
    )
    await session.commit()

    stats = await get_admin_stats(session)

    # Assertions on advanced attributes
    assert stats.total_users >= 1
    assert stats.engaged_users >= 1
    assert stats.premium_users >= 1
    assert stats.converting_users >= 1
    assert stats.failed_conversions >= 1
    assert len(stats.top_sources) >= 1
    assert len(stats.top_targets) >= 1
    assert stats.total_sticker_packs >= 1
    assert stats.total_stickers_count >= 5
    assert stats.total_sponsors >= 1
    assert stats.active_sponsors >= 1
    assert stats.total_seen_members >= 1
    assert len(stats.sponsor_breakdown) >= 1
    assert stats.conversion_user_rate > 0.0
    assert stats.engaged_user_rate > 0.0

    # Music & voice toolkit assertions.
    assert stats.music_voice_total >= 2
    assert stats.music_voice_success >= 2
    assert stats.music_voice_bytes >= 2_900_000 + 480_000
    assert stats.voice_intake >= 1
    assert stats.audio_intake >= 1
    assert len(stats.music_voice_tools) >= 2
    assert stats.music_voice_success_rate > 0.0

    # Formatters verification
    overview_text = format_stats_overview(stats)
    assert "آمار جامع" in overview_text
    assert "کاربران و رشد" in overview_text
    assert "کاربران واقعی" in overview_text

    media_text = format_stats_media(stats)
    assert "آمار جامع مدیا" in media_text
    assert "عملیات‌های تبدیل" in media_text
    assert "استیکرپک‌ها" in media_text
    assert "ابزارهای موزیک و ویس" in media_text
    assert "ویرایش تگ و کاور موزیک" in media_text
    assert "تبدیل به MP3" in media_text

    sponsor_text = format_stats_sponsors(stats)
    assert "آمار جامع اسپانسر" in sponsor_text
    assert "Test Channel" in sponsor_text


async def test_successful_conversion_sets_has_used_bot(session: AsyncSession) -> None:
    """log_conversion(success) flips the real-user flag; failures don't."""
    user_repo = UserRepo(session)
    maker = TgUser(id=555_000_111, is_bot=False, first_name="Flag", username="flag_u")
    await user_repo.upsert_from_telegram(maker)
    await session.commit()

    row = await user_repo.get(555_000_111)
    assert row is not None
    assert row.has_used_bot is False

    conv_repo = ConversionRepo(session)
    await conv_repo.log_conversion(
        telegram_id=555_000_111,
        source_kind="photo",
        target_id="webp",
        status="failed",
        error_code="boom",
    )
    await session.commit()
    session.expire_all()
    row = await user_repo.get(555_000_111)
    assert row is not None
    assert row.has_used_bot is False, "a failed conversion must not flag the user"

    await conv_repo.log_conversion(
        telegram_id=555_000_111,
        source_kind="photo",
        target_id="webp",
        status="success",
    )
    await session.commit()
    session.expire_all()
    row = await user_repo.get(555_000_111)
    assert row is not None
    assert row.has_used_bot is True

    stats = await get_admin_stats(session)
    assert stats.engaged_users >= 1


async def test_engaged_means_real_use_not_button_taps(session: AsyncSession) -> None:
    """A user with many updates but zero conversions is passerby, not engaged."""
    user_repo = UserRepo(session)
    tapper = TgUser(id=555_000_222, is_bot=False, first_name="Tapper", username="tapper_u")
    u = await user_repo.upsert_from_telegram(tapper)
    u.updates_seen = 50
    # A second user with one real conversion — deterministic engaged baseline.
    doer = TgUser(id=555_000_223, is_bot=False, first_name="Doer", username="doer_u")
    await user_repo.upsert_from_telegram(doer)
    await ConversionRepo(session).log_conversion(
        telegram_id=555_000_223,
        source_kind="photo",
        target_id="webp",
        status="success",
    )
    await session.commit()

    stats = await get_admin_stats(session)
    assert stats.engaged_users >= 1
    assert stats.passerby_users >= 1
    session.expire_all()
    row = await user_repo.get(555_000_222)
    assert row is not None
    assert row.has_used_bot is False


async def test_admin_stats_tabs_navigation_e2e(
    app_dispatcher: Dispatcher, session: AsyncSession
) -> None:
    """E2E navigation through admin stats tabs via callback queries."""
    from aiogram.types import Update

    bot = RecordingBot()
    try:
        # Tab 1: Overview
        cb_overview = CallbackQuery(
            id="c_stat_1",
            from_user=TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin"),
            message=Message(
                message_id=10,
                date=datetime.datetime.now(datetime.UTC),
                chat=Chat(id=ADMIN_ID, type="private"),
                text="Current Panel",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
            ),
            chat_instance="ci",
            data="admin_stats:overview",
        )
        await app_dispatcher.feed_update(bot, Update(update_id=10, callback_query=cb_overview))
        edits_1 = [m for m in bot.sent if type(m).__name__ == "EditMessageText"]
        assert len(edits_1) >= 1
        assert "کاربران و رشد" in str(edits_1[-1].text)

        # Tab 2: Media
        cb_media = CallbackQuery(
            id="c_stat_2",
            from_user=TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin"),
            message=Message(
                message_id=11,
                date=datetime.datetime.now(datetime.UTC),
                chat=Chat(id=ADMIN_ID, type="private"),
                text="Current Panel",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
            ),
            chat_instance="ci",
            data="admin_stats:media",
        )
        await app_dispatcher.feed_update(bot, Update(update_id=11, callback_query=cb_media))
        edits_2 = [m for m in bot.sent if type(m).__name__ == "EditMessageText"]
        assert len(edits_2) >= 2
        assert "مدیا" in str(edits_2[-1].text)

        # Tab 3: Sponsors
        cb_sponsors = CallbackQuery(
            id="c_stat_3",
            from_user=TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin"),
            message=Message(
                message_id=12,
                date=datetime.datetime.now(datetime.UTC),
                chat=Chat(id=ADMIN_ID, type="private"),
                text="Current Panel",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
            ),
            chat_instance="ci",
            data="admin_stats:sponsors",
        )
        await app_dispatcher.feed_update(bot, Update(update_id=12, callback_query=cb_sponsors))
        edits_3 = [m for m in bot.sent if type(m).__name__ == "EditMessageText"]
        assert len(edits_3) >= 3
        assert "اسپانسر" in str(edits_3[-1].text)

    finally:
        await bot.session.close()


async def test_sponsor_panel_displays_seen_members(session: AsyncSession) -> None:
    """Verify render_sponsor_panel includes confirmed member count."""
    sp_repo = SponsorRepo(session)
    chan = await sp_repo.add_or_update(
        channel_id=-100999888777,
        title="Seen Test Channel",
        username="seen_chan",
        is_active=True,
    )
    session.add(
        SponsorSeenMember(
            channel_id=chan.channel_id,
            user_id=123456789,
        )
    )
    await session.commit()

    registry = FakeRegistry()
    text, _kb = await render_sponsor_panel(session, registry)

    assert "Seen Test Channel" in text
    assert "تاییدشده" in text


async def test_admin_users_pagination_and_search(session: AsyncSession) -> None:
    """Verify pagination calculation and search matching."""
    user_repo = UserRepo(session)
    for i in range(1, 13):
        await user_repo.upsert_from_telegram(
            TgUser(
                id=999_700_000_000 + i,
                is_bot=False,
                first_name=f"PageUser{i}",
                username=f"page_user_{i}",
            )
        )
    await session.commit()

    registry = FakeRegistry()

    # Page 1
    t1, kb1 = await _render_users_page(session, registry, page=1)
    assert "مدیریت و لیست کاربران" in t1
    assert "صفحه:" in t1
    assert "از" in t1
    assert len(kb1.as_markup().inline_keyboard) >= 3

    # Page 2
    t2, _kb2 = await _render_users_page(session, registry, page=2)
    assert "صفحه:" in t2
    assert "از" in t2

    # Search existing user
    t_search, _ = await _render_users_page(session, registry, page=1, query="page_user_7")
    assert "نتایج جستجو برای:" in t_search
    assert "PageUser7" in t_search

    # Search nonexistent user
    t_none, _ = await _render_users_page(session, registry, page=1, query="nonexistent_xyz_999")
    assert "هیچ کاربری با این مشخصات یافت نشد" in t_none


async def test_admin_user_detail_card_and_ban_toggle(
    app_dispatcher: Dispatcher, session: AsyncSession
) -> None:
    """Verify profile inspection card and ban/unban toggling."""
    from aiogram.types import Update

    target_id = 999_600_000_123
    user_repo = UserRepo(session)
    await user_repo.upsert_from_telegram(
        TgUser(
            id=target_id,
            is_bot=False,
            first_name="CardTest",
            username="card_test_user",
        )
    )
    await session.commit()

    registry = FakeRegistry()
    card_text, _card_kb = await _render_user_detail(target_id, 1, session, registry)
    assert "کارت مشخصات و مدیریت کاربر" in card_text
    assert str(target_id) in card_text
    assert "CardTest" in card_text
    assert "@card_test_user" in card_text

    bot = RecordingBot()
    try:
        # Toggle ban -> ban
        cb_ban = CallbackQuery(
            id="c_ban_1",
            from_user=TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin"),
            message=Message(
                message_id=20,
                date=datetime.datetime.now(datetime.UTC),
                chat=Chat(id=ADMIN_ID, type="private"),
                text="User Card",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
            ),
            chat_instance="ci",
            data=f"admin_user:toggle_ban:{target_id}:1",
        )
        await app_dispatcher.feed_update(bot, Update(update_id=20, callback_query=cb_ban))
        session.expire_all()
        user_after = await user_repo.get(target_id)
        assert user_after is not None
        assert user_after.is_banned is True

        # Toggle ban -> unban
        cb_unban = CallbackQuery(
            id="c_ban_2",
            from_user=TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin"),
            message=Message(
                message_id=21,
                date=datetime.datetime.now(datetime.UTC),
                chat=Chat(id=ADMIN_ID, type="private"),
                text="User Card",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
            ),
            chat_instance="ci",
            data=f"admin_user:toggle_ban:{target_id}:1",
        )
        await app_dispatcher.feed_update(bot, Update(update_id=21, callback_query=cb_unban))
        session.expire_all()
        user_unbanned = await user_repo.get(target_id)
        assert user_unbanned is not None
        assert user_unbanned.is_banned is False

    finally:
        await bot.session.close()


async def test_admin_user_search_flow_e2e(
    app_dispatcher: Dispatcher, session: AsyncSession
) -> None:
    """E2E search prompt, query input, and result rendering."""
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.types import Update

    bot = RecordingBot()
    try:
        # 1. Tap Search Button
        cb_search = CallbackQuery(
            id="c_search_1",
            from_user=TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin"),
            message=Message(
                message_id=30,
                date=datetime.datetime.now(datetime.UTC),
                chat=Chat(id=ADMIN_ID, type="private"),
                text="Users List",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[]),
            ),
            chat_instance="ci",
            data="admin_users:search",
        )
        await app_dispatcher.feed_update(bot, Update(update_id=30, callback_query=cb_search))
        edits = [m for m in bot.sent if type(m).__name__ == "EditMessageText"]
        assert len(edits) >= 1
        assert "جستجوی کاربر در سیستم" in str(edits[-1].text)

        # 2. Send query message while in search state
        key = StorageKey(bot_id=bot.id, chat_id=ADMIN_ID, user_id=ADMIN_ID)
        state = FSMContext(app_dispatcher.storage, key)
        from app.admin.router import UserSearchSG

        await state.set_state(UserSearchSG.waiting_for_query)

        search_msg = Message(
            message_id=31,
            date=datetime.datetime.now(datetime.UTC),
            chat=Chat(id=ADMIN_ID, type="private"),
            from_user=TgUser(id=ADMIN_ID, is_bot=False, first_name="Admin"),
            text="card_test_user",
        )
        await app_dispatcher.feed_update(bot, Update(update_id=31, message=search_msg))
        sents = [m for m in bot.sent if type(m).__name__ == "SendMessage"]
        assert len(sents) >= 1
        assert "نتایج جستجو برای:" in str(sents[-1].text)
        assert "card_test_user" in str(sents[-1].text)

    finally:
        await bot.session.close()
