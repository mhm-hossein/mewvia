"""Instagram cookies management router for admin panel.

Provides multi-account Instagram cookie management:
- View all registered accounts, their status (valid / invalid / untested), and usage stats.
- Add new account cookies via .txt document upload or pasted text.
- Live health check on individual accounts or all accounts simultaneously.
- Toggle active/inactive state in download rotation.
- Edit account labels and delete expired/unwanted accounts.
"""

from __future__ import annotations

import asyncio
import tempfile
from datetime import datetime
from pathlib import Path

from aiogram import Bot, F, Router, html
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, Message
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.core.logging import get_logger
from app.core.units import fa_digits
from app.db.models.instagram_cookie import InstagramCookie
from app.db.repo import InstagramCookieRepo
from app.filters import IsAdmin
from app.media.cookie_checker import check_instagram_cookie_health_sync, parse_and_normalize_cookies
from app.ui.emoji import EmojiRegistry, get_emoji_registry
from app.ui.kb import KB, inline_button
from app.ui.styles import Style
from app.ui.texts import t

log = get_logger(__name__)

router = Router(name="admin_cookies")


class CookieAddSG(StatesGroup):
    waiting_for_cookie = State()
    waiting_for_label = State()


class CookieEditSG(StatesGroup):
    waiting_for_label = State()


async def ready_registry() -> EmojiRegistry:
    try:
        registry = get_emoji_registry()
        await registry.refresh_premium_flag()
        await registry.load()
        return registry
    except Exception:
        pass

    class _StandInRegistry:
        def custom_id(self, slot: str) -> str | None:
            return None

        def fallback(self, slot: str) -> str:
            return ""

        def slot(self, name: str) -> None:
            return None

        def is_premium_ready(self) -> bool:
            return False

        async def refresh_premium_flag(self) -> bool:
            return True

        async def load(self) -> dict[str, tuple[str | None, str]]:
            return {}

    return _StandInRegistry()  # type: ignore[return-value]


def _status_badge(c: InstagramCookie) -> str:
    if not c.is_active:
        return "⏸ غیرفعال"
    if c.status == "valid":
        return "🟢 سالم"
    if c.status == "invalid":
        return "🔴 منقضی"
    return "⚪️ بررسی‌نشده"


def _format_datetime(dt: datetime | None) -> str:
    if not dt:
        return "—"
    return fa_digits(dt.strftime("%Y/%m/%d %H:%M"))


def _sync_cookie_file_sync(text: str | None) -> None:
    cookie_dir = get_settings().cookie_dir
    target = cookie_dir / "instagram.txt"
    legacy = cookie_dir.parent / "cookies.txt"
    if text and text.strip():
        cookie_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for path in (target, legacy):
            with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
                staging = Path(tmp.name)
            try:
                staging.write_text(text, encoding="utf-8")
                staging.replace(path)
            finally:
                staging.unlink(missing_ok=True)
    else:
        if target.exists():
            target.unlink(missing_ok=True)
        if legacy.exists():
            legacy.unlink(missing_ok=True)


async def sync_active_cookie_to_disk(session: AsyncSession) -> None:
    """Sync the primary active valid cookie to /data/cookies/instagram.txt for system fallbacks."""
    repo = InstagramCookieRepo(session)
    next_cookie = await repo.get_next_cookie()
    raw = next_cookie.cookie_text if next_cookie else None
    await asyncio.to_thread(_sync_cookie_file_sync, raw)


