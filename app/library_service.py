import base64
import binascii
import hashlib
import hmac
import json
import os
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, Response

from .auth import sign_session as _sign_session
from .config import settings
from .constants import (
    ACTIVE_JOB_STATUSES,
    DEFAULT_GALLERY_ID,
    DEFAULT_GALLERY_NAME,
    GALLERY_PASSWORD_HASH_ALGORITHM,
    GALLERY_PASSWORD_HASH_ITERATIONS,
    GALLERY_UNLOCK_COOKIE,
    GALLERY_UNLOCK_MAX_AGE_SECONDS,
    JOB_LIMIT,
    TRASH_LIMIT,
)
from .image_files import (
    ensure_history_thumbnail as _ensure_history_thumbnail,
    history_file_metadata as _history_file_metadata,
)
from .providers import safe_provider_id as _safe_provider_id
from .storage import (
    json_file_lock as _json_file_lock,
    read_json_list_unlocked as _read_json_list_unlocked,
    write_json_list_unlocked as _write_json_list_unlocked,
)
from .telemetry import log_event as _log_event
from .validators import safe_int as _safe_int


HISTORY_LIMIT = settings.history_limit
MAX_ACTIVE_JOBS = settings.max_active_jobs


def _history_path() -> Path:
    return settings.output_dir / "history.json"

def _jobs_path() -> Path:
    return settings.output_dir / "jobs.json"

def _galleries_path() -> Path:
    return settings.output_dir / "galleries.json"

def _gallery_password_hash(password: str, salt: str | None = None) -> str:
    salt_text = salt or base64.urlsafe_b64encode(os.urandom(16)).decode("utf-8")
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt_text.encode("utf-8"),
        GALLERY_PASSWORD_HASH_ITERATIONS,
    )
    digest_text = base64.urlsafe_b64encode(digest).decode("utf-8")
    return (
        f"{GALLERY_PASSWORD_HASH_ALGORITHM}$"
        f"{GALLERY_PASSWORD_HASH_ITERATIONS}$"
        f"{salt_text}$"
        f"{digest_text}"
    )

def _verify_gallery_password(password: str, stored_hash: str) -> bool:
    try:
        algorithm, iterations_text, salt, digest_text = stored_hash.split("$", 3)
        iterations = int(iterations_text)
    except (ValueError, TypeError):
        return False
    if algorithm != GALLERY_PASSWORD_HASH_ALGORITHM or iterations <= 0:
        return False
    digest = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        salt.encode("utf-8"),
        iterations,
    )
    expected_digest = base64.urlsafe_b64encode(digest).decode("utf-8")
    expected = f"{algorithm}${iterations}${salt}${expected_digest}"
    return hmac.compare_digest(stored_hash, expected)

def _gallery_has_password(gallery: dict[str, Any]) -> bool:
    return bool(str(gallery.get("password_hash") or "").strip())

def _gallery_password_version(gallery: dict[str, Any]) -> int:
    if not _gallery_has_password(gallery):
        return 0
    return (
        _safe_int(gallery.get("password_updated_at"))
        or _safe_int(gallery.get("created_at"))
        or 1
    )

def _new_gallery_password_version() -> int:
    return int(time.time() * 1000)

def _require_gallery_unlock_secret() -> None:
    if settings.session_secret:
        return
    raise HTTPException(
        status_code=503,
        detail=(
            "APP_SESSION_SECRET is required before password-protected galleries "
            "can be used."
        ),
    )

def _decode_gallery_unlocks(request: Request) -> dict[str, dict[str, int]]:
    if not settings.session_secret:
        return {}
    raw_token = request.cookies.get(GALLERY_UNLOCK_COOKIE)
    if not raw_token or "." not in raw_token:
        return {}
    payload_b64, signature = raw_token.rsplit(".", 1)
    expected = _sign_session(f"gallery_unlock:{payload_b64}")
    if not hmac.compare_digest(signature, expected):
        return {}
    try:
        decoded = base64.urlsafe_b64decode(payload_b64.encode("utf-8")).decode("utf-8")
        data = json.loads(decoded)
    except (ValueError, binascii.Error, json.JSONDecodeError):
        return {}
    galleries = data.get("galleries") if isinstance(data, dict) else None
    if not isinstance(galleries, dict):
        return {}
    now = int(time.time())
    unlocks: dict[str, dict[str, int]] = {}
    for raw_gallery_id, raw_entry in galleries.items():
        if not isinstance(raw_entry, dict):
            continue
        gallery_id = _safe_provider_id(raw_gallery_id, "")
        expires_at = _safe_int(raw_entry.get("expires_at"))
        password_version = _safe_int(raw_entry.get("password_version"))
        if not gallery_id or expires_at <= now or not password_version:
            continue
        unlocks[gallery_id] = {
            "expires_at": expires_at,
            "password_version": password_version,
        }
    return unlocks

