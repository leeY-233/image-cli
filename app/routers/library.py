import time
import uuid
from pathlib import Path
from typing import Any

from starlette.background import BackgroundTask
from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import FileResponse

from ..auth import require_auth as _require_auth
from ..config import settings
from ..constants import (
    DEFAULT_GALLERY_ID,
    MAX_GALLERIES,
    MAX_GALLERY_NAME_LENGTH,
    TRASH_MAX_RETENTION_DAYS,
    TRASH_MIN_RETENTION_DAYS,
)
from ..env_file import write_env_updates as _write_env_updates
from ..library_service import (
    _build_history_zip,
    _delete_history_file,
    _delete_history_item,
    _delete_history_items,
    _filter_accessible_records,
    _filter_accessible_trash_records,
    _galleries_path,
    _gallery_has_password,
    _gallery_locked_exception,
    _gallery_password_hash,
    _gallery_public,
    _get_gallery,
    _history_file_references,
    _history_path,
    _history_public,
    _is_gallery_unlocked,
    _load_galleries,
    _load_history,
    _load_trash,
    _move_history_items,
    _new_gallery_password_version,
    _normalize_galleries,
    _normalize_history_records,
    _permanently_delete_trash_items,
    _reorder_history_items,
    _require_file_access,
    _require_gallery_access,
    _require_gallery_unlock_secret,
    _require_history_ids_access,
    _require_records_gallery_access,
    _require_trash_ids_access,
    _resolve_gallery_id,
    _restore_trash_items,
    _safe_float,
    _set_gallery_unlocked,
    _trash_history_items,
    _trash_public,
    _trash_retention_seconds,
    _verify_gallery_password,
)
from ..providers import (
    load_provider_configs as _load_provider_configs,
    provider_public as _provider_public,
    safe_provider_id as _safe_provider_id,
)
from ..storage import (
    json_file_lock as _json_file_lock,
    read_json_list_unlocked as _read_json_list_unlocked,
    write_json_list_unlocked as _write_json_list_unlocked,
)
from ..telemetry import log_event as _log_event


router = APIRouter()


@router.get("/files/{filename}")
def serve_file(filename: str, request: Request) -> FileResponse:
    _require_auth(request)
    safe_name = Path(filename).name
    file_path = settings.output_dir / safe_name
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    _require_file_access(request, f"/files/{safe_name}")
    return FileResponse(file_path)

@router.get("/v1/history")
def history(
    request: Request, gallery_id: str | None = None
) -> dict[str, list[dict[str, Any]]]:
    _require_auth(request)
    records = _load_history()
    if gallery_id:
        gallery_id = _resolve_gallery_id(gallery_id)
        _require_gallery_access(request, gallery_id)
        records = [
            record
            for record in records
            if str(record.get("gallery_id") or DEFAULT_GALLERY_ID) == gallery_id
        ]
    else:
        records = _filter_accessible_records(request, records)
    return {"images": [_history_public(record) for record in records]}

@router.get("/v1/providers")
def providers(request: Request) -> dict[str, Any]:
    _require_auth(request)
    provider_list = [_provider_public(provider) for provider in _load_provider_configs()]
    return {
        "providers": provider_list,
        "default_provider_id": provider_list[0]["id"] if provider_list else "",
    }

@router.get("/v1/galleries")
def list_galleries(request: Request) -> dict[str, Any]:
    _require_auth(request)
    gallery_list = [_gallery_public(gallery, request) for gallery in _load_galleries()]
    return {
        "galleries": gallery_list,
        "default_gallery_id": DEFAULT_GALLERY_ID,
    }

