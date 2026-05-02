import asyncio
import base64
import binascii
import fcntl
import hashlib
import hmac
import json
import logging
import os
import shutil
import tempfile
import time
import uuid
import zipfile
from contextlib import contextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Literal

import httpx
from dotenv import load_dotenv
from starlette.background import BackgroundTask
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from pydantic import BaseModel, Field


load_dotenv()


class Settings(BaseModel):
    api_key: str = Field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    base_url: str = Field(
        default_factory=lambda: os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1")
    )
    model: str = Field(default_factory=lambda: os.getenv("IMAGE_MODEL", "gpt-image-2"))
    image_size: str = Field(default_factory=lambda: os.getenv("IMAGE_SIZE", "1024x1024"))
    image_quality: str = Field(default_factory=lambda: os.getenv("IMAGE_QUALITY", "auto"))
    image_output_format: str = Field(
        default_factory=lambda: os.getenv("IMAGE_OUTPUT_FORMAT", "png")
    )
    response_format: str = Field(
        default_factory=lambda: os.getenv("IMAGE_RESPONSE_FORMAT", "b64_json")
    )
    output_dir: Path = Field(
        default_factory=lambda: Path(os.getenv("OUTPUT_DIR", "outputs"))
    )
    log_dir: Path = Field(default_factory=lambda: Path(os.getenv("LOG_DIR", "logs")))
    timeout_seconds: float = Field(
        default_factory=lambda: float(os.getenv("REQUEST_TIMEOUT_SECONDS", "180"))
    )
    provider_max_attempts: int = Field(
        default_factory=lambda: max(1, int(os.getenv("PROVIDER_MAX_ATTEMPTS", "2")))
    )
    app_password: str = Field(default_factory=lambda: os.getenv("APP_PASSWORD", ""))
    session_secret: str = Field(
        default_factory=lambda: os.getenv("APP_SESSION_SECRET", "")
    )
    systemd_unit: str = Field(default_factory=lambda: os.getenv("SYSTEMD_UNIT", "image-cli"))
    session_max_age_seconds: int = 60 * 60 * 24 * 7

    @property
    def images_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/images/generations"


settings = Settings()
settings.output_dir.mkdir(parents=True, exist_ok=True)
settings.log_dir.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Image CLI Web Service", version="0.1.0")
HISTORY_LIMIT = 30
JOB_LIMIT = 100
MAX_ACTIVE_JOBS = 5
ACTIVE_JOB_STATUSES = {"queued", "running"}
JOB_TASKS: dict[str, asyncio.Task] = {}
MIN_IMAGE_DIMENSION = 16
MAX_IMAGE_DIMENSION = 4096
MAX_IMAGE_PIXELS = 3840 * 2160

logger = logging.getLogger("image_cli")
logger.setLevel(logging.INFO)
logger.propagate = False
if not logger.handlers:
    file_handler = RotatingFileHandler(
        settings.log_dir / "app.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(file_handler)

if not (settings.app_password and settings.session_secret):
    logger.warning(
        json.dumps(
            {
                "ts": int(time.time()),
                "event": "auth_disabled_warning",
                "detail": (
                    "APP_PASSWORD and APP_SESSION_SECRET are both required to enable login. "
                    "The service is currently accessible without authentication."
                ),
            },
            ensure_ascii=False,
        )
    )


class GenerateImageRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=8000)
    model: str | None = None
    size: str | None = None
    quality: str | None = None
    output_format: Literal["png", "jpeg", "webp"] | None = None
    n: int = Field(default=1, ge=1, le=10)
    response_format: Literal["b64_json", "url"] | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class GeneratedImage(BaseModel):
    index: int
    url: str | None = None
    file: str | None = None
    revised_prompt: str | None = None


class GenerateImageResponse(BaseModel):
    model: str
    images: list[GeneratedImage]
    provider_response: dict[str, Any]


def _log_event(event: str, **fields: Any) -> None:
    payload = {
        "ts": int(time.time()),
        "event": event,
        **fields,
    }
    logger.info(json.dumps(_redact_large_payloads(payload), ensure_ascii=False, default=str))


