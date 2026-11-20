"""Phase-3 acceptance tests: media engine (probe, specs, ffmpeg, fit, pipeline).

Tests run offline against dynamically generated fixtures using ffmpeg/rlottie inside
the Docker container.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from app.core.errors import ProbeError, UnsupportedMediaError
from app.media.fit import calculate_sticker_dimensions
from app.media.kinds import MediaKind
from app.media.pipeline import convert
from app.media.probe import probe
from app.media.specs import (
    LIMITS_STATIC_STICKER,
    LIMITS_VIDEO_STICKER,
    TARGET_GIF,
    TARGET_MP4,
    TARGET_PNG,
    TARGET_WEBM_STICKER,
    TARGET_WEBP,
    allowed_targets_for,
    get_target,
)

from tests.fixtures.gen import (
    generate_mp4_fixture,
    generate_png_fixture,
    generate_tgs_fixture,
)

pytestmark = pytest.mark.media


# ---------------------------------------------------------------------------
# Dimension calculation arithmetic
# ---------------------------------------------------------------------------


def test_calculate_sticker_dimensions_landscape() -> None:
    dims = calculate_sticker_dimensions(1920, 1080, 512)
    assert dims.width == 512
    assert dims.height <= 512
    assert dims.height % 2 == 0, "height must be even for encoder compatibility"


def test_calculate_sticker_dimensions_portrait() -> None:
    dims = calculate_sticker_dimensions(1080, 1920, 512)
    assert dims.height == 512
    assert dims.width <= 512
    assert dims.width % 2 == 0, "width must be even for encoder compatibility"


def test_calculate_sticker_dimensions_square() -> None:
    dims = calculate_sticker_dimensions(800, 800, 512)
    assert dims.width == 512
    assert dims.height == 512


# ---------------------------------------------------------------------------
# Specs & Conversion Matrix
# ---------------------------------------------------------------------------


def test_target_specs_and_matrix() -> None:
    assert get_target("webp") == TARGET_WEBP
    assert get_target("webm_sticker") == TARGET_WEBM_STICKER

    targets_for_photo = allowed_targets_for(MediaKind.PHOTO)
    target_ids = [t.id for t in targets_for_photo]
    assert "png" in target_ids
    assert "webp" in target_ids
    assert "webm_sticker" in target_ids
    assert "gif" in target_ids


def test_telegram_sticker_limits() -> None:
    assert LIMITS_STATIC_STICKER.max_bytes == 512 * 1024
    assert LIMITS_STATIC_STICKER.exact_dimension == 512

    assert LIMITS_VIDEO_STICKER.max_bytes == 256 * 1024
    assert LIMITS_VIDEO_STICKER.max_duration == 3.0
    assert LIMITS_VIDEO_STICKER.max_fps == 30.0
    assert LIMITS_VIDEO_STICKER.no_audio is True


# ---------------------------------------------------------------------------
# ffprobe probe()
# ---------------------------------------------------------------------------


async def test_probe_png(tmp_path: Path) -> None:
    png_file = tmp_path / "test.png"
    generate_png_fixture(png_file, 640, 480)

    info = await probe(png_file)
    assert info.width == 640
    assert info.height == 480
    assert info.file_size > 0


async def test_probe_mp4(tmp_path: Path) -> None:
    mp4_file = tmp_path / "test.mp4"
    generate_mp4_fixture(mp4_file, 320, 240, duration=1.5)

    info = await probe(mp4_file)
    assert info.width == 320
    assert info.height == 240
    assert 1.0 <= info.duration <= 2.0
    assert info.fps >= 25.0
    assert info.video_codec == "h264"


async def test_probe_nonexistent_file(tmp_path: Path) -> None:
    with pytest.raises(ProbeError, match="file not found"):
        await probe(tmp_path / "missing.mp4")


# ---------------------------------------------------------------------------
# Conversion Pipeline (Offline End-to-End)
# ---------------------------------------------------------------------------


async def test_convert_photo_to_static_webp_sticker(tmp_path: Path) -> None:
    png_file = generate_png_fixture(tmp_path / "src.png", 1024, 768)

    out = await convert(png_file, MediaKind.PHOTO, TARGET_WEBP, tmp_path)

    assert out.path.is_file()
    assert out.info.file_size <= LIMITS_STATIC_STICKER.max_bytes
    assert max(out.info.width, out.info.height) == 512
    assert out.info.video_codec == "webp"


async def test_convert_video_to_webm_video_sticker(tmp_path: Path) -> None:
    mp4_file = generate_mp4_fixture(tmp_path / "src.mp4", 1280, 720, duration=4.0)

    out = await convert(mp4_file, MediaKind.VIDEO, TARGET_WEBM_STICKER, tmp_path)

    assert out.path.is_file()
    assert out.info.file_size <= LIMITS_VIDEO_STICKER.max_bytes
    assert out.info.duration <= 3.05
    assert out.info.fps <= 30.5
    assert max(out.info.width, out.info.height) == 512
    assert out.info.video_codec == "vp9"
    assert not out.info.has_audio


async def test_convert_video_to_gif(tmp_path: Path) -> None:
    mp4_file = generate_mp4_fixture(tmp_path / "src.mp4", 400, 300, duration=2.0)

    out = await convert(mp4_file, MediaKind.VIDEO, TARGET_GIF, tmp_path)

    assert out.path.is_file()
    assert out.info.video_codec == "h264"
    assert not out.info.has_audio


async def test_convert_tgs_to_png_and_webm_sticker(tmp_path: Path) -> None:
    tgs_file = generate_tgs_fixture(tmp_path / "anim.tgs")

    # TGS -> static PNG
    out_png = await convert(tgs_file, MediaKind.STICKER_ANIMATED, TARGET_PNG, tmp_path)
    assert out_png.path.is_file()
    assert out_png.info.video_codec == "png"

    assert len(list((tmp_path / "tgs_frames").glob("frame_*.png"))) == 1

    # TGS -> WEBM video sticker
    out_webm = await convert(tgs_file, MediaKind.STICKER_ANIMATED, TARGET_WEBM_STICKER, tmp_path)
    assert out_webm.path.is_file()
    assert out_webm.info.file_size <= LIMITS_VIDEO_STICKER.max_bytes
    assert out_webm.info.video_codec == "vp9"


async def test_convert_unsupported_kind(tmp_path: Path) -> None:
    png_file = generate_png_fixture(tmp_path / "src.png")

    # PHOTO -> GIF is supported (still-to-animation encoder); PHOTO -> MP4 is
    # the genuinely unsupported pair (TARGET_MP4 takes only video inputs).
    assert MediaKind.PHOTO not in TARGET_MP4.accepts
    with pytest.raises(UnsupportedMediaError):
        await convert(png_file, MediaKind.PHOTO, TARGET_MP4, tmp_path)