@router.post("/v1/galleries")
async def create_gallery(request: Request, response: Response) -> dict[str, Any]:
    _require_auth(request)
    body = await request.json()
    name = str(body.get("name") or "").strip()
    password = str(body.get("password") or "")
    if not name:
        raise HTTPException(status_code=400, detail="画廊名称不能为空")
    if len(name) > MAX_GALLERY_NAME_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"画廊名称最多 {MAX_GALLERY_NAME_LENGTH} 个字符",
        )
    if password:
        _require_gallery_unlock_secret()

    path = _galleries_path()
    with _json_file_lock(path, exclusive=True):
        galleries = _normalize_galleries(_read_json_list_unlocked(path))
        if len(galleries) >= MAX_GALLERIES:
            raise HTTPException(
                status_code=400,
                detail=f"最多只能创建 {MAX_GALLERIES} 个画廊",
            )
        if any(gallery["name"] == name for gallery in galleries):
            raise HTTPException(status_code=400, detail=f"画廊 “{name}” 已存在")

        existing_ids = {gallery["id"] for gallery in galleries}
        gallery_id = uuid.uuid4().hex[:12]
        while gallery_id in existing_ids or gallery_id == DEFAULT_GALLERY_ID:
            gallery_id = uuid.uuid4().hex[:12]

        new_gallery = {
            "id": gallery_id,
            "name": name,
            "created_at": int(time.time()),
            "position": max(
                [_safe_float(gallery.get("position")) for gallery in galleries] or [0.0]
            )
            + 1.0,
        }
        if password:
            new_gallery["password_hash"] = _gallery_password_hash(password)
            new_gallery["password_updated_at"] = _new_gallery_password_version()
        galleries.append(new_gallery)
        _write_json_list_unlocked(path, _normalize_galleries(galleries))
    if password:
        _set_gallery_unlocked(response, request, new_gallery)
    _log_event(
        "gallery_created",
        gallery_id=gallery_id,
        name=name,
        password_protected=bool(password),
    )
    return _gallery_public(new_gallery, request, force_unlocked=bool(password))

@router.post("/v1/galleries/reorder")
async def reorder_galleries(request: Request) -> dict[str, Any]:
    _require_auth(request)
    body = await request.json()
    raw_ids = (
        body.get("ordered_ids")
        or body.get("gallery_ids")
        or body.get("ids")
    )
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="ordered_ids must be a list")

    ordered_ids: list[str] = []
    seen_ids: set[str] = set()
    for item in raw_ids:
        gallery_id = _safe_provider_id(item, "")
        if not gallery_id:
            continue
        if gallery_id in seen_ids:
            raise HTTPException(status_code=400, detail="ordered_ids must be unique")
        ordered_ids.append(gallery_id)
        seen_ids.add(gallery_id)
    if not ordered_ids:
        raise HTTPException(status_code=400, detail="ordered_ids cannot be empty")

    path = _galleries_path()
    with _json_file_lock(path, exclusive=True):
        galleries = _normalize_galleries(_read_json_list_unlocked(path))
        existing_ids = {gallery["id"] for gallery in galleries}
        unknown_ids = [gallery_id for gallery_id in ordered_ids if gallery_id not in existing_ids]
        if unknown_ids:
            raise HTTPException(
                status_code=400,
                detail=f"unknown gallery ids: {unknown_ids[:5]}",
            )
        next_order = ordered_ids + [
            gallery["id"] for gallery in galleries if gallery["id"] not in seen_ids
        ]
        position_by_id = {
            gallery_id: float(index)
            for index, gallery_id in enumerate(next_order, start=1)
        }
        for gallery in galleries:
            gallery["position"] = position_by_id[gallery["id"]]
        galleries = _normalize_galleries(galleries)
        _write_json_list_unlocked(path, galleries)

    _log_event("galleries_reordered", ordered_ids=next_order)
    return {
        "ok": True,
        "galleries": [_gallery_public(gallery, request) for gallery in galleries],
        "default_gallery_id": DEFAULT_GALLERY_ID,
    }

@router.post("/v1/galleries/{gallery_id}/unlock")
async def unlock_gallery(
    gallery_id: str, request: Request, response: Response
) -> dict[str, Any]:
    _require_auth(request)
    gallery = _get_gallery(gallery_id)
    if gallery is None:
        raise HTTPException(status_code=404, detail="画廊不存在")
    if not _gallery_has_password(gallery):
        return _gallery_public(gallery, request, force_unlocked=True)
    _require_gallery_unlock_secret()
    body = await request.json()
    password = str(body.get("password") or "")
    if not password:
        raise HTTPException(status_code=400, detail="请输入画廊密码")
    if not _verify_gallery_password(password, str(gallery.get("password_hash") or "")):
        raise HTTPException(status_code=401, detail="画廊密码不正确")
    _set_gallery_unlocked(response, request, gallery)
    _log_event("gallery_unlocked", gallery_id=gallery_id)
    return _gallery_public(gallery, request, force_unlocked=True)

