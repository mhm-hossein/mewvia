"""Entrypoint: `python -m app`.

Starts long polling, runs the heartbeat alongside it, and shuts both down
cleanly on SIGTERM (docker stop) or SIGINT.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys

from aiogram.exceptions import TelegramUnauthorizedError
from aiogram.types import (
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
)
from aiogram.utils.token import TokenValidationError, validate_token

from app import __version__
from app.admin.autobackup import shutdown_autobackup, sync_autobackup_job
from app.bot import build_bot, build_dispatcher
from app.cache.redis import close_redis, init_redis
from app.config import Settings, get_settings
from app.core.health import PollReadiness, heartbeat
from app.core.logging import get_logger, setup_logging
from app.core.settings_store import init_settings_store
from app.db.seed import apply_seed
from app.db.session import dispose_engine, get_sessionmaker, init_engine, session_scope
from app.media.ffmpeg import configure_ffmpeg_limiter
from app.ui.emoji import init_emoji_registry

log = get_logger(__name__)

PRIVATE_BOT_COMMANDS = [
    BotCommand(command="start", description="شروع مجدد و منوی اصلی"),
    BotCommand(command="cancel", description="انصراف و لغو عملیات جاری"),
    BotCommand(command="shazam", description="تشخیص آهنگ با ویس، ویدیو یا فایل"),
    BotCommand(command="music", description="جستجوی مستقیم موزیک (اسپاتیفای و ساندکلاد)"),
    BotCommand(command="game", description="بازی با میوویا — دوز، گل یا پوچ و بیشتر"),
    BotCommand(command="help", description="راهنما"),
    BotCommand(command="ping", description="بررسی زنده بودن ربات"),
]

GROUP_BOT_COMMANDS = [
    BotCommand(command="media", description="دستیار مدیا (ریپلای روی فایل)"),
    BotCommand(command="music", description="جستجوی مستقیم موزیک (اسپاتیفای و ساندکلاد)"),
    BotCommand(command="shazam", description="تشخیص آهنگ (ریپلای روی ویس یا ویدیو)"),
    BotCommand(command="game", description="منوی بازی‌های گروهی"),
    BotCommand(command="game_xo", description="دوز — بازی دونفره"),
    BotCommand(command="game_gol", description="گل یا پوچ — چهار دست، دونفره"),
    BotCommand(command="game_rps", description="سنگ، کاغذ، قیچی — بازی دونفره"),
    BotCommand(command="game_four", description="چهار در خط — بازی دونفره"),
    BotCommand(command="game_lb", description="رتبه‌بندی همهٔ بازی‌های این گروه"),
]


def _preflight(settings: Settings) -> list[str]:
    """Configuration problems worth a readable message instead of a traceback.

    A crash-looping container with a stack trace is the worst possible answer
    to "I forgot to paste my token", which is the most common first-run mistake.
    """
    problems: list[str] = []

    try:
        validate_token(settings.bot_token)
    except TokenValidationError:
        problems.append(
            "BOT_TOKEN is missing or malformed. Copy it from @BotFather into .env "
            "(it looks like 123456789:AA...)."
        )

    if not settings.admin_ids:
        problems.append(
            "ADMIN_IDS is empty — /health and the future admin panel will be "
            "unreachable. Put your numeric id (see @userinfobot) in .env."
        )

    if not settings.postgres_password:
        problems.append("POSTGRES_PASSWORD is empty.")

    return problems


async def _run() -> None:
    settings = get_settings()
    setup_logging(settings.log_level, settings.log_json)

    if problems := _preflight(settings):
        # One structured line for log aggregation, one human block for the
        # person staring at `docker compose logs`.
        log.error("config_invalid", problems=problems)
        print("\nMewvia cannot start:\n", file=sys.stderr)
        for problem in problems:
            print(f"  • {problem}", file=sys.stderr)
        print("\nEdit .env, then run:  bash dev.sh up\n", file=sys.stderr)
        raise SystemExit(78)  # EX_CONFIG

    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    settings.backup_dir.mkdir(parents=True, exist_ok=True)

    init_engine(settings)
    redis = init_redis(settings)
    init_settings_store(redis)
    init_emoji_registry(redis)
    configure_ffmpeg_limiter(settings)

    # Seed is idempotent and cheap; running it on boot means a fresh deploy needs
    # only `alembic upgrade head`, and newly added slots appear without a manual step.
    try:
        async with session_scope() as session:
            counts = await apply_seed(session, settings)
        log.info("seed_applied", **counts)
    except Exception as exc:
        log.error(
            "seed_failed",
            error=str(exc),
            hint="have migrations been applied? run: bash dev.sh migrate",
        )
        await close_redis()
        await dispose_engine()
        print(
            "\nDatabase is not ready. Run:  bash dev.sh migrate\n",
            file=sys.stderr,
        )
        raise SystemExit(78) from None

    bot = build_bot(settings)
    readiness = settings.health_file.with_suffix(".ready")
    readiness.unlink(missing_ok=True)
    bot.session.middleware(PollReadiness(readiness))
    dp = build_dispatcher(settings, get_sessionmaker())

    try:
        me = await bot.get_me()
    except TelegramUnauthorizedError:
        await bot.session.close()
        await close_redis()
        await dispose_engine()
        log.error("token_rejected")
        print(
            "\nTelegram rejected BOT_TOKEN. Re-copy it from @BotFather into .env.\n",
            file=sys.stderr,
        )
        raise SystemExit(78) from None

    log.info(
        "starting",
        version=__version__,
        bot=me.username,
        bot_id=me.id,
        local_api=settings.uses_local_api,
        admins=len(settings.admin_ids),
    )
    if me.username and me.username.lower() != settings.bot_username.lower():
        log.warning(
            "bot_username_mismatch",
            configured=settings.bot_username,
            actual=me.username,
            hint="sticker pack names must end in _by_<actual username>",
        )

    await bot.set_my_commands(PRIVATE_BOT_COMMANDS, scope=BotCommandScopeAllPrivateChats())
    await bot.set_my_commands(GROUP_BOT_COMMANDS, scope=BotCommandScopeAllGroupChats())

    hb = asyncio.create_task(heartbeat(settings.health_file), name="heartbeat")
    try:
        await sync_autobackup_job(settings, bot)
        # Preserve pending updates across restarts; dropping them loses user requests.
        # tasks_concurrency_limit: bound concurrency under burst load (no Redis/DB starvation).
        await dp.start_polling(
            bot,
            handle_signals=True,
            tasks_concurrency_limit=32,
        )
    finally:
        hb.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await hb
        await shutdown_autobackup()
        await dp.storage.close()
        await bot.session.close()
        await close_redis()
        await dispose_engine()
        log.info("stopped")


def main() -> None:
    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
    except SystemExit as exc:
        # Preserve the exit status so `docker compose ps` and CI see the failure.
        raise SystemExit(exc.code) from None


if __name__ == "__main__":
    main()
