from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from app.config import Settings
from app.media import ffmpeg
from app.media.probe import probe


async def test_settings_free_calls_share_boot_limiter(monkeypatch: pytest.MonkeyPatch) -> None:
    ffmpeg.reset_ffmpeg_limiter()
    spawn = AsyncMock(side_effect=AssertionError("concurrency cap bypassed"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    try:
        ffmpeg.configure_ffmpeg_limiter(Settings(max_concurrent_jobs=1))
        async with ffmpeg._slots(1):
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(ffmpeg.run_ffmpeg([]), 0.1)
        spawn.assert_not_awaited()
    finally:
        ffmpeg.reset_ffmpeg_limiter()


async def test_noisy_encoder_only_retains_prefix() -> None:
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import sys; sys.stderr.write('x' * 2000000)",
        stderr=asyncio.subprocess.PIPE,
    )
    result = await asyncio.wait_for(ffmpeg._diagnostics(proc), 10)
    assert proc.returncode == 0
    assert result == b"x" * 2048


async def test_cancelled_probe_reaps_child(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "source"
    path.touch()
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        "import time; time.sleep(60)",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    task = asyncio.create_task(probe(path))
    try:
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert proc.returncode is not None
    finally:
        if proc.returncode is None:
            proc.kill()
            await proc.communicate()