@router.patch("/v1/galleries/{gallery_id}")
async def update_gallery(
    gallery_id: str, request: Request, response: Response
) -> dict[str, Any]:
    _require_auth(request)
    body = await request.json()
    name_supplied = "name" in body
    name = str(body.get("name") or "").strip() if name_supplied else ""
    password = str(body.get("password") or "")
    clear_password = bool(body.get("clear_password"))
    current_password = str(body.get("current_password") or "")
    if name_supplied and not name:
        raise HTTPException(status_code=400, detail="画廊名称不能为空")
    if name_supplied and len(name) > MAX_GALLERY_NAME_LENGTH:
        raise HTTPException(
            status_code=400,
            detail=f"画廊名称最多 {MAX_GALLERY_NAME_LENGTH} 个字符",
        )
    if password and clear_password:
        raise HTTPException(status_code=400, detail="password 和 clear_password 不能同时设置")
    if not name_supplied and not password and not clear_password:
        raise HTTPException(status_code=400, detail="没有可保存的画廊设置")
    if gallery_id == DEFAULT_GALLERY_ID and (password or clear_password):
        raise HTTPException(status_code=400, detail="默认画廊不能设置密码")
    if password:
        _require_gallery_unlock_secret()

    path = _galleries_path()
    password_changed = False
    with _json_file_lock(path, exclusive=True):
        galleries = _normalize_galleries(_read_json_list_unlocked(path))
        target = next((gallery for gallery in galleries if gallery["id"] == gallery_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="画廊不存在")
        if _gallery_has_password(target) and (name_supplied or password or clear_password):
            if not settings.session_secret:
                _require_gallery_unlock_secret()
            if not _is_gallery_unlocked(request, target):
                raise _gallery_locked_exception(target)
        if _gallery_has_password(target) and (password or clear_password):
            if not current_password:
                raise HTTPException(status_code=400, detail="请输入当前密码")
            if not _verify_gallery_password(
                current_password, str(target.get("password_hash") or "")
            ):
                raise HTTPException(status_code=401, detail="当前密码不正确")
        if name_supplied:
            if any(
                gallery["id"] != gallery_id and gallery["name"] == name
                for gallery in galleries
            ):
                raise HTTPException(status_code=400, detail=f"画廊 “{name}” 已存在")
            target["name"] = name
        if password:
            target["password_hash"] = _gallery_password_hash(password)
            target["password_updated_at"] = _new_gallery_password_version()
            password_changed = True
        elif clear_password:
            target.pop("password_hash", None)
            target.pop("password_updated_at", None)
            password_changed = True
        _write_json_list_unlocked(path, _normalize_galleries(galleries))
    if password:
        _set_gallery_unlocked(response, request, target)
    _log_event(
        "gallery_updated",
        gallery_id=gallery_id,
        name=target.get("name"),
        password_protected=_gallery_has_password(target),
        password_changed=password_changed,
    )
    return _gallery_public(target, request, force_unlocked=bool(password))

@router.delete("/v1/galleries/{gallery_id}")
def delete_gallery(gallery_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    if gallery_id == DEFAULT_GALLERY_ID:
        raise HTTPException(status_code=400, detail="默认画廊不能删除")
    _require_gallery_access(request, gallery_id)

    history_path = _history_path()
    deleted_count = 0
    with _json_file_lock(history_path, exclusive=True):
        records = _normalize_history_records(_read_json_list_unlocked(history_path))
        kept: list[dict[str, Any]] = []
        removed: list[dict[str, Any]] = []
        for record in records:
            record_gallery = str(record.get("gallery_id") or DEFAULT_GALLERY_ID)
            if record_gallery == gallery_id:
                removed.append(record)
            else:
                kept.append(record)
        if removed:
            history_file_references = _history_file_references(kept)
            for record in removed:
                _delete_history_file(
                    record,
                    history_file_references=history_file_references,
                )
            _write_json_list_unlocked(history_path, kept)
            deleted_count = len(removed)

    galleries_path = _galleries_path()
    with _json_file_lock(galleries_path, exclusive=True):
        galleries = _normalize_galleries(_read_json_list_unlocked(galleries_path))
        next_galleries = [gallery for gallery in galleries if gallery["id"] != gallery_id]
        if len(next_galleries) == len(galleries):
            raise HTTPException(status_code=404, detail="画廊不存在")
        _write_json_list_unlocked(
            galleries_path, _normalize_galleries(next_galleries)
        )
    _log_event(
        "gallery_deleted",
        gallery_id=gallery_id,
        deleted_history=deleted_count,
    )
    return {"ok": True, "deleted_history": deleted_count}

@router.get("/v1/history/{history_id}")
def history_detail(history_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    for record in _load_history():
        if str(record.get("id")) == history_id:
            _require_records_gallery_access(request, [record])
            return record
    raise HTTPException(status_code=404, detail="History item not found")

@router.delete("/v1/history/{history_id}")
def delete_history(history_id: str, request: Request) -> dict[str, bool]:
    _require_auth(request)
    _require_history_ids_access(request, [history_id])
    _delete_history_item(history_id)
    return {"ok": True}

@router.post("/v1/history/delete")
async def delete_history_batch(request: Request) -> dict[str, int | bool]:
    _require_auth(request)
    body = await request.json()
    raw_ids = body.get("ids", [])
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="ids must be a list")
    history_ids = [str(item) for item in raw_ids]
    _require_history_ids_access(request, history_ids)
    deleted = _delete_history_items(history_ids)
    return {"ok": True, "deleted": deleted}

@router.post("/v1/history/move")
async def move_history_batch(request: Request) -> dict[str, Any]:
    _require_auth(request)
    body = await request.json()
    raw_ids = body.get("ids", [])
    raw_gallery_id = body.get("gallery_id", "")
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="ids must be a list")
    if not isinstance(raw_gallery_id, str) or not raw_gallery_id.strip():
        raise HTTPException(status_code=400, detail="gallery_id is required")
    history_ids = [str(item) for item in raw_ids if str(item).strip()]
    if not history_ids:
        raise HTTPException(status_code=400, detail="ids must not be empty")
    _require_history_ids_access(request, history_ids)
    _require_gallery_access(request, raw_gallery_id.strip())
    moved = _move_history_items(history_ids, raw_gallery_id.strip())
    _log_event(
        "history_moved",
        gallery_id=raw_gallery_id.strip(),
        moved=moved,
        requested=len(history_ids),
    )
    return {"ok": True, "moved": moved, "gallery_id": raw_gallery_id.strip()}

@router.post("/v1/history/trash")
async def trash_history_batch(request: Request) -> dict[str, Any]:
    _require_auth(request)
    body = await request.json()
    raw_ids = body.get("ids", [])
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="ids must be a list")
    history_ids = [str(item) for item in raw_ids if str(item).strip()]
    if not history_ids:
        raise HTTPException(status_code=400, detail="ids must not be empty")
    _require_history_ids_access(request, history_ids)
    moved = _trash_history_items(history_ids)
    _log_event("history_trashed", count=moved, requested=len(history_ids))
    return {"ok": True, "trashed": moved}