def _make_gallery_unlock_token(unlocks: dict[str, dict[str, int]]) -> str:
    _require_gallery_unlock_secret()
    now = int(time.time())
    clean_unlocks = {
        _safe_provider_id(gallery_id, ""): {
            "expires_at": _safe_int(entry.get("expires_at")),
            "password_version": _safe_int(entry.get("password_version")),
        }
        for gallery_id, entry in unlocks.items()
        if _safe_provider_id(gallery_id, "")
        and _safe_int(entry.get("expires_at")) > now
        and _safe_int(entry.get("password_version"))
    }
    payload = json.dumps(
        {"galleries": clean_unlocks},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    payload_b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("utf-8")
    signature = _sign_session(f"gallery_unlock:{payload_b64}")
    return f"{payload_b64}.{signature}"

def _set_gallery_unlocked(
    response: Response, request: Request, gallery: dict[str, Any]
) -> None:
    _require_gallery_unlock_secret()
    gallery_id = str(gallery.get("id") or "").strip()
    if not gallery_id or not _gallery_has_password(gallery):
        return
    unlocks = _decode_gallery_unlocks(request)
    unlocks[gallery_id] = {
        "expires_at": int(time.time()) + GALLERY_UNLOCK_MAX_AGE_SECONDS,
        "password_version": _gallery_password_version(gallery),
    }
    response.set_cookie(
        GALLERY_UNLOCK_COOKIE,
        _make_gallery_unlock_token(unlocks),
        max_age=GALLERY_UNLOCK_MAX_AGE_SECONDS,
        httponly=True,
        samesite="lax",
    )

def _is_gallery_unlocked(request: Request, gallery: dict[str, Any]) -> bool:
    return _gallery_unlock_entry(request, gallery) is not None

def _gallery_unlock_entry(
    request: Request, gallery: dict[str, Any]
) -> dict[str, int] | None:
    if not _gallery_has_password(gallery):
        return {"expires_at": 0, "password_version": 0}
    if not settings.session_secret:
        return None
    gallery_id = str(gallery.get("id") or "").strip()
    entry = _decode_gallery_unlocks(request).get(gallery_id)
    if not entry:
        return None
    if _safe_int(entry.get("expires_at")) <= int(time.time()):
        return None
    if _safe_int(entry.get("password_version")) != _gallery_password_version(gallery):
        return None
    return entry

def _normalize_galleries(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen_ids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    has_default = False
    now = int(time.time())
    for record in records:
        gallery_id = _safe_provider_id(record.get("id"), "")
        if not gallery_id or gallery_id in seen_ids:
            continue
        name = str(record.get("name") or gallery_id).strip() or gallery_id
        created_at = _safe_int(record.get("created_at"), now)
        seen_ids.add(gallery_id)
        if gallery_id == DEFAULT_GALLERY_ID:
            has_default = True
        raw_position = record.get("position")
        position: float | None = None
        if raw_position is not None:
            try:
                parsed_position = float(raw_position)
            except (TypeError, ValueError):
                parsed_position = 0.0
            if parsed_position > 0:
                position = parsed_position
        normalized.append(
            {
                "id": gallery_id,
                "name": name,
                "created_at": created_at,
                "_position": position,
            }
        )
        password_hash = str(record.get("password_hash") or "").strip()
        if gallery_id != DEFAULT_GALLERY_ID and password_hash:
            normalized[-1]["password_hash"] = password_hash
            normalized[-1]["password_updated_at"] = (
                _safe_int(record.get("password_updated_at")) or created_at or now
            )
    if not has_default:
        normalized.insert(
            0,
            {
                "id": DEFAULT_GALLERY_ID,
                "name": DEFAULT_GALLERY_NAME,
                "created_at": now,
                "_position": None,
            },
        )
    has_custom_order = any(item.get("_position") is not None for item in normalized)
    if has_custom_order:
        normalized.sort(
            key=lambda item: (
                0 if item.get("_position") is not None else 1,
                _safe_float(item.get("_position"), float(_safe_int(item.get("created_at")))),
                _safe_int(item.get("created_at")),
                str(item.get("name") or ""),
            )
        )
    else:
        # First-run migration keeps the old behavior: default gallery first,
        # then by created_at ascending. Future loads honor saved positions.
        normalized.sort(
            key=lambda item: (
                0 if item["id"] == DEFAULT_GALLERY_ID else 1,
                _safe_int(item.get("created_at")),
                str(item.get("name") or ""),
            )
        )
    for index, item in enumerate(normalized, start=1):
        item["position"] = index
        item.pop("_position", None)
    return normalized

def _load_galleries() -> list[dict[str, Any]]:
    path = _galleries_path()
    with _json_file_lock(path, exclusive=True):
        records = _read_json_list_unlocked(path)
        normalized = _normalize_galleries(records)
        if normalized != records:
            _write_json_list_unlocked(path, normalized)
        return normalized

def _gallery_public(
    gallery: dict[str, Any],
    request: Request | None = None,
    force_unlocked: bool = False,
) -> dict[str, Any]:
    password_protected = _gallery_has_password(gallery)
    unlock_entry = _gallery_unlock_entry(request, gallery) if request else None
    unlocked = force_unlocked or not password_protected or bool(unlock_entry)
    return {
        "id": gallery.get("id", ""),
        "name": gallery.get("name", ""),
        "created_at": _safe_int(gallery.get("created_at")),
        "position": _safe_float(gallery.get("position")),
        "password_protected": password_protected,
        "unlocked": unlocked,
        "unlock_expires_at": (
            int(time.time()) + GALLERY_UNLOCK_MAX_AGE_SECONDS
            if force_unlocked and password_protected
            else _safe_int((unlock_entry or {}).get("expires_at"))
        ),
    }

def _get_gallery(gallery_id: str) -> dict[str, Any] | None:
    text = str(gallery_id or "").strip() or DEFAULT_GALLERY_ID
    for gallery in _load_galleries():
        if gallery["id"] == text:
            return gallery
    return None

def _resolve_gallery_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return DEFAULT_GALLERY_ID
    galleries = _load_galleries()
    if not any(g["id"] == text for g in galleries):
        raise HTTPException(
            status_code=400, detail=f"Gallery '{text}' is not configured"
        )
    return text

def _gallery_locked_exception(gallery: dict[str, Any]) -> HTTPException:
    return HTTPException(
        status_code=403,
        detail={
            "error": {
                "type": "GalleryLocked",
                "message": "这个画廊已设置密码，请先输入密码。",
                "hint": "解锁状态会保存 7 天，过期后需要重新输入。",
            },
            "gallery_id": gallery.get("id", ""),
            "password_required": True,
        },
    )

def _require_gallery_access(request: Request, gallery_id: str | None) -> dict[str, Any]:
    gallery = _get_gallery(gallery_id or DEFAULT_GALLERY_ID)
    if gallery is None:
        raise HTTPException(status_code=404, detail="画廊不存在")
    if not _gallery_has_password(gallery):
        return gallery
    _require_gallery_unlock_secret()
    if _is_gallery_unlocked(request, gallery):
        return gallery
    raise _gallery_locked_exception(gallery)

def _can_access_gallery(request: Request, gallery_id: str | None) -> bool:
    try:
        _require_gallery_access(request, gallery_id)
        return True
    except HTTPException:
        return False

def _require_records_gallery_access(
    request: Request, records: list[dict[str, Any]]
) -> None:
    gallery_ids = {
        str(record.get("gallery_id") or DEFAULT_GALLERY_ID)
        for record in records
    }
    for gallery_id in sorted(gallery_ids):
        _require_gallery_access(request, gallery_id)

def _filter_accessible_records(
    request: Request, records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if _can_access_gallery(request, str(record.get("gallery_id") or DEFAULT_GALLERY_ID))
    ]

def _records_matching_ids(
    records: list[dict[str, Any]], record_ids: list[str]
) -> list[dict[str, Any]]:
    wanted_ids = {str(item) for item in record_ids if str(item).strip()}
    if not wanted_ids:
        return []
    return [record for record in records if str(record.get("id")) in wanted_ids]

def _require_history_ids_access(request: Request, history_ids: list[str]) -> None:
    _require_records_gallery_access(
        request,
        _records_matching_ids(_load_history(), history_ids),
    )

def _trash_record_gallery_id(record: dict[str, Any]) -> str:
    gallery_id = str(
        record.get("original_gallery_id")
        or record.get("gallery_id")
        or DEFAULT_GALLERY_ID
    )
    return gallery_id if _get_gallery(gallery_id) is not None else DEFAULT_GALLERY_ID

def _require_trash_records_access(
    request: Request, records: list[dict[str, Any]]
) -> None:
    gallery_ids = {_trash_record_gallery_id(record) for record in records}
    for gallery_id in sorted(gallery_ids):
        _require_gallery_access(request, gallery_id)

def _filter_accessible_trash_records(
    request: Request, records: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    return [
        record
        for record in records
        if _can_access_gallery(request, _trash_record_gallery_id(record))
    ]

def _require_trash_ids_access(request: Request, trash_ids: list[str]) -> None:
    _require_trash_records_access(
        request,
        _records_matching_ids(_load_trash(), trash_ids),
    )

def _trash_path() -> Path:
    return settings.output_dir / "trash.json"

def _trash_retention_seconds() -> int:
    days = max(1, _safe_int(settings.trash_retention_days, 3))
    return days * 24 * 60 * 60

def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _trash_record_expires_at(record: dict[str, Any]) -> int:
    deleted_at = _safe_int(record.get("deleted_at"))
    if not deleted_at:
        return 0
    return deleted_at + _trash_retention_seconds()

def _normalize_trash_records(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (kept, expired). Each record is normalized in place."""
    now = int(time.time())
    kept: list[dict[str, Any]] = []
    expired: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for raw in records:
        if not isinstance(raw, dict):
            continue
        record = _normalize_history_record(raw)
        if not record.get("deleted_at"):
            record["deleted_at"] = now
        original = str(raw.get("original_gallery_id") or record.get("gallery_id") or DEFAULT_GALLERY_ID)
        record["original_gallery_id"] = original
        record_id = str(record.get("id") or "")
        if not record_id or record_id in seen_ids:
            continue
        seen_ids.add(record_id)
        if _trash_record_expires_at(record) and _trash_record_expires_at(record) <= now:
            expired.append(record)
        else:
            kept.append(record)
    kept.sort(key=lambda item: _safe_int(item.get("deleted_at")), reverse=True)
    return kept[:TRASH_LIMIT], expired

def _load_trash() -> list[dict[str, Any]]:
    path = _trash_path()
    with _json_file_lock(path, exclusive=True):
        raw_records = _read_json_list_unlocked(path)
        kept, expired = _normalize_trash_records(raw_records)
        if expired:
            history_file_references = _history_file_references(_load_history())
            kept_file_references = _history_file_references(kept)
            job_file_references = _job_file_references()
            preserve = history_file_references | kept_file_references | job_file_references
            for record in expired:
                _delete_history_file(
                    record,
                    history_file_references=preserve,
                )
            _log_event("trash_expired_purged", count=len(expired))
        if (kept != raw_records) or expired:
            _write_json_list_unlocked(path, kept)
        return kept

def _save_trash(records: list[dict[str, Any]]) -> None:
    path = _trash_path()
    with _json_file_lock(path, exclusive=True):
        kept, _ = _normalize_trash_records(records)
        _write_json_list_unlocked(path, kept)

def _trash_public(record: dict[str, Any]) -> dict[str, Any]:
    payload = _history_public(record)
    payload["deleted_at"] = _safe_int(record.get("deleted_at"))
    payload["expires_at"] = _trash_record_expires_at(record)
    payload["original_gallery_id"] = str(
        record.get("original_gallery_id") or record.get("gallery_id") or DEFAULT_GALLERY_ID
    )
    return payload

def _load_jobs() -> list[dict[str, Any]]:
    path = _jobs_path()
    with _json_file_lock(path, exclusive=False):
        return _read_json_list_unlocked(path)

def _save_jobs(jobs: list[dict[str, Any]]) -> None:
    jobs.sort(key=lambda item: _safe_int(item.get("created_at")), reverse=True)
    path = _jobs_path()
    with _json_file_lock(path, exclusive=True):
        _write_json_list_unlocked(path, jobs[:JOB_LIMIT])

def _mark_interrupted_jobs_on_startup() -> None:
    now = int(time.time())
    path = _jobs_path()
    interrupted: list[str] = []
    with _json_file_lock(path, exclusive=True):
        jobs = _read_json_list_unlocked(path)
        for job in jobs:
            if job.get("status") not in ACTIVE_JOB_STATUSES:
                continue
            job["status"] = "failed"
            job["status_code"] = 500
            job["finished_at"] = now
            job["updated_at"] = now
            job["error"] = {
                "error": {
                    "type": "JobInterrupted",
                    "message": "服务重启后该 Job 的本地执行任务已丢失，请重新提交生成。",
                }
            }
            interrupted.append(str(job.get("id") or ""))
        if interrupted:
            jobs.sort(key=lambda item: _safe_int(item.get("created_at")), reverse=True)
            _write_json_list_unlocked(path, jobs[:JOB_LIMIT])
    if interrupted:
        _log_event("jobs_marked_interrupted_on_startup", job_ids=interrupted)

def _get_job(job_id: str) -> dict[str, Any] | None:
    for job in _load_jobs():
        if job.get("id") == job_id:
            return job
    return None

def _active_job_count_unlocked(jobs: list[dict[str, Any]]) -> int:
    return sum(1 for job in jobs if job.get("status") in ACTIVE_JOB_STATUSES)

def _create_job_with_limit(job: dict[str, Any]) -> None:
    path = _jobs_path()
    with _json_file_lock(path, exclusive=True):
        jobs = _read_json_list_unlocked(path)
        active_count = _active_job_count_unlocked(jobs)
        if active_count >= MAX_ACTIVE_JOBS:
            _log_event(
                "job_rejected_active_limit",
                job_id=job.get("id"),
                active_count=active_count,
                max_active_jobs=MAX_ACTIVE_JOBS,
            )
            raise HTTPException(
                status_code=429,
                detail={
                    "error": {
                        "type": "TooManyActiveJobs",
                        "message": f"最多只能同时运行 {MAX_ACTIVE_JOBS} 个 Job，请等前面的任务完成或取消后再提交。",
                    },
                    "active_jobs": active_count,
                    "max_active_jobs": MAX_ACTIVE_JOBS,
                },
            )
        jobs.insert(0, job)
        jobs.sort(key=lambda item: _safe_int(item.get("created_at")), reverse=True)
        _write_json_list_unlocked(path, jobs[:JOB_LIMIT])

def _update_job(job_id: str, **fields: Any) -> dict[str, Any]:
    path = _jobs_path()
    with _json_file_lock(path, exclusive=True):
        jobs = _read_json_list_unlocked(path)
        for index, job in enumerate(jobs):
            if job.get("id") == job_id:
                updated_job = job.copy()
                updated_job.update(fields)
                updated_job["updated_at"] = int(time.time())
                jobs[index] = updated_job
                jobs.sort(key=lambda item: _safe_int(item.get("created_at")), reverse=True)
                _write_json_list_unlocked(path, jobs[:JOB_LIMIT])
                return updated_job
    raise HTTPException(status_code=404, detail="Job not found")

def _legacy_history_id(record: dict[str, Any]) -> str:
    seed = "|".join(
        [
            str(record.get("file") or ""),
            str(record.get("url") or ""),
            str(record.get("created_at") or ""),
        ]
    )
    if not seed.strip("|"):
        seed = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    return uuid.uuid5(uuid.NAMESPACE_URL, seed).hex

def _normalize_history_record(record: dict[str, Any]) -> dict[str, Any]:
    normalized = record.copy()
    if not normalized.get("id"):
        normalized["id"] = _legacy_history_id(normalized)
    normalized.setdefault("prompt", "")
    normalized.setdefault("size", "")
    normalized.setdefault("quality", "")
    normalized.setdefault("requested_quality", "")
    normalized.setdefault("actual_quality", "")
    normalized.setdefault("output_format", "")
    normalized.setdefault("operation", "generate")
    normalized.setdefault("status", "succeeded")
    normalized.setdefault("error", None)
    normalized.setdefault("status_code", None)
    normalized.setdefault("created_at", 0)
    raw_gallery_id = str(normalized.get("gallery_id") or "").strip()
    normalized["gallery_id"] = raw_gallery_id or DEFAULT_GALLERY_ID
    raw_position = normalized.get("position")
    if raw_position is None or raw_position == "":
        normalized["position"] = float(_safe_int(normalized.get("created_at")))
    else:
        try:
            normalized["position"] = float(raw_position)
        except (TypeError, ValueError):
            normalized["position"] = float(_safe_int(normalized.get("created_at")))
    metadata = _history_file_metadata(str(normalized.get("file") or ""))
    if normalized.get("file_size_bytes") is None:
        normalized["file_size_bytes"] = metadata["file_size_bytes"]
    if normalized.get("image_width") is None:
        normalized["image_width"] = metadata["image_width"]
    if normalized.get("image_height") is None:
        normalized["image_height"] = metadata["image_height"]
    normalized.setdefault(
        "image_dimensions",
        metadata["image_dimensions"] or (
            f"{normalized.get('image_width')}x{normalized.get('image_height')}"
            if normalized.get("image_width") and normalized.get("image_height")
            else ""
        ),
    )
    _ensure_history_thumbnail(normalized, _log_event)
    return normalized

def _sort_history_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [_normalize_history_record(record) for record in records]
    normalized.sort(
        key=lambda item: (
            _safe_float(item.get("position"), float(_safe_int(item.get("created_at")))),
            _safe_int(item.get("created_at")),
        ),
        reverse=True,
    )
    return normalized

def _apply_history_limit(
    records: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep at most HISTORY_LIMIT records per gallery, preserve original order.

    Records must already be sorted in newest-first order.
    """
    gallery_counts: dict[str, int] = {}
    kept: list[dict[str, Any]] = []
    overflow: list[dict[str, Any]] = []
    for record in records:
        gallery_id = str(record.get("gallery_id") or DEFAULT_GALLERY_ID)
        count = gallery_counts.get(gallery_id, 0)
        if count < HISTORY_LIMIT:
            kept.append(record)
            gallery_counts[gallery_id] = count + 1
        else:
            overflow.append(record)
    return kept, overflow

def _normalize_history_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sorted_records = _sort_history_records(records)
    kept, _ = _apply_history_limit(sorted_records)
    return kept

def _load_history() -> list[dict[str, Any]]:
    path = _history_path()
    with _json_file_lock(path, exclusive=True):
        raw_records = _read_json_list_unlocked(path)
        normalized = _normalize_history_records(raw_records)
        if normalized != raw_records:
            _write_json_list_unlocked(path, normalized)
        return normalized

def _history_public(record: dict[str, Any]) -> dict[str, Any]:
    actual_quality = record.get("actual_quality") or record.get("quality", "")
    requested_quality = record.get("requested_quality") or actual_quality
    return {
        "id": record.get("id", ""),
        "prompt": record.get("prompt", ""),
        "size": record.get("size", ""),
        "quality": actual_quality,
        "requested_quality": requested_quality,
        "actual_quality": actual_quality,
        "output_format": record.get("output_format", ""),
        "model": record.get("model", ""),
        "provider_id": record.get("provider_id", ""),
        "provider_name": record.get("provider_name", ""),
        "gallery_id": record.get("gallery_id", DEFAULT_GALLERY_ID),
        "position": _safe_float(record.get("position"), float(_safe_int(record.get("created_at")))),
        "operation": record.get("operation", "generate"),
        "source_file": record.get("source_file"),
        "source_history_id": record.get("source_history_id"),
        "status": record.get("status", "succeeded"),
        "error": record.get("error"),
        "status_code": record.get("status_code"),
        "file_size_bytes": record.get("file_size_bytes"),
        "image_width": record.get("image_width"),
        "image_height": record.get("image_height"),
        "image_dimensions": record.get("image_dimensions", ""),
        "thumbnail_file": record.get("thumbnail_file"),
        "thumbnail_file_size_bytes": record.get("thumbnail_file_size_bytes"),
        "thumbnail_width": record.get("thumbnail_width"),
        "thumbnail_height": record.get("thumbnail_height"),
        "thumbnail_dimensions": record.get("thumbnail_dimensions", ""),
        "created_at": record.get("created_at", 0),
    }

def _save_history(records: list[dict[str, Any]]) -> None:
    path = _history_path()
    with _json_file_lock(path, exclusive=True):
        _write_json_list_unlocked(path, _normalize_history_records(records))

def _append_history(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    new_records = []
    for record in records:
        new_record = record.copy()
        new_record.setdefault("id", uuid.uuid4().hex)
        new_records.append(new_record)

    path = _history_path()
    with _json_file_lock(path, exclusive=True):
        existing_sorted = _sort_history_records(_read_json_list_unlocked(path))
        gallery_max: dict[str, float] = {}
        for record in existing_sorted:
            gid = str(record.get("gallery_id") or DEFAULT_GALLERY_ID)
            pos = _safe_float(record.get("position"), float(_safe_int(record.get("created_at"))))
            if gid not in gallery_max or pos > gallery_max[gid]:
                gallery_max[gid] = pos
        for new_record in new_records:
            if "position" in new_record and new_record["position"] is not None:
                continue
            gid = str(new_record.get("gallery_id") or DEFAULT_GALLERY_ID)
            base = gallery_max.get(gid, float(_safe_int(new_record.get("created_at"))))
            base += 1.0
            new_record["position"] = base
            gallery_max[gid] = base
        history = new_records + existing_sorted
        seen: set[str] = set()
        deduped: list[dict[str, Any]] = []
        for record in history:
            record = _normalize_history_record(record)
            dedupe_key = str(record.get("file") or record.get("url") or record.get("id") or "")
            if not dedupe_key or dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            deduped.append(record)
        deduped = _sort_history_records(deduped)
        kept, overflow = _apply_history_limit(deduped)
        job_file_references = _job_file_references() if overflow else set()
        history_file_references = _history_file_references(kept) if overflow else set()
        for record in overflow:
            _delete_history_file(
                record,
                preserve_job_references=True,
                job_file_references=job_file_references,
                history_file_references=history_file_references,
            )
        _write_json_list_unlocked(path, kept)
        new_ids = {record["id"] for record in new_records}
        return [record for record in kept if record.get("id") in new_ids]

def _collect_file_urls(value: Any) -> set[str]:
    if isinstance(value, dict):
        return set().union(*(_collect_file_urls(item) for item in value.values()))
    if isinstance(value, list):
        return set().union(*(_collect_file_urls(item) for item in value))
    if isinstance(value, str) and value.startswith("/files/"):
        return {value}
    return set()

def _job_file_references() -> set[str]:
    return set().union(*(_collect_file_urls(job.get("result")) for job in _load_jobs()))

def _record_file_urls(record: dict[str, Any]) -> set[str]:
    return {
        str(record.get(key) or "")
        for key in ("file", "source_file")
        if str(record.get(key) or "").startswith("/files/")
    }

def _record_access_file_urls(record: dict[str, Any]) -> set[str]:
    return {
        str(record.get(key) or "")
        for key in ("file", "source_file", "thumbnail_file")
        if str(record.get(key) or "").startswith("/files/")
    }

def _history_file_references(records: list[dict[str, Any]]) -> set[str]:
    return set().union(*(_record_file_urls(record) for record in records))

def _gallery_ids_for_file_url(file_url: str) -> set[str]:
    gallery_ids: set[str] = set()
    for record in _load_history():
        if file_url in _record_access_file_urls(record):
            gallery_ids.add(str(record.get("gallery_id") or DEFAULT_GALLERY_ID))
    for record in _load_trash():
        if file_url in _record_access_file_urls(record):
            gallery_ids.add(
                str(
                    record.get("original_gallery_id")
                    or record.get("gallery_id")
                    or DEFAULT_GALLERY_ID
                )
            )
    for job in _load_jobs():
        if file_url in _collect_file_urls(job):
            payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
            gallery_ids.add(str(payload.get("gallery_id") or DEFAULT_GALLERY_ID))
    return gallery_ids

def _require_file_access(request: Request, file_url: str) -> None:
    gallery_ids = _gallery_ids_for_file_url(file_url)
    if not gallery_ids:
        return
    locked_gallery_id: str | None = None
    for gallery_id in sorted(gallery_ids):
        gallery = _get_gallery(gallery_id)
        if gallery is None or not _gallery_has_password(gallery):
            return
        if _is_gallery_unlocked(request, gallery):
            return
        locked_gallery_id = gallery_id
    if locked_gallery_id:
        _require_gallery_access(request, locked_gallery_id)

def _delete_history_file(
    record: dict[str, Any],
    preserve_job_references: bool = False,
    job_file_references: set[str] | None = None,
    history_file_references: set[str] | None = None,
) -> None:
    seen: set[str] = set()
    thumbnail_url = str(record.get("thumbnail_file") or "")
    if thumbnail_url.startswith("/files/"):
        _delete_file_url(thumbnail_url)

    for key in ("file", "source_file"):
        file_url = str(record.get(key) or "")
        if not file_url.startswith("/files/") or file_url in seen:
            continue
        seen.add(file_url)
        if history_file_references is not None and file_url in history_file_references:
            _log_event(
                "history_file_preserved",
                history_id=record.get("id"),
                file=file_url,
                reason="referenced_by_another_history_record",
            )
            continue
        if preserve_job_references and file_url in (
            job_file_references if job_file_references is not None else _job_file_references()
        ):
            _log_event(
                "history_overflow_file_preserved",
                history_id=record.get("id"),
                file=file_url,
                reason="referenced_by_job_result",
            )
            continue
        file_path = settings.output_dir / Path(file_url.removeprefix("/files/")).name
        try:
            file_path.unlink(missing_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to delete image file: {exc}") from exc

def _delete_history_item(history_id: str) -> None:
    path = _history_path()
    with _json_file_lock(path, exclusive=True):
        records = _normalize_history_records(_read_json_list_unlocked(path))
        for index, record in enumerate(records):
            if str(record.get("id")) == history_id:
                removed = records.pop(index)
                _delete_history_file(
                    removed,
                    history_file_references=_history_file_references(records),
                )
                _write_json_list_unlocked(path, records)
                return
    raise HTTPException(status_code=404, detail="History item not found")

def _delete_history_items(history_ids: list[str]) -> int:
    wanted_ids = {str(item) for item in history_ids}
    if not wanted_ids:
        return 0

    path = _history_path()
    with _json_file_lock(path, exclusive=True):
        records = _normalize_history_records(_read_json_list_unlocked(path))
        kept: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []
        for record in records:
            if str(record.get("id")) in wanted_ids:
                removed.append(record)
            else:
                kept.append(record)
        history_file_references = _history_file_references(kept)
        for record in removed:
            _delete_history_file(
                record,
                history_file_references=history_file_references,
            )
        _write_json_list_unlocked(path, kept)
        return len(removed)

def _move_history_items(history_ids: list[str], target_gallery_id: str) -> int:
    target_id = _resolve_gallery_id(target_gallery_id)
    wanted_ids = {str(item) for item in history_ids if item}
    if not wanted_ids:
        return 0

    path = _history_path()
    moved = 0
    with _json_file_lock(path, exclusive=True):
        sorted_records = _sort_history_records(_read_json_list_unlocked(path))
        for record in sorted_records:
            if str(record.get("id")) not in wanted_ids:
                continue
            current_gallery = str(record.get("gallery_id") or DEFAULT_GALLERY_ID)
            if current_gallery == target_id:
                continue
            record["gallery_id"] = target_id
            moved += 1
        kept, overflow = _apply_history_limit(sorted_records)
        if overflow:
            job_file_references = _job_file_references()
            history_file_references = _history_file_references(kept)
            for record in overflow:
                _delete_history_file(
                    record,
                    preserve_job_references=True,
                    job_file_references=job_file_references,
                    history_file_references=history_file_references,
                )
        _write_json_list_unlocked(path, kept)
    return moved

def _trash_history_items(history_ids: list[str]) -> int:
    wanted_ids = {str(item) for item in history_ids if str(item).strip()}
    if not wanted_ids:
        return 0
    history_path = _history_path()
    trash_path = _trash_path()
    moved = 0
    now = int(time.time())
    with _json_file_lock(history_path, exclusive=True):
        history_records = _read_json_list_unlocked(history_path)
        kept: list[dict[str, Any]] = []
        moved_records: list[dict[str, Any]] = []
        for record in history_records:
            normalized = _normalize_history_record(record)
            if str(normalized.get("id")) in wanted_ids:
                normalized["original_gallery_id"] = str(
                    normalized.get("gallery_id") or DEFAULT_GALLERY_ID
                )
                normalized["deleted_at"] = now
                moved_records.append(normalized)
            else:
                kept.append(normalized)
        if not moved_records:
            return 0
        with _json_file_lock(trash_path, exclusive=True):
            trash_records = _read_json_list_unlocked(trash_path)
            trash_records = moved_records + [
                record
                for record in trash_records
                if isinstance(record, dict)
                and str(record.get("id")) not in {str(r.get("id")) for r in moved_records}
            ]
            normalized_trash, expired = _normalize_trash_records(trash_records)
            if expired:
                preserve = _history_file_references(kept) | _job_file_references()
                for record in expired:
                    _delete_history_file(
                        record,
                        history_file_references=preserve,
                    )
            _write_json_list_unlocked(trash_path, normalized_trash)
        _write_json_list_unlocked(history_path, kept)
        moved = len(moved_records)
    return moved

def _restore_trash_items(trash_ids: list[str]) -> int:
    wanted_ids = {str(item) for item in trash_ids if str(item).strip()}
    if not wanted_ids:
        return 0
    galleries = _load_galleries()
    valid_gallery_ids = {gallery["id"] for gallery in galleries}

    history_path = _history_path()
    trash_path = _trash_path()
    restored = 0
    with _json_file_lock(trash_path, exclusive=True):
        trash_records = _read_json_list_unlocked(trash_path)
        keep_trash: list[dict[str, Any]] = []
        restoring: list[dict[str, Any]] = []
        for record in trash_records:
            if not isinstance(record, dict):
                continue
            if str(record.get("id")) in wanted_ids:
                restoring.append(record)
            else:
                keep_trash.append(record)
        if not restoring:
            return 0
        with _json_file_lock(history_path, exclusive=True):
            history_records = _read_json_list_unlocked(history_path)
            existing_ids = {
                str(_normalize_history_record(record).get("id"))
                for record in history_records
            }
            now = int(time.time())
            new_records: list[dict[str, Any]] = []
            for raw in restoring:
                record = _normalize_history_record(raw)
                target = str(raw.get("original_gallery_id") or record.get("gallery_id") or DEFAULT_GALLERY_ID)
                if target not in valid_gallery_ids:
                    target = DEFAULT_GALLERY_ID
                record["gallery_id"] = target
                record.pop("deleted_at", None)
                record.pop("original_gallery_id", None)
                record["position"] = None  # _append_history will re-assign max+1
                if str(record.get("id")) in existing_ids:
                    record["id"] = uuid.uuid4().hex
                record["created_at"] = _safe_int(record.get("created_at")) or now
                new_records.append(record)
            kept_normalized, _ = _normalize_trash_records(keep_trash)
            _write_json_list_unlocked(trash_path, kept_normalized)
        # outside trash lock; _append_history takes its own history lock
        appended = _append_history(new_records)
        restored = len(appended)
    return restored

def _permanently_delete_trash_items(trash_ids: list[str]) -> int:
    wanted_ids = {str(item) for item in trash_ids if str(item).strip()}
    if not wanted_ids:
        return 0
    trash_path = _trash_path()
    removed_count = 0
    with _json_file_lock(trash_path, exclusive=True):
        trash_records = _read_json_list_unlocked(trash_path)
        kept: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []
        for record in trash_records:
            if not isinstance(record, dict):
                continue
            normalized = _normalize_history_record(record)
            normalized["deleted_at"] = _safe_int(record.get("deleted_at"))
            if str(normalized.get("id")) in wanted_ids:
                removed.append(normalized)
            else:
                kept.append(normalized)
        if removed:
            history_records = _load_history()
            preserve = (
                _history_file_references(history_records)
                | _history_file_references(kept)
                | _job_file_references()
            )
            for record in removed:
                _delete_history_file(
                    record,
                    history_file_references=preserve,
                )
            kept_normalized, _ = _normalize_trash_records(kept)
            _write_json_list_unlocked(trash_path, kept_normalized)
            removed_count = len(removed)
    return removed_count

def _empty_trash() -> int:
    trash_path = _trash_path()
    with _json_file_lock(trash_path, exclusive=True):
        trash_records = _read_json_list_unlocked(trash_path)
        ids = [
            str(_normalize_history_record(record).get("id"))
            for record in trash_records
            if isinstance(record, dict)
        ]
    if not ids:
        return 0
    return _permanently_delete_trash_items(ids)

def _reorder_history_items(gallery_id: str, ordered_ids: list[str]) -> int:
    target_id = _resolve_gallery_id(gallery_id)
    wanted_ids = [str(item) for item in ordered_ids if str(item).strip()]
    if not wanted_ids:
        return 0
    if len(set(wanted_ids)) != len(wanted_ids):
        raise HTTPException(status_code=400, detail="ordered_ids must be unique")

    path = _history_path()
    updated = 0
    with _json_file_lock(path, exclusive=True):
        records = _read_json_list_unlocked(path)
        # Compute the highest existing position in the gallery so the reordered
        # block sits above any unrelated rows that were not part of the request.
        max_pos = 0.0
        existing_ids: set[str] = set()
        for raw in records:
            normalized = _normalize_history_record(raw)
            existing_ids.add(str(normalized.get("id")))
            if str(normalized.get("gallery_id") or DEFAULT_GALLERY_ID) != target_id:
                continue
            pos = _safe_float(normalized.get("position"), float(_safe_int(normalized.get("created_at"))))
            if pos > max_pos:
                max_pos = pos
        unknown = [item for item in wanted_ids if item not in existing_ids]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"unknown history ids: {unknown[:5]}",
            )
        base = max_pos + 1.0
        N = len(wanted_ids)
        id_to_pos = {hid: base + float(N - 1 - index) for index, hid in enumerate(wanted_ids)}
        rewritten: list[dict[str, Any]] = []
        for raw in records:
            normalized = _normalize_history_record(raw)
            hid = str(normalized.get("id"))
            current_gallery = str(normalized.get("gallery_id") or DEFAULT_GALLERY_ID)
            if hid in id_to_pos and current_gallery == target_id:
                normalized["position"] = id_to_pos[hid]
                updated += 1
            rewritten.append(normalized)
        _write_json_list_unlocked(path, rewritten)
    return updated

def _history_image_path(record: dict[str, Any]) -> Path | None:
    file_url = str(record.get("file") or "")
    if not file_url.startswith("/files/"):
        return None
    file_path = settings.output_dir / Path(file_url.removeprefix("/files/")).name
    if not file_path.is_file():
        return None
    return file_path

def _build_history_zip(history_ids: list[str]) -> Path:
    records = _load_history()
    wanted_ids = {str(item) for item in history_ids}
    selected_records = [record for record in records if str(record.get("id")) in wanted_ids]
    if not selected_records:
        raise HTTPException(status_code=400, detail="No valid history ids selected")

    zip_file = tempfile.NamedTemporaryFile(
        suffix=".zip",
        prefix="image-cli-history-",
        delete=False,
    )
    zip_file.close()
    zip_path = Path(zip_file.name)
    added = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for sequence, record in enumerate(selected_records, start=1):
            image_path = _history_image_path(record)
            if image_path is None:
                continue
            archive.write(image_path, f"{sequence:03d}-{image_path.name}")
            prompt = str(record.get("prompt") or "")
            if prompt:
                archive.writestr(f"{sequence:03d}-{image_path.stem}-prompt.txt", prompt)
            added += 1

    if added == 0:
        zip_path.unlink(missing_ok=True)
        raise HTTPException(status_code=404, detail="Selected history items have no local image files")

    return zip_path

def _delete_file_url(file_url: str) -> None:
    if not file_url.startswith("/files/"):
        return
    file_path = settings.output_dir / Path(file_url.removeprefix("/files/")).name
    try:
        file_path.unlink(missing_ok=True)
    except OSError as exc:
        _log_event(
            "file_delete_failed",
            file=file_url,
            error_type=exc.__class__.__name__,
            error_message=str(exc) or repr(exc),
        )
