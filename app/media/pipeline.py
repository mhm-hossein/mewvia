"""Main media conversion pipeline.

Translates input media into the requested target format according to the
conversion matrix in `app.media.specs`. Handles TGS vector rendering,
ffmpeg video/raster conversions, and quality fitting.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import ConversionError, UnsupportedMediaError
from app.core.logging import get_logger
from app.media.ffmpeg import run_ffmpeg
from app.media.fit import fit_static_webp, fit_video_sticker
from app.media.kinds import MediaKind
from app.media.lottie import render_tgs_to_frames
from app.media.probe import MediaInfo, probe
from app.media.specs import (
    TARGET_GIF,
    TARGET_MP4,
    TARGET_PNG,
    TARGET_WEBM_STICKER,
    TARGET_WEBP,
    TargetSpec,
)
from app.media.watermark import add_watermark_to_image, add_watermark_to_video

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class OutputFile:
    path: Path
    target: TargetSpec
    info: MediaInfo


async def convert(
    src_path: Path,
    src_kind: MediaKind,
    target: TargetSpec,
    work_dir: Path,
) -> OutputFile:
    """Convert input media to target spec.

    `work_dir` is a temporary directory created for this job; output file is
    written inside it.
    """
    if src_kind not in target.accepts:
        raise UnsupportedMediaError(f"cannot convert {src_kind.value} to target format {target.id}")

    out_file = work_dir / f"output.{target.extension}"

    # Branch 1: Animated TGS input
    if src_kind == MediaKind.STICKER_ANIMATED:
        await _convert_from_tgs(src_path, target, out_file, work_dir)
        out_info = await probe(out_file)
        return OutputFile(path=out_file, target=target, info=out_info)

    # Branch 2: Standard probe and conversion for non-TGS media
    info = await probe(src_path)

    # WEBM Video Sticker
    if target.id == TARGET_WEBM_STICKER.id:
        await fit_video_sticker(src_path, out_file, info)

    # Static WEBP Sticker
    elif target.id == TARGET_WEBP.id:
        await fit_static_webp(src_path, out_file, info)

    # PNG Image
    elif target.id == TARGET_PNG.id:
        args = ["-i", str(src_path), "-vframes", "1", str(out_file)]
        await run_ffmpeg(args)

    # GIF (Looped silent MP4)
    elif target.id == TARGET_GIF.id:
        if src_kind in (MediaKind.PHOTO, MediaKind.DOCUMENT_IMAGE, MediaKind.STICKER_STATIC):
            # Convert static photo/sticker to 1-second looping MP4 GIF
            args = [
                "-loop",
                "1",
                "-i",
                str(src_path),
                "-t",
                "1.0",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-vf",
                "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-movflags",
                "+faststart",
                str(out_file),
            ]
        else:
            args = [
                "-i",
                str(src_path),
                "-an",  # no audio for GIF
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(out_file),
            ]
        await run_ffmpeg(args)

    # MP4 Video
    elif target.id == TARGET_MP4.id:
        args = [
            "-i",
            str(src_path),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "128k",
            "-movflags",
            "+faststart",
            str(out_file),
        ]
        if not info.has_audio:
            args = [
                "-i",
                str(src_path),
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(out_file),
            ]
        await run_ffmpeg(args)

    else:
        raise ConversionError(f"unhandled target format: {target.id}")

    # Add watermark only when size limits won't be violated (photos, videos, GIFs)
    if target.id in (TARGET_PNG.id, TARGET_GIF.id, TARGET_MP4.id):
        try:
            await add_watermark_to_video(out_file, out_file) if target.id in (
                TARGET_GIF.id,
                TARGET_MP4.id,
            ) else await add_watermark_to_image(out_file, out_file)
        except Exception as exc:
            log.warning("watermark_skipped", error=str(exc))

    out_info = await probe(out_file)
    return OutputFile(path=out_file, target=target, info=out_info)


async def _convert_from_tgs(
    tgs_path: Path,
    target: TargetSpec,
    out_file: Path,
    work_dir: Path,
) -> OutputFile:
    """Render TGS to PNG frames first, then encode to target format."""
    frames_dir = work_dir / "tgs_frames"
    res = await render_tgs_to_frames(
        tgs_path, frames_dir, first_only=target.id in (TARGET_PNG.id, TARGET_WEBP.id)
    )

    fps = res.get("fps", 30.0)

    # Frame sequence pattern for ffmpeg
    frame_pattern = str(frames_dir / "frame_%04d.png")

    if target.id == TARGET_PNG.id:
        # First frame as static PNG
        first_frame = frames_dir / "frame_0000.png"
        if not first_frame.is_file():
            raise ConversionError("TGS frame 0 rendering failed")
        shutil.copy(first_frame, out_file)

    elif target.id == TARGET_WEBP.id:
        first_frame = frames_dir / "frame_0000.png"
        frame_info = await probe(first_frame)
        await fit_static_webp(first_frame, out_file, frame_info)

    elif target.id == TARGET_WEBM_STICKER.id:
        # Create temporary MP4 video from frames, then run fit_video_sticker
        temp_mp4 = work_dir / "temp_tgs.mp4"
        args = [
            "-framerate",
            f"{fps:.2f}",
            "-i",
            frame_pattern,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(temp_mp4),
        ]
        await run_ffmpeg(args)
        temp_info = await probe(temp_mp4)
        await fit_video_sticker(temp_mp4, out_file, temp_info)

    elif target.id in (TARGET_GIF.id, TARGET_MP4.id):
        args = [
            "-framerate",
            f"{fps:.2f}",
            "-i",
            frame_pattern,
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-movflags",
            "+faststart",
            str(out_file),
        ]
        await run_ffmpeg(args)

    else:
        raise ConversionError(f"unhandled target format for TGS: {target.id}")

    out_info = await probe(out_file)
    return OutputFile(path=out_file, target=target, info=out_info)
