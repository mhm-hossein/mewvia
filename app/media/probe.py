"""ffprobe wrapper returning structured MediaInfo.

Uses `ffprobe` in JSON output mode to extract width, height, duration, fps,
codecs, alpha channel presence, audio stream presence, and file size.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import ProbeError


@dataclass(frozen=True, slots=True)
class MediaInfo:
    path: Path
    width: int
    height: int
    duration: float
    fps: float
    video_codec: str
    audio_codec: str | None
    has_alpha: bool
    has_audio: bool
    file_size: int

    @property
    def is_animated(self) -> bool:
        return self.duration > 0 and self.fps > 0

    @property
    def aspect_ratio(self) -> float:
        return self.width / self.height if self.height > 0 else 1.0


async def probe(path: Path, *, timeout: float = 30.0) -> MediaInfo:  # noqa: ASYNC109
    """Probe a media file using ffprobe.

    Raises ProbeError if ffprobe returns non-zero or invalid output.
    """
    if not path.is_file():  # noqa: ASYNC240
        raise ProbeError(f"file not found: {path}")

    cmd = [
        "ffprobe",
        "-loglevel",
        "error",
        "-print_format",
        "json",
        "-show_entries",
        "format=duration,size:"
        "stream=codec_type,codec_name,width,height,r_frame_rate,duration,pix_fmt",
        str(path),
    ]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            raise ProbeError(f"ffprobe timed out after {timeout}s: {path}") from None
        finally:
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.communicate()
    except ProbeError:
        raise
    except Exception as exc:
        raise ProbeError(f"failed to run ffprobe: {exc}") from exc

    if proc.returncode != 0:
        err_msg = stderr.decode(errors="replace").strip()
        raise ProbeError(f"ffprobe failed: {err_msg}")

    try:
        data = json.loads(stdout.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise ProbeError(f"invalid ffprobe JSON output: {exc}") from exc

    format_info = data.get("format", {})
    streams = data.get("streams", [])

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    if not video_stream and not audio_stream:
        raise ProbeError("no video, image, or audio stream found in file")

    width = int(video_stream.get("width", 0)) if video_stream else 0
    height = int(video_stream.get("height", 0)) if video_stream else 0
    video_codec = str(video_stream.get("codec_name", "")) if video_stream else ""
    audio_codec = str(audio_stream.get("codec_name", "")) if audio_stream else None
    has_audio = audio_stream is not None

    # Calculate FPS
    fps = 0.0
    if video_stream:
        r_frame_rate = video_stream.get("r_frame_rate", "0/0")
        if "/" in r_frame_rate:
            num, den = r_frame_rate.split("/")
            if float(den) > 0:
                fps = float(num) / float(den)

    # Calculate duration
    duration = 0.0
    if video_stream and "duration" in video_stream:
        duration = float(video_stream["duration"])
    elif audio_stream and "duration" in audio_stream:
        duration = float(audio_stream["duration"])
    elif "duration" in format_info:
        duration = float(format_info["duration"])

    # Check alpha channel presence
    has_alpha = False
    if video_stream:
        pix_fmt = str(video_stream.get("pix_fmt", ""))
        has_alpha = any(
            a in pix_fmt for a in ("alpha", "rgba", "yuva420p", "yuva422p", "yuva444p", "gbrap")
        )

    file_size = int(format_info.get("size", path.stat().st_size))  # noqa: ASYNC240

    return MediaInfo(
        path=path,
        width=width,
        height=height,
        duration=duration,
        fps=fps,
        video_codec=video_codec,
        audio_codec=audio_codec,
        has_alpha=has_alpha,
        has_audio=has_audio,
        file_size=file_size,
    )
