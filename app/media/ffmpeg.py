"""ffmpeg async execution helper.

Runs ffmpeg as a subprocess with a configurable timeout, captures stderr for
diagnostics, and raises structured ConversionError or ConversionTimeoutError.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from app.config import Settings
from app.core.errors import ConversionError, ConversionTimeoutError
from app.core.logging import get_logger

log = get_logger(__name__)

DEFAULT_TIMEOUT = 120  # seconds — fallback when the caller passes no settings.

# Process-wide cap on concurrent ffmpeg invocations. Every heavy media path
# funnels through run_ffmpeg, so one semaphore here bounds total CPU/memory
# pressure no matter which feature (or how many users) trigger jobs at once.
# Sized lazily from the first settings seen; tests reset via reset helper.
_ffmpeg_slots: asyncio.Semaphore | None = None


def reset_ffmpeg_limiter() -> None:
    """Drop the cached semaphore (test isolation)."""
    global _ffmpeg_slots
    _ffmpeg_slots = None


def configure_ffmpeg_limiter(settings: Settings) -> None:
    """Size the process-wide ffmpeg semaphore from config. Call once at boot."""
    _slots(settings.max_concurrent_jobs)


def _slots(size: int | None = None) -> asyncio.Semaphore:
    global _ffmpeg_slots
    if _ffmpeg_slots is None:
        size = max(1, size if size is not None else 4)
        _ffmpeg_slots = asyncio.Semaphore(size)
        log.info("ffmpeg_limiter_configured", max_concurrent_jobs=size)
    return _ffmpeg_slots


async def _diagnostics(proc: asyncio.subprocess.Process) -> bytes:
    """Drain even a noisy encoder without retaining its whole error stream."""
    assert proc.stderr is not None
    prefix = bytearray()
    while chunk := await proc.stderr.read(65536):
        prefix.extend(chunk[: 2048 - len(prefix)])
    await proc.wait()
    return bytes(prefix)


async def run_ffmpeg(
    args: list[str],
    *,
    timeout: float | None = None,  # noqa: ASYNC109
    cwd: Path | None = None,
    settings: Settings | None = None,
) -> None:
    """Run ffmpeg with arguments.

    `args` should NOT include the leading "ffmpeg" binary command.
    Timeout precedence: explicit `timeout` > `settings.job_timeout` > 120s.
    """
    if timeout is None:
        timeout = float(settings.job_timeout) if settings is not None else DEFAULT_TIMEOUT
    slots = _slots(settings.max_concurrent_jobs if settings is not None else None)
    cmd = ["ffmpeg", "-y", "-loglevel", "error", *args]

    log.debug("ffmpeg_start", cmd=" ".join(cmd))

    # The semaphore bounds total ffmpeg concurrency process-wide: extra jobs
    # wait here instead of OOMing the container under burst load.
    async with slots:
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
        except Exception as exc:
            raise ConversionError(f"failed to spawn ffmpeg: {exc}") from exc

        try:
            stderr = await asyncio.wait_for(_diagnostics(proc), timeout=timeout)
        except TimeoutError:
            log.warning("ffmpeg_timeout", timeout=timeout, cmd=" ".join(cmd[:10]))
            raise ConversionTimeoutError(f"ffmpeg conversion timed out after {timeout}s") from None
        except asyncio.CancelledError:
            log.warning("ffmpeg_cancelled", cmd=" ".join(cmd[:10]))
            raise
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await _diagnostics(proc)

    if proc.returncode != 0:
        err_text = stderr.decode("utf-8", errors="replace").strip()
        log.error("ffmpeg_failed", returncode=proc.returncode, stderr=err_text[:500])
        raise ConversionError(f"ffmpeg error: {err_text[:200]}")
