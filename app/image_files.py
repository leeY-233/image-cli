import uuid
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageOps, UnidentifiedImageError

from .config import settings
from .constants import THUMBNAIL_MAX_EDGE, THUMBNAIL_QUALITY
from .validators import safe_int


def file_path_from_url(file_url: str) -> Path | None:
    if not file_url.startswith("/files/"):
        return None
    return settings.output_dir / Path(file_url.removeprefix("/files/")).name


def png_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    if data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 4 or not data.startswith(b"\xff\xd8"):
        return None
    index = 2
    while index + 9 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        index += 2
        if marker in {0xD8, 0xD9}:
            continue
        if index + 2 > len(data):
            return None
        segment_length = int.from_bytes(data[index:index + 2], "big")
        if segment_length < 2 or index + segment_length > len(data):
            return None
        if marker in {
            0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
            0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF,
        }:
            height = int.from_bytes(data[index + 3:index + 5], "big")
            width = int.from_bytes(data[index + 5:index + 7], "big")
            return width, height
        index += segment_length
    return None


def webp_dimensions(data: bytes) -> tuple[int, int] | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk_type = data[12:16]
    if chunk_type == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return width, height
    if chunk_type == b"VP8 " and len(data) >= 30:
        if data[23:26] != b"\x9d\x01\x2a":
            return None
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return width, height
    if chunk_type == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return width, height
    return None


def image_dimensions(path: Path) -> tuple[int, int] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    return png_dimensions(data) or jpeg_dimensions(data) or webp_dimensions(data)


def history_file_metadata(file_url: str) -> dict[str, int | str | None]:
    file_path = file_path_from_url(file_url)
    if file_path is None or not file_path.is_file():
        return {
            "file_size_bytes": None,
            "image_width": None,
            "image_height": None,
            "image_dimensions": "",
        }
    try:
        file_size = file_path.stat().st_size
    except OSError:
        file_size = None
    dimensions = image_dimensions(file_path)
    width, height = dimensions if dimensions else (None, None)
    return {
        "file_size_bytes": file_size,
        "image_width": width,
        "image_height": height,
        "image_dimensions": f"{width}x{height}" if width and height else "",
    }


def thumbnail_file_url(history_id: str) -> str:
    safe_id = "".join(char if char.isalnum() or char in "-_" else "-" for char in history_id)
    return f"/files/thumb-{safe_id or uuid.uuid4().hex}.webp"


def thumbnail_metadata(file_url: str) -> dict[str, int | str | None]:
    metadata = history_file_metadata(file_url)
    return {
        "thumbnail_file_size_bytes": metadata["file_size_bytes"],
        "thumbnail_width": metadata["image_width"],
        "thumbnail_height": metadata["image_height"],
        "thumbnail_dimensions": metadata["image_dimensions"],
    }


def generate_history_thumbnail(
    history_id: str,
    file_url: str,
    log_event: Callable[..., None] | None = None,
) -> dict[str, int | str | None]:
    source_path = file_path_from_url(file_url)
    if source_path is None or not source_path.is_file():
        return {}

    thumbnail_url = thumbnail_file_url(history_id)
    thumbnail_path = file_path_from_url(thumbnail_url)
    if thumbnail_path is None:
        return {}
    if thumbnail_path.is_file():
        return {"thumbnail_file": thumbnail_url, **thumbnail_metadata(thumbnail_url)}

    try:
        with Image.open(source_path) as raw_image:
            image = ImageOps.exif_transpose(raw_image)
            image.thumbnail((THUMBNAIL_MAX_EDGE, THUMBNAIL_MAX_EDGE), Image.Resampling.LANCZOS)
            if image.mode not in {"RGB", "RGBA"}:
                image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
            image.save(thumbnail_path, "WEBP", quality=THUMBNAIL_QUALITY, method=6)
    except (OSError, UnidentifiedImageError) as exc:
        if log_event is not None:
            log_event(
                "thumbnail_generate_failed",
                history_id=history_id,
                file=file_url,
                detail=f"{exc.__class__.__name__}: {exc}",
            )
        return {}

    metadata = thumbnail_metadata(thumbnail_url)
    if log_event is not None:
        log_event(
            "thumbnail_generated",
            history_id=history_id,
            file=file_url,
            thumbnail_file=thumbnail_url,
            thumbnail_width=metadata["thumbnail_width"],
            thumbnail_height=metadata["thumbnail_height"],
            thumbnail_file_size_bytes=metadata["thumbnail_file_size_bytes"],
        )
    return {"thumbnail_file": thumbnail_url, **metadata}


def ensure_history_thumbnail(
    record: dict[str, Any],
    log_event: Callable[..., None] | None = None,
) -> None:
    if record.get("status") == "failed":
        return
    file_url = str(record.get("file") or "")
    if not file_url.startswith("/files/"):
        return

    thumbnail_url = str(record.get("thumbnail_file") or "")
    thumbnail_path = file_path_from_url(thumbnail_url) if thumbnail_url else None
    if thumbnail_url and thumbnail_path and thumbnail_path.is_file():
        metadata = thumbnail_metadata(thumbnail_url)
        longest_edge = max(
            safe_int(metadata.get("thumbnail_width")),
            safe_int(metadata.get("thumbnail_height")),
        )
        source_longest_edge = max(
            safe_int(record.get("image_width")),
            safe_int(record.get("image_height")),
        )
        if source_longest_edge > longest_edge and longest_edge < THUMBNAIL_MAX_EDGE:
            thumbnail_path.unlink(missing_ok=True)
        else:
            record["thumbnail_file_size_bytes"] = metadata["thumbnail_file_size_bytes"]
            record["thumbnail_width"] = metadata["thumbnail_width"]
            record["thumbnail_height"] = metadata["thumbnail_height"]
            record["thumbnail_dimensions"] = metadata["thumbnail_dimensions"]
            return

    thumbnail_data = generate_history_thumbnail(
        str(record.get("id") or ""), file_url, log_event
    )
    if thumbnail_data:
        record.update(thumbnail_data)


