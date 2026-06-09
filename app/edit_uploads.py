import uuid
from pathlib import Path
from typing import Any

from fastapi import HTTPException, UploadFile

from .config import settings
from .constants import (
    MAX_EDIT_SOURCE_IMAGES,
    MAX_EDIT_UPLOAD_BYTES,
    SUPPORTED_EDIT_IMAGE_TYPES,
)
from .library_service import _delete_file_url


def _upload_suffix(filename: str | None, content_type: str | None) -> str:
    if content_type == "image/jpeg":
        return ".jpg"
    if content_type == "image/webp":
        return ".webp"
    if content_type == "image/png":
        return ".png"
    suffix = Path(filename or "").suffix.lower()
    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        return ".jpg" if suffix == ".jpeg" else suffix
    return ".png"

async def _read_edit_upload(
    upload: UploadFile, field_name: str
) -> dict[str, str | bytes]:
    filename = Path(upload.filename or f"{field_name}.png").name
    content_type = upload.content_type or "application/octet-stream"
    if content_type not in SUPPORTED_EDIT_IMAGE_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{field_name} '{filename}' must be a png, jpeg, or webp image "
                f"(received {content_type})."
            ),
        )

    content = await upload.read()
    if not content:
        raise HTTPException(status_code=400, detail=f"{field_name} '{filename}' is empty")
    if len(content) > MAX_EDIT_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=(
                f"{field_name} '{filename}' is too large. "
                f"Maximum size is {MAX_EDIT_UPLOAD_BYTES // (1024 * 1024)}MB."
            ),
        )

    return {
        "filename": filename,
        "content_type": content_type,
        "content": content,
    }

async def _read_edit_uploads(
    image: list[UploadFile], mask: UploadFile | None
) -> tuple[list[dict[str, str | bytes]], dict[str, str | bytes] | None]:
    if not image:
        raise HTTPException(status_code=400, detail="At least one source image is required")
    if len(image) > MAX_EDIT_SOURCE_IMAGES:
        raise HTTPException(
            status_code=400,
            detail=f"At most {MAX_EDIT_SOURCE_IMAGES} source images are supported",
        )
    source_images = [
        await _read_edit_upload(upload, f"image[{index}]")
        for index, upload in enumerate(image, start=1)
    ]
    mask_image = await _read_edit_upload(mask, "mask") if mask is not None else None
    return source_images, mask_image

def _save_uploaded_source_copy(source_image: dict[str, str | bytes]) -> str:
    suffix = _upload_suffix(
        str(source_image.get("filename") or ""),
        str(source_image.get("content_type") or ""),
    )
    filename = f"{uuid.uuid4().hex}-source{suffix}"
    output_path = settings.output_dir / filename
    content = source_image.get("content")
    if not isinstance(content, bytes):
        return ""
    output_path.write_bytes(content)
    return f"/files/{filename}"

def _save_edit_job_file(
    upload_data: dict[str, str | bytes], role: str, index: int = 0
) -> dict[str, str]:
    suffix = _upload_suffix(
        str(upload_data.get("filename") or ""),
        str(upload_data.get("content_type") or ""),
    )
    filename = f"{uuid.uuid4().hex}-{role}{index if index else ''}{suffix}"
    output_path = settings.output_dir / filename
    content = upload_data.get("content")
    if not isinstance(content, bytes):
        raise HTTPException(status_code=400, detail=f"{role} upload is missing content")
    output_path.write_bytes(content)
    return {
        "file": f"/files/{filename}",
        "filename": str(upload_data.get("filename") or filename),
        "content_type": str(upload_data.get("content_type") or "image/png"),
    }

def _read_persisted_edit_file(file_record: dict[str, Any]) -> dict[str, str | bytes]:
    file_url = str(file_record.get("file") or "")
    if not file_url.startswith("/files/"):
        raise HTTPException(status_code=500, detail="Edit job source file is invalid")
    file_path = settings.output_dir / Path(file_url.removeprefix("/files/")).name
    if not file_path.is_file():
        raise HTTPException(status_code=500, detail="Edit job source file is missing")
    return {
        "filename": str(file_record.get("filename") or file_path.name),
        "content_type": str(file_record.get("content_type") or "image/png"),
        "content": file_path.read_bytes(),
        "file": file_url,
    }

def _cleanup_edit_job_files(job: dict[str, Any], preserve_source_files: bool) -> None:
    edit_inputs = job.get("edit_inputs")
    if not isinstance(edit_inputs, dict):
        return
    source_files = edit_inputs.get("source_files")
    if isinstance(source_files, list):
        for source_file in source_files:
            if preserve_source_files:
                continue
            if isinstance(source_file, dict):
                _delete_file_url(str(source_file.get("file") or ""))
    mask_file = edit_inputs.get("mask_file")
    if isinstance(mask_file, dict):
        _delete_file_url(str(mask_file.get("file") or ""))