@router.post("/v1/history/reorder")
async def reorder_history_batch(request: Request) -> dict[str, Any]:
    _require_auth(request)
    body = await request.json()
    raw_ids = body.get("ordered_ids", [])
    raw_gallery_id = body.get("gallery_id", "")
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="ordered_ids must be a list")
    if not isinstance(raw_gallery_id, str) or not raw_gallery_id.strip():
        raise HTTPException(status_code=400, detail="gallery_id is required")
    history_ids = [str(item) for item in raw_ids if str(item).strip()]
    if not history_ids:
        raise HTTPException(status_code=400, detail="ordered_ids must not be empty")
    _require_gallery_access(request, raw_gallery_id.strip())
    updated = _reorder_history_items(raw_gallery_id.strip(), history_ids)
    _log_event(
        "history_reordered",
        gallery_id=raw_gallery_id.strip(),
        updated=updated,
        requested=len(history_ids),
    )
    return {"ok": True, "updated": updated, "gallery_id": raw_gallery_id.strip()}

@router.get("/v1/trash")
def list_trash(request: Request) -> dict[str, Any]:
    _require_auth(request)
    records = _filter_accessible_trash_records(request, _load_trash())
    return {
        "items": [_trash_public(record) for record in records],
        "retention_days": settings.trash_retention_days,
        "retention_seconds": _trash_retention_seconds(),
    }