def _elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _parse_size(size: str) -> tuple[int, int] | None:
    width_text, separator, height_text = size.lower().partition("x")
    if separator != "x":
        return None
    try:
        width = int(width_text)
        height = int(height_text)
    except ValueError:
        return None
    return width, height


def _validate_size_budget(size: str) -> None:
    if size == "auto":
        return
    parsed = _parse_size(size)
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


@contextmanager
def _json_file_lock(path: Path, exclusive: bool):
    lock_path = path.with_name(f"{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _read_json_list_unlocked(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _write_json_list_unlocked(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(records, ensure_ascii=False, indent=2)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as tmp_file:
        tmp_file.write(payload)
        tmp_path = Path(tmp_file.name)
    tmp_path.replace(path)


def _auth_enabled() -> bool:
    return bool(settings.app_password and settings.session_secret)


def _sign_session(payload: str) -> str:
    return hmac.new(
        settings.session_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _make_session_token() -> str:
    expires_at = int(time.time()) + settings.session_max_age_seconds
    nonce = uuid.uuid4().hex
    payload = f"{expires_at}:{nonce}"
    signature = _sign_session(payload)
    token = f"{payload}:{signature}".encode("utf-8")
    return base64.urlsafe_b64encode(token).decode("utf-8")


def _is_authenticated(request: Request) -> bool:
    if not _auth_enabled():
        return True

    raw_token = request.cookies.get("image_cli_session")
    if not raw_token:
        return False

    try:
        decoded = base64.urlsafe_b64decode(raw_token.encode("utf-8")).decode("utf-8")
        expires_text, nonce, signature = decoded.split(":", 2)
        payload = f"{expires_text}:{nonce}"
        expires_at = int(expires_text)
    except (ValueError, binascii.Error):
        return False

    if expires_at < int(time.time()):
        return False

    expected = _sign_session(payload)
    return hmac.compare_digest(signature, expected)


def _require_auth(request: Request) -> None:
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")


def _template_path(name: str) -> Path:
    return Path(__file__).parent / "templates" / name


def _read_template(name: str) -> str:
    return _template_path(name).read_text(encoding="utf-8")


def _sse_payload(line: str) -> str:
    return f"data: {json.dumps(line, ensure_ascii=False)}\n\n"


def _sse_event(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _debug_timestamp(ts: int | float | None = None) -> str:
    timestamp = time.localtime(ts if ts is not None else time.time())
    return time.strftime("%Y-%m-%d %H:%M:%S", timestamp)


def _starts_with_timestamp(line: str) -> bool:
    if len(line) >= 19 and line[4:5] == "-" and line[7:8] == "-" and line[10:11] in {" ", "T"}:
        return True
    return False


def _format_debug_line(line: str, source: str) -> str:
    if _starts_with_timestamp(line):
        return line

    if source == "app":
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            payload = None
        if isinstance(payload, dict):
            raw_ts = payload.get("ts")
            if isinstance(raw_ts, (int, float)):
                return f"{_debug_timestamp(raw_ts)} {line}"

    return f"{_debug_timestamp()} {line}"


async def _stream_command_lines(
    command: list[str], unavailable_message: str, source: str
):
    try:
        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        yield _sse_payload(_format_debug_line(unavailable_message, source))
        yield _sse_event("close", {"reason": "command_not_found"})
        return
    except Exception as exc:
        yield _sse_payload(
            _format_debug_line(f"日志进程启动失败：{exc.__class__.__name__}: {exc}", source)
        )
        return

    try:
        if process.stdout is None:
            yield _sse_payload(_format_debug_line("日志进程没有可读取的输出", source))
            return
        while True:
            line = await process.stdout.readline()
            if not line:
                return_code = await process.wait()
                if return_code:
                    yield _sse_payload(
                        _format_debug_line(f"日志进程已退出，exit={return_code}", source)
                    )
                return
            text = line.decode("utf-8", errors="replace").rstrip("\n")
            yield _sse_payload(_format_debug_line(text, source))
    finally:
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=3)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()


def _debug_log_command(source: str) -> tuple[list[str], str]:
    if source == "journal":
        journalctl = shutil.which("journalctl")
        if not journalctl:
            return (
                ["journalctl"],
                "当前系统没有 journalctl，systemd 日志只能在 Linux systemd 服务器上查看。",
            )
        return (
            [
                journalctl,
                "-u",
                settings.systemd_unit,
                "-n",
                "200",
                "-f",
                "--no-pager",
                "-o",
                "short-iso",
            ],
            "",
        )

    if source == "app":
        tail = shutil.which("tail")
        if not tail:
            return (["tail"], "当前系统没有 tail 命令，无法持续读取 app.log。")
        return (
            [tail, "-n", "200", "-F", str(settings.log_dir / "app.log")],
            "",
        )

    raise HTTPException(status_code=404, detail="Unknown debug log source")


def _history_path() -> Path:
    return settings.output_dir / "history.json"


def _jobs_path() -> Path:
    return settings.output_dir / "jobs.json"


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
    return normalized


def _sort_history_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized = [_normalize_history_record(record) for record in records]
    normalized.sort(key=lambda item: _safe_int(item.get("created_at")), reverse=True)
    return normalized


def _normalize_history_records(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return _sort_history_records(records)[:HISTORY_LIMIT]


def _load_history() -> list[dict[str, Any]]:
    path = _history_path()
    with _json_file_lock(path, exclusive=False):
        return _normalize_history_records(_read_json_list_unlocked(path))


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
        "operation": record.get("operation", "generate"),
        "status": record.get("status", "succeeded"),
        "error": record.get("error"),
        "status_code": record.get("status_code"),
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
        history = new_records + _sort_history_records(_read_json_list_unlocked(path))
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
        kept = deduped[:HISTORY_LIMIT]
        overflow = deduped[HISTORY_LIMIT:]
        job_file_references = _job_file_references() if overflow else set()
        for record in overflow:
            _delete_history_file(
                record,
                preserve_job_references=True,
                job_file_references=job_file_references,
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


def _delete_history_file(
    record: dict[str, Any],
    preserve_job_references: bool = False,
    job_file_references: set[str] | None = None,
) -> None:
    file_url = str(record.get("file") or "")
    if file_url.startswith("/files/"):
        if preserve_job_references and file_url in (
            job_file_references if job_file_references is not None else _job_file_references()
        ):
            _log_event(
                "history_overflow_file_preserved",
                history_id=record.get("id"),
                file=file_url,
                reason="referenced_by_job_result",
            )
            return
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
                _delete_history_file(removed)
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
        for record in removed:
            _delete_history_file(record)
        _write_json_list_unlocked(path, kept)
        return len(removed)


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


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "model": settings.model}


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if _is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(_read_template("login.html"))


@app.post("/login")
async def login(request: Request) -> JSONResponse:
    if not _auth_enabled():
        return JSONResponse({"ok": True})

    body = await request.json()
    password = str(body.get("password", ""))
    if not hmac.compare_digest(password, settings.app_password):
        raise HTTPException(status_code=401, detail="密码不正确")

    response = JSONResponse({"ok": True})
    response.set_cookie(
        "image_cli_session",
        _make_session_token(),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
    )
    return response


@app.get("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("image_cli_session")
    return response


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(_read_template("index.html"))


@app.on_event("startup")
def mark_interrupted_jobs() -> None:
    _mark_interrupted_jobs_on_startup()


@app.get("/debug", response_class=HTMLResponse)
def debug_page(request: Request):
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(_read_template("debug.html"))


@app.get("/v1/debug/logs/{source}")
async def debug_logs(source: str, request: Request) -> StreamingResponse:
    _require_auth(request)
    command, unavailable_message = _debug_log_command(source)
    return StreamingResponse(
        _stream_command_lines(command, unavailable_message, source),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/files/{filename}")
def serve_file(filename: str, request: Request) -> FileResponse:
    _require_auth(request)
    safe_name = Path(filename).name
    file_path = settings.output_dir / safe_name
    if not file_path.is_file():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(file_path)


@app.get("/v1/history")
def history(request: Request) -> dict[str, list[dict[str, Any]]]:
    _require_auth(request)
    return {"images": [_history_public(record) for record in _load_history()]}


@app.get("/v1/history/{history_id}")
def history_detail(history_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    for record in _load_history():
        if str(record.get("id")) == history_id:
            return record
    raise HTTPException(status_code=404, detail="History item not found")


@app.delete("/v1/history/{history_id}")
def delete_history(history_id: str, request: Request) -> dict[str, bool]:
    _require_auth(request)
    _delete_history_item(history_id)
    return {"ok": True}


@app.post("/v1/history/delete")
async def delete_history_batch(request: Request) -> dict[str, int | bool]:
    _require_auth(request)
    body = await request.json()
    raw_ids = body.get("ids", [])
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="ids must be a list")
    history_ids = [str(item) for item in raw_ids]
    deleted = _delete_history_items(history_ids)
    return {"ok": True, "deleted": deleted}


@app.post("/v1/history/download")
async def download_history_batch(request: Request) -> FileResponse:
    _require_auth(request)
    body = await request.json()
    raw_ids = body.get("ids", [])
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="ids must be a list")
    history_ids = [str(item) for item in raw_ids]
    zip_path = _build_history_zip(history_ids)
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename="history-images.zip",
        background=BackgroundTask(zip_path.unlink, missing_ok=True),
    )


def _payload_from_request(request: GenerateImageRequest) -> dict[str, Any]:
    size = request.size or settings.image_size
    _validate_size_budget(size)
    payload: dict[str, Any] = {
        "model": request.model or settings.model,
        "prompt": request.prompt,
        "size": size,
        "quality": request.quality or settings.image_quality,
        "output_format": request.output_format or settings.image_output_format,
        "n": request.n,
        "response_format": request.response_format or settings.response_format,
    }
    payload.update(request.extra)
    return payload


def _provider_request_error_detail(
    exc: httpx.HTTPError,
    payload: dict[str, Any],
    provider_url: str,
    started_at: float,
) -> dict[str, Any]:
    return {
        "error": {
            "type": exc.__class__.__name__,
            "message": str(exc) or repr(exc),
            "hint": "上游请求失败，常见原因是 provider 超时、断流、DNS/TLS/网络异常。",
        },
        "request": {
            "model": payload["model"],
            "size": payload["size"],
            "quality": payload["quality"],
            "n": payload["n"],
        },
        "provider_url": provider_url,
        "elapsed_ms": _elapsed_ms(started_at),
    }


def _provider_non_json_detail(response: httpx.Response, started_at: float) -> dict[str, Any]:
    return {
        "error": {
            "type": "ProviderNonJsonResponse",
            "message": "Provider returned non-JSON response",
        },
        "status_code": response.status_code,
        "body_preview": response.text[:2000],
        "elapsed_ms": _elapsed_ms(started_at),
    }


def _provider_response_preview(response: httpx.Response) -> Any:
    try:
        return _redact_large_payloads(response.json())
    except ValueError:
        return response.text[:1000]


def _is_retryable_provider_response(response: httpx.Response) -> bool:
    if response.status_code in {502, 503, 504}:
        return True
    if response.status_code != 500:
        return False

    detail = _provider_response_preview(response)
    detail_text = json.dumps(detail, ensure_ascii=False, default=str).lower()
    retryable_markers = (
        "stream disconnected",
        "stream error",
        "internal_error",
        "internal server error",
        "server_error",
        "bad gateway",
        "gateway timeout",
        "temporarily unavailable",
        "timeout",
    )
    return any(marker in detail_text for marker in retryable_markers)


def _retry_delay_seconds(attempt: int) -> float:
    return min(30.0, 2.0 * (2 ** attempt))


def _save_images_to_history(
    images: list[GeneratedImage],
    provider_json: dict[str, Any],
    payload: dict[str, Any],
    prompt: str,
    operation: str,
    source_file: str | None = None,
) -> None:
    now = int(time.time())
    requested_quality = str(payload["quality"])
    effective_quality = str(provider_json.get("quality") or payload["quality"])
    history_records = [
        {
            "file": image.file,
            "url": image.url,
            "prompt": prompt,
            "revised_prompt": image.revised_prompt,
            "size": payload["size"],
            "quality": effective_quality,
            "requested_quality": requested_quality,
            "actual_quality": effective_quality,
            "output_format": provider_json.get("output_format") or payload.get("output_format", ""),
            "model": payload["model"],
            "operation": operation,
            "source_file": source_file,
            "created_at": now,
        }
        for image in images
        if image.file or image.url
    ]
    _append_history(history_records)


def _save_failure_to_history(
    job_id: str,
    job: dict[str, Any],
    error: Any,
    status_code: int,
) -> None:
    payload = dict(job.get("payload") or {})
    now = int(time.time())
    _append_history(
        [
            {
                "id": f"failed-{job_id}",
                "prompt": job.get("prompt") or payload.get("prompt") or "",
                "size": payload.get("size", ""),
                "quality": payload.get("quality", ""),
                "requested_quality": payload.get("quality", ""),
                "actual_quality": "",
                "output_format": payload.get("output_format", ""),
                "model": payload.get("model", settings.model),
                "operation": "generate",
                "status": "failed",
                "error": _redact_large_payloads(error),
                "status_code": status_code,
                "created_at": now,
            }
        ]
    )


def _append_images_with_stable_indexes(
    target: list[GeneratedImage], source: list[GeneratedImage]
) -> None:
    for image in source:
        target.append(
            GeneratedImage(
                index=len(target),
                url=image.url,
                file=image.file,
                revised_prompt=image.revised_prompt,
            )
        )


def _provider_response_summary(provider_json: dict[str, Any]) -> dict[str, Any]:
    summary = {
        key: provider_json[key]
        for key in ("created", "background", "output_format", "quality", "size", "usage")
        if key in provider_json
    }
    data = provider_json.get("data")
    summary["data_count"] = len(data) if isinstance(data, list) else 0
    return summary


def _combine_provider_responses(
    provider_responses: list[dict[str, Any]],
    payload: dict[str, Any],
    returned_n: int,
) -> dict[str, Any]:
    if not provider_responses:
        return {"requested_n": payload.get("n", 1), "returned_n": returned_n}

    combined = provider_responses[0].copy()
    combined_data: list[Any] = []
    qualities: list[str] = []
    for provider_json in provider_responses:
        data = provider_json.get("data")
        if isinstance(data, list):
            combined_data.extend(data)
        quality = provider_json.get("quality")
        if quality:
            qualities.append(str(quality))

    if qualities:
        unique_qualities = sorted(set(qualities))
        combined["quality"] = unique_qualities[0] if len(unique_qualities) == 1 else "mixed"
    combined["data"] = combined_data
    combined["requested_n"] = payload.get("n", 1)
    combined["returned_n"] = returned_n
    combined["provider_request_count"] = len(provider_responses)
    combined["provider_response_summaries"] = [
        _provider_response_summary(provider_json) for provider_json in provider_responses
    ]
    return combined


async def _request_provider_images(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    provider_payload: dict[str, Any],
    request_id: str,
    started_at: float,
    provider_request_index: int,
) -> tuple[dict[str, Any], list[GeneratedImage]]:
    try:
        response = await _post_with_retry(
            client, settings.images_url, headers, provider_payload, request_id
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = _provider_error_detail(exc.response)
        _log_event(
            "generate_provider_http_error",
            request_id=request_id,
            provider_request_index=provider_request_index,
            status_code=exc.response.status_code,
            detail=detail,
            elapsed_ms=_elapsed_ms(started_at),
        )
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    except httpx.HTTPError as exc:
        detail = _provider_request_error_detail(
            exc, provider_payload, settings.images_url, started_at
        )
        _log_event(
            "generate_provider_request_error",
            request_id=request_id,
            provider_request_index=provider_request_index,
            detail=detail,
        )
        raise HTTPException(status_code=502, detail=detail) from exc

    try:
        provider_json = response.json()
    except ValueError as exc:
        detail = _provider_non_json_detail(response, started_at)
        _log_event(
            "generate_provider_non_json",
            request_id=request_id,
            provider_request_index=provider_request_index,
            detail=detail,
        )
        raise HTTPException(status_code=502, detail=detail) from exc

    _log_event(
        "generate_provider_response",
        request_id=request_id,
        provider_request_index=provider_request_index,
        status_code=response.status_code,
        provider_response=_redact_large_payloads(provider_json),
        elapsed_ms=_elapsed_ms(started_at),
    )

    try:
        images = await _normalize_images(
            provider_json,
            str(provider_payload.get("output_format") or "png"),
        )
    except HTTPException as exc:
        _log_event(
            "generate_normalize_error",
            request_id=request_id,
            provider_request_index=provider_request_index,
            detail=exc.detail,
            provider_response=_redact_large_payloads(provider_json),
            elapsed_ms=_elapsed_ms(started_at),
        )
        raise
    except Exception as exc:
        detail = {
            "error": {
                "type": exc.__class__.__name__,
                "message": str(exc) or repr(exc),
                "hint": "Provider returned JSON, but local image normalization or saving failed.",
            },
            "elapsed_ms": _elapsed_ms(started_at),
        }
        _log_event(
            "generate_normalize_unexpected_error",
            request_id=request_id,
            provider_request_index=provider_request_index,
            detail=detail,
            provider_response=_redact_large_payloads(provider_json),
        )
        raise HTTPException(status_code=502, detail=detail) from exc

    return provider_json, images


async def _execute_generation(
    request_id: str,
    payload: dict[str, Any],
    prompt: str,
    started_at: float,
    client_host: str | None = None,
) -> GenerateImageResponse:
    if not settings.api_key:
        _log_event(
            "generate_config_error",
            request_id=request_id,
            detail="OPENAI_API_KEY is not configured",
        )
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not configured")

    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
    }

    _log_event(
        "generate_start",
        request_id=request_id,
        provider_url=settings.images_url,
        payload=payload,
        client=client_host,
    )

    requested_n = max(1, _safe_int(payload.get("n"), 1))
    provider_responses: list[dict[str, Any]] = []
    images: list[GeneratedImage] = []

    async with httpx.AsyncClient(timeout=settings.timeout_seconds) as client:
        provider_json, provider_images = await _request_provider_images(
            client,
            headers,
            payload,
            request_id,
            started_at,
            provider_request_index=1,
        )
        provider_responses.append(provider_json)
        _append_images_with_stable_indexes(images, provider_images)

        missing = requested_n - len(images)
        if missing > 0:
            _log_event(
                "generate_count_shortfall",
                request_id=request_id,
                requested_n=requested_n,
                returned_n=len(images),
                missing_n=missing,
                provider_request_index=1,
            )

        for offset in range(missing):
            if len(images) >= requested_n:
                break
            single_payload = payload.copy()
            single_payload["n"] = 1
            provider_json, provider_images = await _request_provider_images(
                client,
                headers,
                single_payload,
                request_id,
                started_at,
                provider_request_index=offset + 2,
            )
            provider_responses.append(provider_json)
            _append_images_with_stable_indexes(images, provider_images)

    if len(images) > requested_n:
        images = images[:requested_n]

    provider_json = _combine_provider_responses(provider_responses, payload, len(images))

    _save_images_to_history(images, provider_json, payload, prompt, "generate")

    _log_event(
        "generate_success",
        request_id=request_id,
        elapsed_ms=_elapsed_ms(started_at),
        request=payload,
        provider_quality=provider_json.get("quality"),
        images=[
            {
                "file": image.file,
                "url": image.url,
                "revised_prompt": image.revised_prompt,
            }
            for image in images
        ],
    )

    return GenerateImageResponse(
        model=payload["model"],
        images=images,
        provider_response=_redact_large_payloads(provider_json),
    )


def _job_public(job: dict[str, Any]) -> dict[str, Any]:
    public = job.copy()
    if "result" in public:
        public["result"] = _redact_large_payloads(public["result"])
    if "error" in public:
        public["error"] = _redact_large_payloads(public["error"])
    return public


async def _run_generation_job(job_id: str) -> None:
    job = _get_job(job_id)
    if job is None:
        return
    if job.get("status") == "cancelled":
        return
    started_at = time.monotonic()
    _update_job(job_id, status="running", started_at=int(time.time()))
    _log_event("job_running", job_id=job_id, request_id=job_id, payload=job.get("payload"))
    try:
        result = await _execute_generation(
            job_id,
            dict(job["payload"]),
            str(job.get("prompt", "")),
            started_at,
            None,
        )
    except asyncio.CancelledError:
        _update_job(
            job_id,
            status="cancelled",
            cancelled_at=int(time.time()),
            note="Cancelled locally. Provider may still finish remotely if it already started.",
        )
        _log_event("job_cancelled", job_id=job_id, request_id=job_id)
        raise
    except HTTPException as exc:
        _save_failure_to_history(job_id, job, exc.detail, exc.status_code)
        _update_job(
            job_id,
            status="failed",
            error=exc.detail,
            status_code=exc.status_code,
            finished_at=int(time.time()),
            elapsed_ms=_elapsed_ms(started_at),
        )
        _log_event(
            "job_failed",
            job_id=job_id,
            request_id=job_id,
            status_code=exc.status_code,
            detail=exc.detail,
        )
    except Exception as exc:
        detail = {"error": {"type": exc.__class__.__name__, "message": str(exc) or repr(exc)}}
        _save_failure_to_history(job_id, job, detail, 500)
        _update_job(
            job_id,
            status="failed",
            error=detail,
            status_code=500,
            finished_at=int(time.time()),
            elapsed_ms=_elapsed_ms(started_at),
        )
        _log_event("job_failed_unexpected", job_id=job_id, request_id=job_id, detail=detail)
    else:
        _update_job(
            job_id,
            status="succeeded",
            result=result.model_dump(),
            finished_at=int(time.time()),
            elapsed_ms=_elapsed_ms(started_at),
        )
        _log_event("job_succeeded", job_id=job_id, request_id=job_id)
    finally:
        JOB_TASKS.pop(job_id, None)


@app.post("/v1/jobs")
async def create_job(request: Request, generation: GenerateImageRequest) -> dict[str, Any]:
    _require_auth(request)
    payload = _payload_from_request(generation)
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


@app.get("/v1/jobs")
def list_jobs(request: Request) -> dict[str, list[dict[str, Any]]]:
    _require_auth(request)
    return {"jobs": [_job_public(job) for job in _load_jobs()]}


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    job = _get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    return _job_public(job)


@app.post("/v1/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request) -> dict[str, Any]:
    _require_auth(request)
    job = _get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
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


@app.post("/v1/generate", response_model=GenerateImageResponse)
async def generate_image(
    http_request: Request, request: GenerateImageRequest
) -> GenerateImageResponse:
    request_id = uuid.uuid4().hex
    started_at = time.monotonic()
    _require_auth(http_request)
    return await _execute_generation(
        request_id=request_id,
        payload=_payload_from_request(request),
        prompt=request.prompt,
        started_at=started_at,
        client_host=http_request.client.host if http_request.client else None,
    )


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    request_id: str,
) -> httpx.Response:
    last_error: httpx.HTTPError | None = None
    last_response: httpx.Response | None = None
    for attempt in range(settings.provider_max_attempts):
        try:
            _log_event(
                "provider_request_attempt",
                request_id=request_id,
                attempt=attempt + 1,
                max_attempts=settings.provider_max_attempts,
                url=url,
                payload=payload,
            )
            response = await client.post(url, headers=headers, json=payload)
            if (
                attempt < settings.provider_max_attempts - 1
                and _is_retryable_provider_response(response)
            ):
                last_response = response
                delay = _retry_delay_seconds(attempt)
                _log_event(
                    "provider_request_retryable_status",
                    request_id=request_id,
                    attempt=attempt + 1,
                    status_code=response.status_code,
                    detail=_provider_response_preview(response),
                    retry_in_seconds=delay,
                )
                await asyncio.sleep(delay)
                continue
            return response
        except httpx.HTTPError as exc:
            last_error = exc
            _log_event(
                "provider_request_attempt_failed",
                request_id=request_id,
                attempt=attempt + 1,
                max_attempts=settings.provider_max_attempts,
                error_type=exc.__class__.__name__,
                error_message=str(exc) or repr(exc),
            )
            if attempt < settings.provider_max_attempts - 1:
                await asyncio.sleep(_retry_delay_seconds(attempt))
    if last_error is None:
        if last_response is not None:
            return last_response
        raise RuntimeError("Provider request failed without a captured exception")
    raise last_error


async def _normalize_images(
    provider_json: dict[str, Any], fallback_output_format: str = "png"
) -> list[GeneratedImage]:
    data = provider_json.get("data")
    if not isinstance(data, list):
        raise HTTPException(status_code=502, detail="Provider response missing data array")

    images: list[GeneratedImage] = []
    output_format = str(provider_json.get("output_format") or fallback_output_format or "png")
    async with httpx.AsyncClient(timeout=settings.timeout_seconds) as client:
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                continue

            file_url = None
            if item.get("b64_json"):
                file_url = _save_b64_image(str(item["b64_json"]), index, output_format)
            elif item.get("url"):
                file_url = await _download_image(client, str(item["url"]), index)

            images.append(
                GeneratedImage(
                    index=index,
                    url=item.get("url"),
                    file=file_url,
                    revised_prompt=item.get("revised_prompt"),
                )
            )

    return images


def _image_suffix_from_format(output_format: str) -> str:
    if output_format == "jpeg":
        return ".jpg"
    if output_format == "webp":
        return ".webp"
    return ".png"


def _save_b64_image(b64_json: str, index: int, output_format: str = "png") -> str:
    try:
        image_bytes = base64.b64decode(b64_json)
    except binascii.Error as exc:
        raise HTTPException(status_code=502, detail="Provider returned invalid base64") from exc

    filename = f"{uuid.uuid4().hex}-{index}{_image_suffix_from_format(output_format)}"
    output_path = settings.output_dir / filename
    output_path.write_bytes(image_bytes)
    return f"/files/{filename}"


async def _download_image(client: httpx.AsyncClient, url: str, index: int) -> str:
    try:
        response = await client.get(url)
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        _log_event(
            "image_download_failed",
            url=url,
            index=index,
            status_code=exc.response.status_code,
            detail=_provider_response_preview(exc.response),
        )
        return ""
    except httpx.HTTPError as exc:
        _log_event(
            "image_download_failed",
            url=url,
            index=index,
            error_type=exc.__class__.__name__,
            error_message=str(exc) or repr(exc),
        )
        return ""

    content_type = response.headers.get("content-type", "")
    suffix = ".png"
    if "jpeg" in content_type or "jpg" in content_type:
        suffix = ".jpg"
    elif "webp" in content_type:
        suffix = ".webp"

    filename = f"{uuid.uuid4().hex}-{index}{suffix}"
    output_path = settings.output_dir / filename
    output_path.write_bytes(response.content)
    return f"/files/{filename}"


def _provider_error_detail(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def _redact_large_payloads(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if key == "b64_json" and isinstance(item, str):
                redacted[key] = f"<redacted base64 image, {len(item)} chars>"
            else:
                redacted[key] = _redact_large_payloads(item)
        return redacted
    if isinstance(value, list):
        return [_redact_large_payloads(item) for item in value]
    return value
