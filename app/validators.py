from typing import Any

from fastapi import HTTPException

from .constants import (
    MAX_IMAGE_COUNT,
    MAX_IMAGE_DIMENSION,
    MAX_IMAGE_PIXELS,
    MIN_IMAGE_DIMENSION,
)


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def clamp_image_count(value: Any) -> int:
    return min(MAX_IMAGE_COUNT, max(1, safe_int(value, 1)))


def parse_size(size: str) -> tuple[int, int] | None:
    width_text, separator, height_text = size.lower().partition("x")
    if separator != "x":
        return None
    try:
        width = int(width_text)
        height = int(height_text)
    except ValueError:
        return None
    return width, height


def validate_size_budget(size: str) -> None:
    if size == "auto":
        return
    parsed = parse_size(size)
    if parsed is None:
        return
    width, height = parsed
    if (
        width < MIN_IMAGE_DIMENSION
        or height < MIN_IMAGE_DIMENSION
        or width > MAX_IMAGE_DIMENSION
        or height > MAX_IMAGE_DIMENSION
    ):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid size '{size}'. Width and height must be between "
                f"{MIN_IMAGE_DIMENSION} and {MAX_IMAGE_DIMENSION}."
            ),
        )
    if width % 16 != 0 or height % 16 != 0:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid size '{size}'. Width and height must both be divisible by 16.",
        )
    if width * height > MAX_IMAGE_PIXELS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid size '{size}'. Requested resolution exceeds the current pixel "
                f"budget of {MAX_IMAGE_PIXELS:,} pixels."
            ),
        )