@router.post("/v1/trash/restore")
async def restore_trash_batch(request: Request) -> dict[str, Any]:
    _require_auth(request)
    body = await request.json()
    raw_ids = body.get("ids", [])
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="ids must be a list")
    trash_ids = [str(item) for item in raw_ids if str(item).strip()]
    if not trash_ids:
        raise HTTPException(status_code=400, detail="ids must not be empty")
    _require_trash_ids_access(request, trash_ids)
    restored = _restore_trash_items(trash_ids)
    _log_event("trash_restored", count=restored, requested=len(trash_ids))
    return {"ok": True, "restored": restored}

@router.post("/v1/trash/delete")
async def delete_trash_batch(request: Request) -> dict[str, Any]:
    _require_auth(request)
    body = await request.json()
    raw_ids = body.get("ids", [])
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="ids must be a list")
    trash_ids = [str(item) for item in raw_ids if str(item).strip()]
    if not trash_ids:
        raise HTTPException(status_code=400, detail="ids must not be empty")
    _require_trash_ids_access(request, trash_ids)
    deleted = _permanently_delete_trash_items(trash_ids)
    _log_event("trash_permanently_deleted", count=deleted, requested=len(trash_ids))
    return {"ok": True, "deleted": deleted}

@router.post("/v1/trash/empty")
def empty_trash(request: Request) -> dict[str, Any]:
    _require_auth(request)
    records = _filter_accessible_trash_records(request, _load_trash())
    deleted = _permanently_delete_trash_items(
        [str(record.get("id")) for record in records]
    )
    _log_event("trash_emptied", count=deleted)
    return {"ok": True, "deleted": deleted}

@router.get("/v1/trash/settings")
def get_trash_settings(request: Request) -> dict[str, Any]:
    _require_auth(request)
    return {
        "retention_days": settings.trash_retention_days,
        "min_days": TRASH_MIN_RETENTION_DAYS,
        "max_days": TRASH_MAX_RETENTION_DAYS,
    }

@router.post("/v1/trash/settings")
async def update_trash_settings(request: Request) -> dict[str, Any]:
    _require_auth(request)
    body = await request.json()
    raw_days = body.get("retention_days")
    if raw_days is None:
        raise HTTPException(status_code=400, detail="retention_days is required")
    try:
        days = int(raw_days)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="retention_days must be an integer")
    if days < TRASH_MIN_RETENTION_DAYS or days > TRASH_MAX_RETENTION_DAYS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"retention_days must be between {TRASH_MIN_RETENTION_DAYS} "
                f"and {TRASH_MAX_RETENTION_DAYS}"
            ),
        )
    # Update both the live setting and the .env file so it survives restarts.
    settings.trash_retention_days = days
    _write_env_updates({"TRASH_RETENTION_DAYS": str(days)})
    _log_event("trash_retention_updated", retention_days=days)
    # trigger immediate cleanup pass with the new retention
    _load_trash()
    return {"ok": True, "retention_days": days}

@router.post("/v1/history/download")
async def download_history_batch(request: Request) -> FileResponse:
    _require_auth(request)
    body = await request.json()
    raw_ids = body.get("ids", [])
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="ids must be a list")
    history_ids = [str(item) for item in raw_ids]
    _require_history_ids_access(request, history_ids)
    zip_path = _build_history_zip(history_ids)
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename="history-images.zip",
        background=BackgroundTask(zip_path.unlink, missing_ok=True),
    )
