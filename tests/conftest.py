"""Shared fixtures.

Two kinds of test live here:

* unit tests, which must be independent of .env and of any running service;
* integration tests, which talk to the real PostgreSQL from docker-compose.

The real database credentials are captured at import time, *before* the
environment-pinning fixture runs, so pinning the environment for unit tests
cannot break the database connection used by integration tests.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest
from aiogram import Dispatcher
from app.config import Settings, get_settings
from sqlalchemy import text
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

# ---------------------------------------------------------------------------
# Real DB credentials, captured before any monkeypatching.
# ---------------------------------------------------------------------------
REAL_DB = {
    "postgres_db": os.environ.get("POSTGRES_DB", "mewvia"),
    "postgres_user": os.environ.get("POSTGRES_USER", "mewvia"),
    "postgres_password": os.environ.get("POSTGRES_PASSWORD", ""),
    "postgres_host": os.environ.get("POSTGRES_HOST", "postgres"),
    "postgres_port": int(os.environ.get("POSTGRES_PORT", "5432")),
}
REAL_REDIS_URL = os.environ.get("REDIS_URL", "redis://redis:6379/0")

DATABASE_URL = Settings(bot_token="1:x", **REAL_DB).database_url  # type: ignore[arg-type]

# Baseline environment for unit tests. Overrides ambient values so a populated
# (or empty) .env cannot change the outcome.
TEST_ENV = {
    "BOT_TOKEN": "111111:TEST-TOKEN-FOR-PYTEST",
    "BOT_USERNAME": "MewviaTestBot",
    "ADMIN_IDS": "1,2",
    "POSTGRES_DB": "mewvia",
    "POSTGRES_USER": "mewvia",
    "POSTGRES_PASSWORD": "test",
    "POSTGRES_HOST": "postgres",
    "POSTGRES_PORT": "5432",
    "REDIS_URL": REAL_REDIS_URL,
    "TELEGRAM_API_URL": "",
    "PREMIUM_EMOJI_ENABLED": "true",
    "LOG_LEVEL": "INFO",
    "LOG_JSON": "true",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[None]:
    """Pin the environment and drop the Settings cache around every test."""
    for key, value in TEST_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("COOKIE_DIR", str(tmp_path / "cookies"))
    monkeypatch.setenv("TMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    # Settings reads .env as a fallback; tests must not.
    monkeypatch.chdir(Path(__file__).parent)

    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Settings:
    """Settings pointed at a temp directory."""
    monkeypatch.setenv("TMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setenv("BACKUP_DIR", str(tmp_path / "backups"))
    get_settings.cache_clear()
    return get_settings()


# ---------------------------------------------------------------------------
# Database fixtures.
#
# Tests run against the real PostgreSQL: an SQLite substitute would not exercise
# ON CONFLICT DO UPDATE, RETURNING, or count(...) FILTER (...), which is exactly
# what the repositories rely on.
#
# Every test is wrapped in a transaction that is rolled back afterwards, so
# tests never see each other's rows and the seeded data survives.
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
async def engine() -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(DATABASE_URL)
    try:
        async with eng.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:  # pragma: no cover - environment problem, not a bug
        await eng.dispose()
        pytest.skip(f"PostgreSQL not reachable ({exc}) — run inside docker compose")
    yield eng
    await eng.dispose()


@pytest.fixture
async def connection(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
    """Outer transaction, always rolled back — the test's writes never persist."""
    async with engine.connect() as conn:
        transaction = await conn.begin()
        try:
            yield conn
        finally:
            await transaction.rollback()


@pytest.fixture
def db_sessionmaker(connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    """Sessionmaker bound to the rolled-back transaction.

    `join_transaction_mode="create_savepoint"` lets the code under test call
    commit() normally; the commit lands on a savepoint inside our transaction.
    """
    return async_sessionmaker(
        bind=connection,
        expire_on_commit=False,
        join_transaction_mode="create_savepoint",
    )


@pytest.fixture
async def session(
    db_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with db_sessionmaker() as db_session:
        yield db_session


# ---------------------------------------------------------------------------
# The application dispatcher.
#
# Feature routers are module-level singletons, and aiogram refuses to attach one
# to a second Dispatcher. The whole suite therefore shares a single instance,
# built by the production `build_dispatcher` so the wiring under test is the real
# wiring. Per-test isolation comes from `current_sessionmaker`, which the
# session-opening helpers are redirected to read.
# ---------------------------------------------------------------------------

current_sessionmaker: dict[str, Any] = {"maker": None}

_APP_DISPATCHER: Dispatcher | None = None


@asynccontextmanager
async def _test_session_scope() -> AsyncIterator[AsyncSession]:
    maker: async_sessionmaker[AsyncSession] = current_sessionmaker["maker"]
    async with maker() as db_session:
        yield db_session
        await db_session.commit()


class _SessionmakerProxy:
    """Sessionmaker-shaped object that defers to the current test's maker.

    DbSessionMiddleware captures its sessionmaker once, at construction; this
    keeps that single instance pointed at whichever transaction is live.
    """

    def __call__(self, **kwargs: Any) -> AsyncSession:
        maker: async_sessionmaker[AsyncSession] = current_sessionmaker["maker"]
        return maker(**kwargs)


@pytest.fixture
def app_dispatcher(
    db_sessionmaker: async_sessionmaker[AsyncSession],
    settings: Settings,
) -> Dispatcher:
    """The one Dispatcher, carrying every real feature router."""
    global _APP_DISPATCHER
    current_sessionmaker["maker"] = db_sessionmaker

    if _APP_DISPATCHER is None:
        import app.core.settings_store as store_module
        import app.ui.emoji as emoji_module
        from app.bot import build_dispatcher
        from app.core.settings_store import init_settings_store
        from app.ui.emoji import init_emoji_registry

        # These modules open their own sessions; redirect them to the test's.
        store_module.session_scope = _test_session_scope  # type: ignore[assignment]
        emoji_module.session_scope = _test_session_scope  # type: ignore[assignment]

        # No Redis: the store and registry read straight through to the database,
        # so a test's writes are visible immediately instead of after a TTL.
        init_settings_store(None)
        init_emoji_registry(None)

        # MemoryStorage rather than Redis, for the same reason.
        _APP_DISPATCHER = build_dispatcher(
            settings.model_copy(update={"redis_url": ""}),
            _SessionmakerProxy(),  # type: ignore[arg-type]
        )

    return _APP_DISPATCHER
