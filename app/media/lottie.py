"""TGS (Telegram Lottie) animation renderer.

`rlottie-python` bundles native rlottie. Python's default multiprocessing start
method on Linux (fork) can deadlock when native libraries like rlottie are used,
so rendering is executed in a ProcessPoolExecutor with `mp_context=get_context("spawn")`.
"""

from __future__ import annotations

import asyncio
import gzip
from concurrent.futures import ProcessPoolExecutor
from multiprocessing import get_context
from pathlib import Path
from typing import Any

from app.core.errors import ConversionError
from app.core.logging import get_logger

log = get_logger(__name__)

_process_pool: ProcessPoolExecutor | None = None


def _init_process_pool() -> ProcessPoolExecutor:
    global _process_pool
    if _process_pool is None:
        ctx = get_context("spawn")
        _process_pool = ProcessPoolExecutor(max_workers=2, mp_context=ctx)
    return _process_pool


def _render_tgs_worker(
    tgs_path: str, out_dir: str, target_width: int, target_height: int, first_only: bool = False
) -> dict[str, Any]:
    """Worker process: loads TGS via rlottie and saves frames as PNGs."""
    from rlottie_python import LottieAnimation

    tgs_file = Path(tgs_path)
    out_path = Path(out_dir)

    try:
        if tgs_file.suffix == ".tgs":
            with gzip.open(tgs_file) as stream:
                data = stream.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                return {"error": "Lottie document too large"}
            anim = LottieAnimation(data=data.decode("utf-8"))
        else:
            anim = LottieAnimation.from_file(str(tgs_file))
    except Exception as exc:
        return {"error": f"Failed to load Lottie animation: {exc}"}

    total_frames = int(anim.lottie_animation_get_totalframe())
    fps = float(anim.lottie_animation_get_framerate())
    duration = float(anim.lottie_animation_get_duration())

    # Telegram animated stickers are at most 3 seconds at 60 fps. Allow some
    # headroom for other animations without letting a job fill the temp volume.
    frames_to_render = min(1, total_frames) if first_only else total_frames
    if not 1 <= frames_to_render <= 360:
        return {"error": "Unsupported Lottie frame count"}

    frame_paths: list[str] = []
    for frame_num in range(frames_to_render):
        frame_file = out_path / f"frame_{frame_num:04d}.png"
        try:
            with anim.render_pillow_frame(
                frame_num, width=target_width, height=target_height
            ) as img:
                img.save(frame_file, "PNG")
            frame_paths.append(str(frame_file))
        except Exception as exc:
            return {"error": f"Failed to render frame {frame_num}: {exc}"}

    return {
        "total_frames": total_frames,
        "fps": fps,
        "duration": duration,
        "frame_paths": frame_paths,
    }


async def render_tgs_to_frames(
    tgs_path: Path,
    out_dir: Path,
    *,
    target_width: int = 512,
    target_height: int = 512,
    first_only: bool = False,
) -> dict[str, Any]:
    """Render TGS file to a folder of PNG frames in a dedicated process."""
    out_dir.mkdir(parents=True, exist_ok=True)  # noqa: ASYNC240
    pool = _init_process_pool()

    loop = asyncio.get_running_loop()
    res = await loop.run_in_executor(
        pool,
        _render_tgs_worker,
        str(tgs_path),
        str(out_dir),
        target_width,
        target_height,
        first_only,
    )

    if "error" in res:
        raise ConversionError(res["error"])

    return res
