import asyncio
import time
import uuid
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from ..auth import require_auth as _require_auth
from ..constants import DEFAULT_GALLERY_ID
from ..edit_uploads import (
    _read_edit_uploads,
    _save_edit_job_file,
)
from ..generation_service import (
    JOB_TASKS,
    _execute_edit,
    _execute_generation,
    _job_public,
    _payload_from_edit_form,
    _payload_from_request,
    _run_generation_job,
)
from ..library_service import (
    _can_access_gallery,
    _create_job_with_limit,
    _delete_file_url,
    _get_job,
    _load_jobs,
    _require_gallery_access,
    _update_job,
)
from ..schemas import GenerateImageRequest, GenerateImageResponse
from ..telemetry import log_event as _log_event


router = APIRouter()


@router.post("/v1/jobs")
async def create_job(request: Request, generation: GenerateImageRequest) -> dict[str, Any]:
    _require_auth(request)
    payload = _payload_from_request(generation)
    _require_gallery_access(request, str(payload.get("gallery_id") or DEFAULT_GALLERY_ID))
    job_id = uuid.uuid4().hex
    now = int(time.time())
    job = {
        "id": job_id,
        "status": "queued",
        "prompt": generation.prompt,
        "payload": payload,
        "created_at": now,
        "updated_at": now,
    }
    _create_job_with_limit(job)
    task = asyncio.create_task(_run_generation_job(job_id))
    JOB_TASKS[job_id] = task
    _log_event("job_created", job_id=job_id, request_id=job_id, payload=payload)
    return _job_public(job)

@router.post("/v1/edit/jobs")
async def create_edit_job(
    http_request: Request,
    image: list[UploadFile] = File(...),
    mask: UploadFile | None = File(default=None),
    prompt: str = Form(...),
    provider_id: str | None = Form(default=None),
    gallery_id: str | None = Form(default=None),
    model: str | None = Form(default=None),
    size: str | None = Form(default=None),
    quality: str | None = Form(default=None),
    output_format: str | None = Form(default=None),
    n: int = Form(default=1),
    response_format: str | None = Form(default=None),
    extra: str | None = Form(default=None),
    source_file: str | None = Form(default=None),
    source_history_id: str | None = Form(default=None),
) -> dict[str, Any]:
    _require_auth(http_request)
    source_images, mask_image = await _read_edit_uploads(image, mask)
    payload = _payload_from_edit_form(
        prompt=prompt,
        provider_id=provider_id,
        gallery_id=gallery_id,
        model=model,
        size=size,
        quality=quality,
        output_format=output_format,
        n=n,
        response_format=response_format,
        extra=extra,
    )
    _require_gallery_access(http_request, str(payload.get("gallery_id") or DEFAULT_GALLERY_ID))
    persisted_files: list[str] = []
    try:
        source_files = [
            _save_edit_job_file(source_image, "edit-source", index)
            for index, source_image in enumerate(source_images, start=1)
        ]
        persisted_files.extend(str(source_file.get("file") or "") for source_file in source_files)
        mask_file = _save_edit_job_file(mask_image, "edit-mask") if mask_image else None
        if mask_file:
            persisted_files.append(str(mask_file.get("file") or ""))
        job_id = uuid.uuid4().hex
        now = int(time.time())
        job = {
            "id": job_id,
            "operation": "edit",
            "status": "queued",
            "prompt": prompt,
            "payload": payload,
            "edit_inputs": {
                "source_files": source_files,
                "mask_file": mask_file,
                "source_file": source_file if source_file and source_file.startswith("/files/") else None,
                "source_history_id": source_history_id or None,
            },
            "created_at": now,
            "updated_at": now,
        }
        _create_job_with_limit(job)
    except Exception:
        for file_url in persisted_files:
            _delete_file_url(file_url)
        raise

    task = asyncio.create_task(_run_generation_job(job_id))
    JOB_TASKS[job_id] = task
    _log_event("job_created", job_id=job_id, request_id=job_id, operation="edit", payload=payload)
    return _job_public(job)

@router.get("/v1/jobs")
def list_jobs(request: Request) -> dict[str, list[dict[str, Any]]]:
    _require_auth(request)
    jobs = [
        job
        for job in _load_jobs()
        if _can_access_gallery(
            request,
            str(
                (job.get("payload") if isinstance(job.get("payload"), dict) else {}).get("gallery_id")
                or DEFAULT_GALLERY_ID
            ),
        )
    ]
    return {"jobs": [_job_public(job) for job in jobs]}

@router.get("/v1/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    job = _get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    _require_gallery_access(request, str(payload.get("gallery_id") or DEFAULT_GALLERY_ID))
    return _job_public(job)

@router.post("/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    job = _get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    _require_gallery_access(request, str(payload.get("gallery_id") or DEFAULT_GALLERY_ID))
    if job.get("status") in {"succeeded", "failed", "cancelled"}:
        return _job_public(job)

    task = JOB_TASKS.get(job_id)
    if task is not None:
        task.cancel()
    job = _update_job(
        job_id,
        status="cancelled",
        cancelled_at=int(time.time()),
        note="Cancelled locally. Provider may still finish remotely if it already started.",
    )
    _log_event("job_cancel_requested", job_id=job_id, request_id=job_id)
    return _job_public(job)

@router.post("/v1/edit", response_model=GenerateImageResponse)
async def edit_image(
    http_request: Request,
    image: list[UploadFile] = File(...),
    mask: UploadFile | None = File(default=None),
    prompt: str = Form(...),
    provider_id: str | None = Form(default=None),
    gallery_id: str | None = Form(default=None),
    model: str | None = Form(default=None),
    size: str | None = Form(default=None),
    quality: str | None = Form(default=None),
    output_format: str | None = Form(default=None),
    n: int = Form(default=1),
    response_format: str | None = Form(default=None),
    extra: str | None = Form(default=None),
) -> GenerateImageResponse:
    request_id = uuid.uuid4().hex
    started_at = time.monotonic()
    _require_auth(http_request)
    source_images, mask_image = await _read_edit_uploads(image, mask)
    payload = _payload_from_edit_form(
        prompt=prompt,
        provider_id=provider_id,
        gallery_id=gallery_id,
        model=model,
        size=size,
        quality=quality,
        output_format=output_format,
        n=n,
        response_format=response_format,
        extra=extra,
    )
    _require_gallery_access(http_request, str(payload.get("gallery_id") or DEFAULT_GALLERY_ID))
    return await _execute_edit(
        request_id=request_id,
        payload=payload,
        source_images=source_images,
        mask_image=mask_image,
        started_at=started_at,
        client_host=http_request.client.host if http_request.client else None,
    )

@router.post("/v1/generate", response_model=GenerateImageResponse)
async def generate_image(
    http_request: Request, request: GenerateImageRequest
) -> GenerateImageResponse:
    request_id = uuid.uuid4().hex
    started_at = time.monotonic()
    _require_auth(http_request)
    payload = _payload_from_request(request)
    _require_gallery_access(http_request, str(payload.get("gallery_id") or DEFAULT_GALLERY_ID))
    return await _execute_generation(
        request_id=request_id,
        payload=payload,
        prompt=request.prompt,
        started_at=started_at,
        client_host=http_request.client.host if http_request.client else None,
    )