async def render_cookies_dashboard(
    session: AsyncSession, registry: EmojiRegistry
) -> tuple[str, KB]:
    repo = InstagramCookieRepo(session)
    cookies = await repo.all()

    total_cnt = len(cookies)
    active_cnt = sum(1 for c in cookies if c.is_active)
    valid_cnt = sum(1 for c in cookies if c.is_active and c.status == "valid")
    invalid_cnt = sum(1 for c in cookies if c.status == "invalid")

    header_lines = [
        "🍪 <b>مدیریت کوکی‌های اینستاگرام (چند اکانته)</b>",
        "",
        "📊 <b>وضعیت کلی:</b>",
        f"• کل اکانت‌ها: <code>{fa_digits(str(total_cnt))}</code>",
        f"• فعال در چرخه: <code>{fa_digits(str(active_cnt))}</code>",
        f"• سالم و متصل: <code>{fa_digits(str(valid_cnt))}</code> 🟢",
        f"• منقضی / خطا: <code>{fa_digits(str(invalid_cnt))}</code> 🔴",
        "",
        "<i>ربات به صورت خودکار بین اکانت‌های فعال چرخش می‌کند و در صورت مسدودی، "
        "بدون وقفه به اکانت بعدی سوئیچ می‌کند.</i>",
        "",
    ]

    if not cookies:
        header_lines.append(
            "⚠️ <i>هنوز هیچ اکانتی ثبت نشده است. با دکمه زیر اولین کوکی را اضافه کنید.</i>"
        )
    else:
        header_lines.append("📋 <b>لیست اکانت‌های ثبت‌شده:</b>")

    text = "\n".join(header_lines)

    kb = KB(registry)

    for c in cookies:
        badge = _status_badge(c)
        label_text = f"{badge} | {c.label[:20]}"
        kb.row(
            inline_button(
                label_text,
                callback_data=f"igc:view:{c.id}",
                style=Style.PRIMARY if c.is_active else Style.NEUTRAL,
                emoji_slot="cookie",
                registry=registry,
            )
        )
        kb.row(
            inline_button(
                "توقف" if c.is_active else "فعال‌سازی",
                callback_data=f"igc:toggle:{c.id}",
                style=Style.NEUTRAL if c.is_active else Style.CONFIRM,
                emoji_slot="on" if c.is_active else "off",
                registry=registry,
            ),
            inline_button(
                "تست",
                callback_data=f"igc:test:{c.id}",
                style=Style.PRIMARY,
                emoji_slot="refresh",
                registry=registry,
            ),
            inline_button(
                "حذف",
                callback_data=f"igc:del:{c.id}",
                style=Style.DESTRUCTIVE,
                emoji_slot="delete",
                registry=registry,
            ),
        )

    # Action buttons
    kb.row(
        inline_button(
            "افزودن اکانت جدید",
            callback_data="igc:add",
            style=Style.CONFIRM,
            emoji_slot="add",
            registry=registry,
        ),
        inline_button(
            "تست سلامت همه",
            callback_data="igc:test_all",
            style=Style.PRIMARY,
            emoji_slot="refresh",
            registry=registry,
        ),
    )
    kb.row(
        inline_button(
            t("common.refresh"),
            callback_data="igc:list",
            style=Style.PRIMARY,
            emoji_slot="refresh",
            registry=registry,
        ),
        inline_button(
            t("common.back"),
            callback_data="menu:admin",
            style=Style.PRIMARY,
            emoji_slot="back",
            registry=registry,
        ),
    )

    return text, kb


async def render_cookie_detail(c: InstagramCookie, registry: EmojiRegistry) -> tuple[str, KB]:
    badge = _status_badge(c)
    err_line = (
        f"⚠️ <b>خطای اخیر:</b> <code>{html.quote(c.error_message)}</code>\n"
        if c.error_message
        else ""
    )
    user_line = f"👤 <b>یوزرنیم:</b> <code>@{html.quote(c.username)}</code>\n" if c.username else ""
    acc_id_line = (
        f"🆔 <b>شناسه اینستاگرام:</b> <code>{html.quote(c.account_id)}</code>\n"
        if c.account_id
        else ""
    )

    text = (
        f"🍪 <b>جزئیات اکانت: {html.quote(c.label)}</b>\n\n"
        f"وضعیت فعلی: {badge}\n"
        f"{user_line}"
        f"{acc_id_line}"
        f"• وضعیت چرخه: {'✅ فعال' if c.is_active else '⏸ متوقف شده'}\n"
        f"• دانلودهای موفق: <code>{fa_digits(str(c.use_count))}</code> بار\n"
        f"• خطاهای متوالی: <code>{fa_digits(str(c.fail_count))}</code> بار\n"
        f"• آخرین استفاده: <code>{_format_datetime(c.last_used_at)}</code>\n"
        f"• آخرین بررسی آنلاین: <code>{_format_datetime(c.last_checked_at)}</code>\n"
        f"{err_line}"
    )

    kb = KB(registry)
    kb.row(
        inline_button(
            "تست آنلاین وضعیت",
            callback_data=f"igc:test:{c.id}",
            style=Style.PRIMARY,
            emoji_slot="refresh",
            registry=registry,
        ),
        inline_button(
            "توقف در چرخه" if c.is_active else "فعال در چرخه",
            callback_data=f"igc:toggle:{c.id}",
            style=Style.NEUTRAL if c.is_active else Style.CONFIRM,
            emoji_slot="on" if c.is_active else "off",
            registry=registry,
        ),
    )
    kb.row(
        inline_button(
            "تغییر نام / برچسب",
            callback_data=f"igc:edit_label:{c.id}",
            style=Style.PRIMARY,
            emoji_slot="edit",
            registry=registry,
        ),
        inline_button(
            "خروجی فایل کوکی",
            callback_data=f"igc:export:{c.id}",
            style=Style.NEUTRAL,
            emoji_slot="download",
            registry=registry,
        ),
    )
    kb.row(
        inline_button(
            "حذف اکانت",
            callback_data=f"igc:del:{c.id}",
            style=Style.DESTRUCTIVE,
            emoji_slot="delete",
            registry=registry,
        ),
        inline_button(
            "بازگشت به لیست",
            callback_data="igc:list",
            style=Style.PRIMARY,
            emoji_slot="back",
            registry=registry,
        ),
    )

    return text, kb


# --- Callback Handlers ---


@router.callback_query(F.data == "admin_act:cookies", IsAdmin())
@router.callback_query(F.data == "igc:list", IsAdmin())
async def show_cookies_panel(
    query: CallbackQuery, session: AsyncSession, state: FSMContext | None = None
) -> None:
    if state is not None:
        await state.clear()
    registry = await ready_registry()
    text, kb = await render_cookies_dashboard(session, registry)
    if isinstance(query.message, Message):
        try:
            await query.message.edit_text(text, reply_markup=kb.as_markup())
        except Exception:
            await query.message.answer(text, reply_markup=kb.as_markup())
    await query.answer()


@router.callback_query(F.data.startswith("igc:view:"), IsAdmin())
async def view_cookie(
    query: CallbackQuery, session: AsyncSession, state: FSMContext | None = None
) -> None:
    if state is not None:
        await state.clear()
    cookie_id = int((query.data or "").split(":")[-1])
    repo = InstagramCookieRepo(session)
    cookie = await repo.get_by_id(cookie_id)
    if not cookie:
        await query.answer("اکانت یافت نشد.", show_alert=True)
        return

    registry = await ready_registry()
    text, kb = await render_cookie_detail(cookie, registry)
    if isinstance(query.message, Message):
        try:
            await query.message.edit_text(text, reply_markup=kb.as_markup())
        except Exception:
            await query.message.answer(text, reply_markup=kb.as_markup())
    await query.answer()


@router.callback_query(F.data.startswith("igc:toggle:"), IsAdmin())
async def toggle_cookie(query: CallbackQuery, session: AsyncSession) -> None:
    cookie_id = int((query.data or "").split(":")[-1])
    repo = InstagramCookieRepo(session)
    cookie = await repo.get_by_id(cookie_id)
    if not cookie:
        await query.answer("اکانت یافت نشد.", show_alert=True)
        return

    new_state = not cookie.is_active
    await repo.set_active(cookie_id, new_state)
    await sync_active_cookie_to_disk(session)
    await session.flush()

    msg = "اکانت فعال شد و در چرخه دانلود قرار گرفت." if new_state else "اکانت غیرفعال شد."
    await query.answer(msg)

    registry = await ready_registry()
    text, kb = await render_cookies_dashboard(session, registry)
    if isinstance(query.message, Message):
        try:
            await query.message.edit_text(text, reply_markup=kb.as_markup())
        except Exception:
            pass


@router.callback_query(F.data.startswith("igc:test:"), IsAdmin())
async def test_single_cookie(query: CallbackQuery, session: AsyncSession) -> None:
    cookie_id = int((query.data or "").split(":")[-1])
    repo = InstagramCookieRepo(session)
    cookie = await repo.get_by_id(cookie_id)
    if not cookie:
        await query.answer("اکانت یافت نشد.", show_alert=True)
        return

    await query.answer("در حال بررسی اتصال به اینستاگرام...", show_alert=False)

    _, cookie_dict = parse_and_normalize_cookies(cookie.cookie_text)
    is_valid, account_id, err = await asyncio.to_thread(
        check_instagram_cookie_health_sync, cookie_dict
    )

    status_str = "valid" if is_valid else "invalid"
    await repo.set_status(cookie_id, status_str, error_message=err)
    if account_id and not cookie.account_id:
        cookie.account_id = account_id
    await sync_active_cookie_to_disk(session)
    await session.flush()

    registry = await ready_registry()
    text, kb = await render_cookie_detail(cookie, registry)
    if isinstance(query.message, Message):
        try:
            await query.message.edit_text(text, reply_markup=kb.as_markup())
        except Exception:
            pass


@router.callback_query(F.data == "igc:test_all", IsAdmin())
async def test_all_cookies(query: CallbackQuery, session: AsyncSession) -> None:
    repo = InstagramCookieRepo(session)
    cookies = await repo.all()
    if not cookies:
        await query.answer("هیچ اکانتی برای تست وجود ندارد.", show_alert=True)
        return

    await query.answer("شروع بررسی سلامت تمام اکانت‌ها...", show_alert=False)

    for c in cookies:
        _, cookie_dict = parse_and_normalize_cookies(c.cookie_text)
        is_valid, account_id, err = await asyncio.to_thread(
            check_instagram_cookie_health_sync, cookie_dict
        )
        status_str = "valid" if is_valid else "invalid"
        await repo.set_status(c.id, status_str, error_message=err)
        if account_id and not c.account_id:
            c.account_id = account_id

    await sync_active_cookie_to_disk(session)
    await session.flush()

    registry = await ready_registry()
    text, kb = await render_cookies_dashboard(session, registry)
    if isinstance(query.message, Message):
        try:
            await query.message.edit_text(text, reply_markup=kb.as_markup())
        except Exception:
            pass


@router.callback_query(F.data.startswith("igc:del:"), IsAdmin())
async def delete_cookie_confirm(query: CallbackQuery, session: AsyncSession) -> None:
    cookie_id = int((query.data or "").split(":")[-1])
    repo = InstagramCookieRepo(session)
    cookie = await repo.get_by_id(cookie_id)
    if not cookie:
        await query.answer("اکانت یافت نشد.", show_alert=True)
        return

    registry = await ready_registry()
    kb = KB(registry)
    kb.row(
        inline_button(
            "بله، حذف کن",
            callback_data=f"igc:del_yes:{cookie.id}",
            style=Style.DESTRUCTIVE,
            emoji_slot="delete",
            registry=registry,
        ),
        inline_button(
            "انصراف",
            callback_data=f"igc:view:{cookie.id}",
            style=Style.NEUTRAL,
            emoji_slot="error",
            registry=registry,
        ),
    )

    confirm_text = (
        f"⚠️ <b>آیا از حذف اکانت «{html.quote(cookie.label)}» اطمینان داری؟</b>\n\n"
        "پس از حذف، این کوکی از چرخه دانلود خارج خواهد شد."
    )
    if isinstance(query.message, Message):
        await query.message.edit_text(confirm_text, reply_markup=kb.as_markup())
    await query.answer()


@router.callback_query(F.data.startswith("igc:del_yes:"), IsAdmin())
async def delete_cookie_execute(query: CallbackQuery, session: AsyncSession) -> None:
    cookie_id = int((query.data or "").split(":")[-1])
    repo = InstagramCookieRepo(session)
    await repo.delete(cookie_id)
    await sync_active_cookie_to_disk(session)
    await session.flush()

    await query.answer("اکانت با موفقیت حذف شد.")
    registry = await ready_registry()
    text, kb = await render_cookies_dashboard(session, registry)
    if isinstance(query.message, Message):
        try:
            await query.message.edit_text(text, reply_markup=kb.as_markup())
        except Exception:
            pass


@router.callback_query(F.data.startswith("igc:export:"), IsAdmin())
async def export_cookie_file(query: CallbackQuery, session: AsyncSession, bot: Bot) -> None:
    cookie_id = int((query.data or "").split(":")[-1])
    repo = InstagramCookieRepo(session)
    cookie = await repo.get_by_id(cookie_id)
    if not cookie or not query.message:
        await query.answer("اکانت یافت نشد.", show_alert=True)
        return

    await query.answer()
    await query.message.answer_document(
        document=BufferedInputFile(
            cookie.cookie_text.encode(), filename=f"instagram_{cookie.label}.txt"
        ),
        caption=f"🍪 فایل کوکی اکانت: <b>{html.quote(cookie.label)}</b>",
    )


@router.callback_query(F.data == "igc:add", IsAdmin())
async def add_cookie_start(query: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(CookieAddSG.waiting_for_cookie)

    registry = await ready_registry()
    kb = KB(registry)
    kb.row(
        inline_button(
            "لغو",
            callback_data="igc:list",
            style=Style.DESTRUCTIVE,
            emoji_slot="error",
            registry=registry,
        )
    )

    prompt = (
        f"{registry.text('cookie')} <b>افزودن کوکی جدید اینستاگرام:</b>\n\n"
        "لطفاً فایل <code>cookies.txt</code> یا متن کوکی اکانت اینستاگرام را بفرست.\n\n"
        f"{registry.text('idea')} <b>نحوه دریافت:</b>\n"
        "با افزونه <i>Get cookies.txt LOCALLY</i> مرورگر، از سایت اینستاگرام خروجی بگیر "
        "و فایل را مستقیماً همین‌جا آپلود کن."
    )
    if isinstance(query.message, Message):
        await query.message.edit_text(prompt, reply_markup=kb.as_markup())
    await query.answer()


@router.message(CookieAddSG.waiting_for_cookie, IsAdmin(), F.document)
async def process_cookie_doc(message: Message, bot: Bot, state: FSMContext) -> None:
    doc = message.document
    if not doc:
        return

    file_info = await bot.get_file(doc.file_id)
    if not file_info.file_path:
        await message.reply("❌ خطا در دریافت فایل از تلگرام.")
        return

    file_bytes = await bot.download_file(file_info.file_path)
    if not file_bytes:
        await message.reply("❌ دریافت فایل ناموفق بود.")
        return

    raw_text = file_bytes.read().decode("utf-8", errors="ignore")
    await _handle_cookie_content(message, raw_text, state)


@router.message(CookieAddSG.waiting_for_cookie, IsAdmin(), F.text)
async def process_cookie_text(message: Message, state: FSMContext) -> None:
    raw_text = message.text or ""
    await _handle_cookie_content(message, raw_text, state)


async def _handle_cookie_content(message: Message, raw_text: str, state: FSMContext) -> None:
    netscape_text, cookie_dict = parse_and_normalize_cookies(raw_text)

    if "sessionid" not in cookie_dict:
        await message.reply(
            "❌ <b>کوکی نامعتبر است!</b>\n\n"
            "فایل ارسالی فاقد مقدار <code>sessionid</code> (شناسه نشست کاربری) است.\n"
            "لطفاً مطمئن شو که قبل از خروجی گرفتن، وارد اکانت شده‌ای و مجدداً ارسال کن."
        )
        return

    account_id = cookie_dict.get("ds_user_id")
    await state.update_data(
        netscape_text=netscape_text,
        cookie_dict=cookie_dict,
        account_id=account_id,
    )
    await state.set_state(CookieAddSG.waiting_for_label)

    registry = await ready_registry()
    kb = KB(registry)
    kb.row(
        inline_button(
            "استفاده از نام خودکار",
            callback_data="igc:use_auto_label",
            style=Style.CONFIRM,
            emoji_slot="ok",
            registry=registry,
        ),
        inline_button(
            "لغو",
            callback_data="igc:list",
            style=Style.DESTRUCTIVE,
            emoji_slot="error",
            registry=registry,
        ),
    )

    acc_info = f" (شناسه: <code>{account_id}</code>)" if account_id else ""
    await message.reply(
        f"✅ <b>کوکی شناسایی شد!{acc_info}</b>\n\n"
        "لطفاً یک نام یا عنوان برای این اکانت بفرست:\n"
        "<i>(مثلاً: اکانت اصلی، پیج تست، یا آیدی اکانت)</i>",
        reply_markup=kb.as_markup(),
    )


@router.callback_query(F.data == "igc:use_auto_label", IsAdmin(), CookieAddSG.waiting_for_label)
async def use_auto_label(query: CallbackQuery, state: FSMContext, session: AsyncSession) -> None:
    data = await state.get_data()
    account_id = data.get("account_id")
    repo = InstagramCookieRepo(session)
    count = await repo.count()
    label = f"اکانت {count + 1}" if not account_id else f"اکانت {account_id[:8]}"
    target = query.message if isinstance(query.message, Message) else None
    await _finalize_cookie_addition(target, label, state, session)
    await query.answer()


@router.message(CookieAddSG.waiting_for_label, IsAdmin(), F.text)
async def process_cookie_label(message: Message, state: FSMContext, session: AsyncSession) -> None:
    label = (message.text or "").strip()[:60]
    if not label:
        label = "اکانت اینستاگرام"
    await _finalize_cookie_addition(message, label, state, session)


async def _finalize_cookie_addition(
    target: Message | None,
    label: str,
    state: FSMContext,
    session: AsyncSession,
) -> None:
    data = await state.get_data()
    netscape_text = data.get("netscape_text", "")
    cookie_dict = data.get("cookie_dict", {})
    account_id = data.get("account_id")

    await state.clear()

    # Run quick live health check
    is_valid, _, err = await asyncio.to_thread(check_instagram_cookie_health_sync, cookie_dict)
    status_str = "valid" if is_valid else "invalid"

    repo = InstagramCookieRepo(session)
    new_cookie = await repo.add(
        label=label,
        cookie_text=netscape_text,
        account_id=account_id,
        status=status_str,
    )
    if not is_valid:
        new_cookie.error_message = err

    await sync_active_cookie_to_disk(session)
    await session.flush()

    status_badge = "🟢 سالم و فعال در چرخه" if is_valid else f"⚠️ نیاز به بررسی ({err})"
    res_msg = (
        f"🎉 <b>اکانت «{html.quote(label)}» با موفقیت ذخیره شد!</b>\n\n"
        f"وضعیت بررسی اولیه: {status_badge}\n"
        "این کوکی به جمع اکانت‌های دانلودر اضافه شد و سیستم به صورت خودکار از آن استفاده خواهد کرد."
    )

    registry = await ready_registry()
    _, kb = await render_cookies_dashboard(session, registry)
    if target:
        await target.answer(res_msg, reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("igc:edit_label:"), IsAdmin())
async def edit_label_start(query: CallbackQuery, state: FSMContext) -> None:
    cookie_id = int((query.data or "").split(":")[-1])
    await state.set_state(CookieEditSG.waiting_for_label)
    await state.update_data(cookie_id=cookie_id)

    registry = await ready_registry()
    kb = KB(registry)
    kb.row(
        inline_button(
            "❌ لغو",
            callback_data=f"igc:view:{cookie_id}",
            style=Style.DESTRUCTIVE,
            emoji_slot="error",
            registry=registry,
        )
    )
    prompt = "✏️ <b>لطفاً نام یا برچسب جدید این اکانت را ارسال کن:</b>"
    if isinstance(query.message, Message):
        await query.message.edit_text(prompt, reply_markup=kb.as_markup())
    await query.answer()


@router.message(CookieEditSG.waiting_for_label, IsAdmin(), F.text)
async def process_new_label(message: Message, state: FSMContext, session: AsyncSession) -> None:
    new_label = (message.text or "").strip()[:60]
    if not new_label:
        await message.reply("لطفاً یک نام معتبر بفرست.")
        return

    data = await state.get_data()
    cookie_id = data.get("cookie_id")
    await state.clear()

    repo = InstagramCookieRepo(session)
    if cookie_id:
        await repo.update_label(cookie_id, new_label)
        await session.flush()

    cookie = await repo.get_by_id(cookie_id) if cookie_id else None
    registry = await ready_registry()
    if cookie:
        _, kb = await render_cookie_detail(cookie, registry)
        await message.reply(
            f"✅ نام به «{html.quote(new_label)}» تغییر یافت.",
            reply_markup=kb.as_markup(),
        )
    else:
        _, kb = await render_cookies_dashboard(session, registry)
        await message.reply("✅ نام تغییر یافت.", reply_markup=kb.as_markup())
