import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
import zipfile
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Literal

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
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
    app_password: str = Field(default_factory=lambda: os.getenv("APP_PASSWORD", ""))
    session_secret: str = Field(
        default_factory=lambda: os.getenv("APP_SESSION_SECRET", "")
    )
    session_max_age_seconds: int = 60 * 60 * 24 * 7

    @property
    def images_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/images/generations"


settings = Settings()
settings.output_dir.mkdir(parents=True, exist_ok=True)
settings.log_dir.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Image CLI Web Service", version="0.1.0")
HISTORY_LIMIT = 200
JOB_LIMIT = 100
JOB_TASKS: dict[str, asyncio.Task] = {}

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


class GenerateImageRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=8000)
    model: str | None = None
    size: str | None = None
    quality: str | None = None
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


def _history_path() -> Path:
    return settings.output_dir / "history.json"


def _jobs_path() -> Path:
    return settings.output_dir / "jobs.json"


def _load_jobs() -> list[dict[str, Any]]:
    path = _jobs_path()
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _save_jobs(jobs: list[dict[str, Any]]) -> None:
    jobs.sort(key=lambda item: int(item.get("created_at", 0)), reverse=True)
    _jobs_path().write_text(
        json.dumps(jobs[:JOB_LIMIT], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _get_job(job_id: str) -> dict[str, Any] | None:
    for job in _load_jobs():
        if job.get("id") == job_id:
            return job
    return None


def _upsert_job(job: dict[str, Any]) -> None:
    jobs = _load_jobs()
    replaced = False
    for index, existing in enumerate(jobs):
        if existing.get("id") == job.get("id"):
            jobs[index] = job
            replaced = True
            break
    if not replaced:
        jobs.insert(0, job)
    _save_jobs(jobs)


def _update_job(job_id: str, **fields: Any) -> dict[str, Any]:
    job = _get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="Job not found")
    job.update(fields)
    job["updated_at"] = int(time.time())
    _upsert_job(job)
    return job


def _load_history() -> list[dict[str, Any]]:
    path = _history_path()
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = []
        if isinstance(data, list):
            records = [item for item in data if isinstance(item, dict)]
        else:
            records = []
    else:
        records = []

    known_files = {record.get("file") for record in records}
    for image_path in sorted(
        settings.output_dir.glob("*.png"),
        key=lambda path_item: path_item.stat().st_mtime,
        reverse=True,
    ):
        file_url = f"/files/{image_path.name}"
        if file_url in known_files:
            continue
        records.append(
            {
                "file": file_url,
                "prompt": "",
                "size": "",
                "quality": "",
                "requested_quality": "",
                "actual_quality": "",
                "created_at": int(image_path.stat().st_mtime),
            }
        )

    records.sort(key=lambda item: int(item.get("created_at", 0)), reverse=True)
    return records[:HISTORY_LIMIT]


def _history_public(record: dict[str, Any], index: int) -> dict[str, Any]:
    actual_quality = record.get("actual_quality") or record.get("quality", "")
    requested_quality = record.get("requested_quality") or actual_quality
    return {
        "id": index,
        "prompt": record.get("prompt", ""),
        "size": record.get("size", ""),
        "quality": actual_quality,
        "requested_quality": requested_quality,
        "actual_quality": actual_quality,
        "model": record.get("model", ""),
        "created_at": record.get("created_at", 0),
    }


def _save_history(records: list[dict[str, Any]]) -> None:
    _history_path().write_text(
        json.dumps(records[:HISTORY_LIMIT], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def _append_history(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    history = _load_history()
    history = records + history
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for record in history:
        file_url = str(record.get("file", ""))
        if not file_url or file_url in seen:
            continue
        seen.add(file_url)
        deduped.append(record)
    _save_history(deduped)
    return deduped[: len(records)]


def _delete_history_item(history_id: int) -> None:
    records = _load_history()
    if history_id < 0 or history_id >= len(records):
        raise HTTPException(status_code=404, detail="History item not found")

    record = records.pop(history_id)
    file_url = str(record.get("file") or "")
    if file_url.startswith("/files/"):
        file_path = settings.output_dir / Path(file_url.removeprefix("/files/")).name
        try:
            file_path.unlink(missing_ok=True)
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"Failed to delete image file: {exc}") from exc

    _save_history(records)


def _delete_history_items(history_ids: list[int]) -> int:
    records = _load_history()
    valid_ids = sorted({item for item in history_ids if 0 <= item < len(records)}, reverse=True)
    if not valid_ids:
        return 0

    for history_id in valid_ids:
        record = records.pop(history_id)
        file_url = str(record.get("file") or "")
        if file_url.startswith("/files/"):
            file_path = settings.output_dir / Path(file_url.removeprefix("/files/")).name
            try:
                file_path.unlink(missing_ok=True)
            except OSError as exc:
                raise HTTPException(status_code=500, detail=f"Failed to delete image file: {exc}") from exc

    _save_history(records)
    return len(valid_ids)


def _history_image_path(record: dict[str, Any]) -> Path | None:
    file_url = str(record.get("file") or "")
    if not file_url.startswith("/files/"):
        return None
    file_path = settings.output_dir / Path(file_url.removeprefix("/files/")).name
    if not file_path.is_file():
        return None
    return file_path


def _build_history_zip(history_ids: list[int]) -> Path:
    records = _load_history()
    valid_ids = [item for item in history_ids if 0 <= item < len(records)]
    if not valid_ids:
        raise HTTPException(status_code=400, detail="No valid history ids selected")

    zip_path = settings.output_dir / f"history-download-{uuid.uuid4().hex}.zip"
    added = 0
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for sequence, history_id in enumerate(valid_ids, start=1):
            record = records[history_id]
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
    return HTMLResponse(
        """
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Login</title>
    <style>
      :root {
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        color: #172033;
        background: #f2f4f7;
      }
      * { box-sizing: border-box; }
      body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: #f2f4f7; }
      form {
        width: min(390px, calc(100vw - 32px));
        display: grid;
        gap: 18px;
        border: 1px solid #d8dee9;
        border-radius: 10px;
        background: #ffffff;
        padding: 24px;
      }
      h1 { margin: 0; font-size: 26px; letter-spacing: 0; }
      p { margin: 0; color: #566176; font-size: 14px; }
      label { display: grid; gap: 8px; color: #344054; font-size: 13px; font-weight: 800; }
      input {
        width: 100%;
        border: 1px solid #c9d2df;
        border-radius: 8px;
        padding: 12px;
        font: inherit;
        outline: none;
      }
      input:focus { border-color: #165dcc; box-shadow: 0 0 0 3px rgba(22, 93, 204, 0.12); }
      button {
        min-height: 46px;
        border: 0;
        border-radius: 8px;
        background: #165dcc;
        color: #ffffff;
        cursor: pointer;
        font: inherit;
        font-weight: 800;
      }
      #error { min-height: 20px; color: #9f1239; font-size: 13px; white-space: pre-wrap; }
    </style>
  </head>
  <body>
    <form id="form">
      <div>
        <h1>gpt-image-2</h1>
        <p>输入密码继续使用图片生成服务</p>
      </div>
      <label>
        Password
        <input name="password" type="password" autocomplete="current-password" autofocus required />
      </label>
      <button type="submit">登录</button>
      <div id="error"></div>
    </form>
    <script>
      const form = document.querySelector("#form");
      const error = document.querySelector("#error");
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        error.textContent = "";
        const body = Object.fromEntries(new FormData(form).entries());
        const response = await fetch("/login", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        if (response.ok) {
          window.location.href = "/";
          return;
        }
        const data = await response.json().catch(() => ({}));
        error.textContent = data.detail || "登录失败";
      });
    </script>
  </body>
</html>
"""
    )


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
    return HTMLResponse("""
<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>gpt-image-2 Web Service</title>
    <style>
      :root {
        color-scheme: light;
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        background: #f2f4f7;
        color: #172033;
      }
      * { box-sizing: border-box; }
      body { margin: 0; height: 100vh; overflow: hidden; background: #f2f4f7; }
      button, textarea, input { font: inherit; }
      main {
        height: 100vh;
        min-height: 0;
        display: grid;
        grid-template-columns: 380px minmax(0, 1fr);
        overflow: hidden;
      }
      aside {
        height: 100vh;
        min-height: 0;
        overflow: auto;
        display: flex;
        flex-direction: column;
        gap: 22px;
        padding: 26px;
        background: #fbfcfe;
        border-right: 1px solid #d8dee9;
      }
      .brand {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 14px;
      }
      h1 { margin: 0; font-size: 25px; line-height: 1.08; letter-spacing: 0; }
      .model {
        width: fit-content;
        margin: 0;
        border: 1px solid #d8dee9;
        border-radius: 999px;
        background: #ffffff;
        color: #566176;
        padding: 6px 10px;
        font-size: 12px;
        font-weight: 700;
        white-space: nowrap;
      }
      form { display: grid; gap: 18px; align-content: start; }
      .field { display: grid; gap: 9px; color: #344054; font-size: 13px; font-weight: 800; }
      textarea, input[type="number"] {
        width: 100%;
        border: 1px solid #c9d2df;
        border-radius: 8px;
        background: #ffffff;
        color: #172033;
        padding: 11px 12px;
        outline: none;
      }
      textarea {
        min-height: 260px;
        resize: vertical;
        line-height: 1.5;
      }
      textarea:focus, input[type="number"]:focus {
        border-color: #1d5fd7;
        box-shadow: 0 0 0 3px rgba(29, 95, 215, 0.12);
      }
      .fields { display: grid; gap: 16px; }
      .field-grid { display: grid; grid-template-columns: 1fr; gap: 16px; }
      .count-field input { max-width: 140px; }
      .segmented {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 8px;
      }
      .segmented.quality { grid-template-columns: repeat(4, minmax(0, 1fr)); }
      .choice input { position: absolute; opacity: 0; pointer-events: none; }
      .choice span {
        min-height: 52px;
        display: grid;
        place-items: center;
        border: 1px solid #c9d2df;
        border-radius: 8px;
        background: #ffffff;
        color: #46536a;
        font-size: 12px;
        font-weight: 800;
        line-height: 1.25;
        text-align: center;
        cursor: pointer;
        user-select: none;
      }
      .choice { position: relative; }
      .choice-tip {
        display: none;
      }
      #sizeTooltip {
        position: absolute;
        left: 0;
        top: 0;
        z-index: 50;
        width: min(260px, 72vw);
        border: 1px solid #c9d2df;
        border-radius: 8px;
        background: #ffffff;
        box-shadow: 0 12px 32px rgba(22, 41, 69, 0.14);
        color: #344054;
        font-size: 12px;
        font-weight: 700;
        line-height: 1.45;
        padding: 10px 11px;
        pointer-events: none;
      }
      #sizeTooltip[hidden] { display: none; }
      #sizeTooltip::after {
        content: "";
        position: absolute;
        left: 50%;
        top: 100%;
        width: 10px;
        height: 10px;
        transform: translate(-50%, -5px) rotate(45deg);
        border-bottom: 1px solid #c9d2df;
        border-right: 1px solid #c9d2df;
        background: #ffffff;
      }
      .choice input:checked + span {
        border-color: #1d5fd7;
        background: #e8f0ff;
        color: #123f91;
      }
      .choice input:focus-visible + span {
        box-shadow: 0 0 0 3px rgba(29, 95, 215, 0.16);
      }
      button {
        min-height: 46px;
        cursor: pointer;
        border: 0;
        border-radius: 8px;
        background: #165dcc;
        color: #ffffff;
        font-weight: 800;
      }
      button:hover:not(:disabled) { background: #104fae; }
      button:disabled { cursor: wait; opacity: 0.68; }
      .workspace {
        height: 100vh;
        min-height: 0;
        display: grid;
        grid-template-rows: auto minmax(0, 1fr) 260px;
        gap: 16px;
        padding: 26px;
        overflow: hidden;
      }
      .toolbar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
      }
      .toolbar h2 { margin: 0; font-size: 20px; line-height: 1.2; }
      .toolbar-actions { display: flex; align-items: center; gap: 10px; }
      .logout {
        color: #566176;
        font-size: 13px;
        font-weight: 800;
        text-decoration: none;
      }
      .logout:hover { color: #165dcc; }
      #status {
        min-height: 30px;
        display: inline-flex;
        align-items: center;
        border: 1px solid #d8dee9;
        border-radius: 999px;
        background: #ffffff;
        color: #566176;
        padding: 6px 11px;
        font-size: 13px;
        font-weight: 700;
        text-align: right;
        white-space: pre-wrap;
      }
      #result {
        height: 100%;
        min-height: 0;
        min-width: 0;
        overflow: hidden;
        display: grid;
        grid-template-columns: 1fr;
        align-content: stretch;
        gap: 16px;
      }
      .panel {
        min-height: 0;
        display: grid;
        grid-template-rows: auto minmax(0, 1fr);
        border: 1px solid #d8dee9;
        border-radius: 8px;
        background: #ffffff;
        padding: 14px;
      }
      .panel-title {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 12px;
      }
      .panel-title h3 { margin: 0; font-size: 15px; line-height: 1.2; }
      .panel-actions { display: flex; align-items: center; gap: 8px; }
      #history {
        min-height: 0;
        max-height: none;
        display: grid;
        align-content: start;
        gap: 8px;
        overflow-y: auto;
        overflow-x: hidden;
        padding-right: 4px;
      }
      .history-item {
        width: 100%;
        height: 74px;
        position: relative;
        appearance: none;
        -webkit-appearance: none;
        display: grid;
        grid-template-columns: minmax(0, 1fr) auto;
        align-items: center;
        gap: 10px;
        border: 1px solid #d8dee9;
        border-radius: 8px;
        background: #ffffff;
        color: inherit;
        padding: 7px;
        text-align: left;
      }
      .selecting .history-item { grid-template-columns: auto minmax(0, 1fr) auto; }
      .history-check {
        display: none;
        width: 18px;
        height: 18px;
        accent-color: #64748b;
      }
      .selecting .history-check { display: block; }
      .history-item::before {
        content: "";
        position: absolute;
        inset: 8px auto 8px 0;
        width: 3px;
        border-radius: 999px;
        background: transparent;
      }
      .history-item:hover:not(:disabled),
      .history-item:focus:not(:disabled),
      .history-item:active:not(:disabled) {
        border-color: #aeb9c9;
        background: #f7f8fa;
        color: inherit;
        box-shadow: 0 6px 18px rgba(22, 41, 69, 0.05);
        outline: none;
      }
      .history-item:hover::before,
      .history-item:focus::before { background: #94a3b8; }
      .history-meta { min-width: 0; display: grid; gap: 3px; }
      .prompt-line {
        color: #344054;
        min-width: 0;
        font-size: 12px;
        font-weight: 700;
        line-height: 1.45;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .muted { color: #647086; font-size: 12px; }
      .view-pill {
        border: 1px solid #c9d2df;
        border-radius: 999px;
        background: #ffffff;
        color: #344054;
        padding: 6px 10px;
        font-size: 12px;
        font-weight: 800;
      }
      .history-item:hover .view-pill {
        border-color: #94a3b8;
        background: #ffffff;
        color: #172033;
      }
      .empty {
        min-height: 100%;
        height: 100%;
        display: grid;
        place-items: center;
        border: 1px dashed #b7c0cf;
        border-radius: 8px;
        color: #566176;
        background: #ffffff;
        text-align: center;
        padding: 24px;
      }
      .loader {
        display: grid;
        justify-items: center;
        gap: 12px;
      }
      .spinner {
        width: 36px;
        height: 36px;
        border: 3px solid #d8dee9;
        border-top-color: #165dcc;
        border-radius: 50%;
        animation: spin 1s linear infinite;
      }
      @keyframes spin { to { transform: rotate(360deg); } }
      figure {
        min-width: 0;
        min-height: 0;
        margin: 0;
        display: grid;
        gap: 10px;
        border: 1px solid #d8dee9;
        border-radius: 8px;
        background: #ffffff;
        padding: 12px;
      }
      .result-card {
        height: 100%;
        min-height: 0;
        grid-template-rows: minmax(0, 1fr) auto;
        overflow: hidden;
      }
      figure img {
        width: 100%;
        height: 100%;
        min-height: 0;
        max-height: none;
        object-fit: contain;
        border-radius: 6px;
        background: #eef2f7;
      }
      figcaption {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 10px;
        color: #647086;
        font-size: 13px;
      }
      .actions { display: flex; gap: 10px; align-items: center; }
      figcaption a { color: #165dcc; font-weight: 800; text-decoration: none; }
      figcaption a:hover { text-decoration: underline; }
      .text-action {
        border: 0;
        background: transparent;
        color: #165dcc;
        cursor: pointer;
        font-size: 13px;
        font-weight: 800;
        padding: 0;
      }
      .text-action:hover { text-decoration: underline; }
      .error {
        grid-column: 1 / -1;
        white-space: pre-wrap;
        border: 1px solid #fecaca;
        border-radius: 8px;
        background: #fff1f2;
        color: #9f1239;
        padding: 14px;
      }
      .modal[hidden] { display: none; }
      .modal {
        position: fixed;
        inset: 0;
        z-index: 20;
        display: grid;
        grid-template-columns: minmax(0, 1fr) min(380px, 34vw);
        gap: 0;
        background: rgba(15, 23, 42, 0.72);
        padding: 28px;
      }
      .modal-image {
        min-width: 0;
        display: grid;
        place-items: center;
        border-radius: 8px 0 0 8px;
        background: #0f172a;
        overflow: hidden;
      }
      .modal-image img {
        max-width: 100%;
        max-height: calc(100vh - 56px);
        object-fit: contain;
      }
      .modal-info {
        min-width: 0;
        display: grid;
        grid-template-rows: auto auto 1fr auto;
        gap: 14px;
        border-radius: 0 8px 8px 0;
        background: #ffffff;
        padding: 18px;
      }
      .modal-info h3 { margin: 0; font-size: 18px; }
      .modal-prompt {
        min-height: 0;
        overflow: auto;
        border: 1px solid #d8dee9;
        border-radius: 8px;
        background: #f8fafc;
        color: #172033;
        padding: 12px;
        white-space: pre-wrap;
        word-break: break-word;
      }
      .pill-action {
        min-height: auto;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        border: 1px solid #c9d2df;
        border-radius: 999px;
        background: #ffffff;
        color: #344054;
        cursor: pointer;
        font: inherit;
        font-size: 13px;
        font-weight: 800;
        line-height: 1;
        padding: 9px 12px;
        text-decoration: none;
      }
      .pill-action:hover,
      .pill-action:focus,
      .pill-action:active,
      button.pill-action:hover:not(:disabled),
      button.pill-action:focus:not(:disabled),
      button.pill-action:active:not(:disabled) {
        border-color: #94a3b8;
        background: #f7f8fa;
        color: #172033;
        outline: none;
        text-decoration: none;
      }
      .danger-action {
        border-color: #fecaca;
        color: #9f1239;
      }
      .danger-action:hover,
      .danger-action:focus,
      .danger-action:active,
      button.danger-action:hover:not(:disabled),
      button.danger-action:focus:not(:disabled),
      button.danger-action:active:not(:disabled) {
        border-color: #fca5a5;
        background: #fff1f2;
        color: #9f1239;
      }
      .modal-actions { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; }
      @media (max-width: 820px) {
        body { height: auto; min-height: 100vh; overflow: auto; }
        main { height: auto; min-height: 100vh; grid-template-columns: 1fr; overflow: visible; }
        aside { height: auto; min-height: auto; border-right: 0; border-bottom: 1px solid #d9e1ee; }
        .workspace { min-height: auto; grid-template-rows: auto minmax(420px, 60vh) auto; }
        .empty { min-height: 320px; }
        .modal { grid-template-columns: 1fr; grid-template-rows: minmax(0, 1fr) auto; padding: 14px; }
        .modal-image { border-radius: 8px 8px 0 0; }
        .modal-info { border-radius: 0 0 8px 8px; max-height: 42vh; }
      }
      @media (max-width: 460px) {
        aside, .workspace { padding: 16px; }
        .field-grid, .segmented, .segmented.quality { grid-template-columns: 1fr; }
        .toolbar { align-items: flex-start; flex-direction: column; }
        #status { text-align: left; }
      }
    </style>
  </head>
  <body>
    <main>
      <aside>
        <div class="brand">
          <h1>gpt-image-2</h1>
          <p class="model">gpt-image-2</p>
        </div>
        <form id="form">
          <label class="field">
            Prompt
            <textarea name="prompt" placeholder="输入图片提示词" required></textarea>
          </label>
          <div class="fields">
            <div class="field">
              Size
              <div class="segmented" role="radiogroup" aria-label="尺寸">
                <label class="choice"><input type="radio" name="size" value="1024x1024" checked><span>Square<br>1024x1024</span><em class="choice-tip">常用于头像、图标、产品主图、社媒图片和电商商品图。构图稳定，适合先试 prompt。</em></label>
                <label class="choice"><input type="radio" name="size" value="1536x1024"><span>Landscape<br>1536x1024</span><em class="choice-tip">常用于摄影感图片、风景、产品横幅、文章配图和人物环境照。比 16:9 更像照片。</em></label>
                <label class="choice"><input type="radio" name="size" value="1024x1536"><span>Portrait<br>1024x1536</span><em class="choice-tip">常用于人物写真、竖版海报、手机壁纸、角色图、时尚图和电商详情图。</em></label>
                <label class="choice"><input type="radio" name="size" value="3840x2160"><span>4K Wide<br>3840x2160</span><em class="choice-tip">常用于宽屏壁纸、视频封面、横屏场景图、电影感画面、建筑和游戏场景。</em></label>
              </div>
            </div>
            <div class="field-grid">
              <div class="field">
                Quality
                <div class="segmented quality" role="radiogroup" aria-label="质量">
                  <label class="choice"><input type="radio" name="quality" value="auto" checked><span>auto</span></label>
                  <label class="choice"><input type="radio" name="quality" value="high"><span>high</span></label>
                  <label class="choice"><input type="radio" name="quality" value="medium"><span>medium</span></label>
                  <label class="choice"><input type="radio" name="quality" value="low"><span>low</span></label>
                </div>
              </div>
              <label class="field count-field">
                Count
                <input name="n" type="number" min="1" max="10" value="1" aria-label="数量" />
              </label>
            </div>
          </div>
          <button id="submit" type="submit">生成图片</button>
        </form>
      </aside>
      <section class="workspace">
        <div class="toolbar">
          <h2>生成结果</h2>
          <div class="toolbar-actions">
            <div id="status">Ready</div>
            <a class="logout" href="/logout">退出</a>
          </div>
        </div>
        <div id="result">
          <div class="empty">No image yet</div>
        </div>
        <section class="panel">
          <div class="panel-title">
            <h3>历史出图</h3>
            <div class="panel-actions">
              <span class="muted" id="historyCount">Loading</span>
              <button class="pill-action" id="selectHistory" type="button">选择</button>
              <button class="pill-action" id="downloadSelected" type="button" hidden>下载所选</button>
              <button class="pill-action danger-action" id="deleteSelected" type="button" hidden>删除所选</button>
            </div>
          </div>
          <div id="history">
            <div class="empty">正在加载历史记录...</div>
          </div>
        </section>
      </section>
    </main>
    <div id="sizeTooltip" hidden></div>
    <div class="modal" id="historyModal" hidden>
      <div class="modal-image">
        <img id="modalImage" alt="history image" />
      </div>
      <div class="modal-info">
        <div class="toolbar">
          <h3>历史详情</h3>
          <button class="pill-action" id="modalClose" type="button">关闭</button>
        </div>
        <div class="muted" id="modalMeta"></div>
        <div class="modal-prompt" id="modalPrompt"></div>
        <div class="modal-actions">
          <a class="pill-action" id="modalOpen" href="#">打开原图</a>
          <a class="pill-action" id="modalDownload" href="#" download>下载</a>
          <button class="pill-action" id="modalReuse" type="button">使用此 Prompt</button>
        </div>
      </div>
    </div>
    <script>
      const form = document.querySelector("#form");
      const result = document.querySelector("#result");
      const history = document.querySelector("#history");
      const historyCount = document.querySelector("#historyCount");
      const selectHistory = document.querySelector("#selectHistory");
      const downloadSelected = document.querySelector("#downloadSelected");
      const deleteSelected = document.querySelector("#deleteSelected");
      const status = document.querySelector("#status");
      const submit = document.querySelector("#submit");
      const promptInput = form.elements.prompt;
      const modal = document.querySelector("#historyModal");
      const modalImage = document.querySelector("#modalImage");
      const modalMeta = document.querySelector("#modalMeta");
      const modalPrompt = document.querySelector("#modalPrompt");
      const modalOpen = document.querySelector("#modalOpen");
      const modalDownload = document.querySelector("#modalDownload");
      const modalReuse = document.querySelector("#modalReuse");
      const modalClose = document.querySelector("#modalClose");
      const sizeTooltip = document.querySelector("#sizeTooltip");
      let activeHistoryRecord = null;
      let selectingHistory = false;
      const selectedHistoryIds = new Set();
      let timer = null;
      function formatError(data) {
        if (typeof data?.detail === "string") return data.detail;
        if (data?.detail) return JSON.stringify(data.detail, null, 2);
        return JSON.stringify(data || { error: "生成失败" }, null, 2);
      }
      function setBusy(message) {
        let seconds = 0;
        status.textContent = `${message} · ${seconds}s`;
        clearInterval(timer);
        timer = setInterval(() => {
          seconds += 1;
          status.textContent = `${message} · ${seconds}s`;
        }, 1000);
      }
      function stopBusy(message) {
        clearInterval(timer);
        timer = null;
        status.textContent = message;
      }
      function formatDate(value) {
        if (!value) return "";
        return new Date(value * 1000).toLocaleString();
      }
      function formatQuality(meta = {}) {
        const requested = meta.requested_quality || meta.requestedQuality || meta.quality || "";
        const actual = meta.actual_quality || meta.actualQuality || meta.quality || "";
        if (requested || actual) return `选择:${requested || "-"} / 实际:${actual || "-"}`;
        return "";
      }
      function showSizeTooltip(choice) {
        const tip = choice.querySelector(".choice-tip");
        if (!tip) return;
        const rect = choice.getBoundingClientRect();
        sizeTooltip.textContent = tip.textContent;
        sizeTooltip.hidden = false;
        const tooltipRect = sizeTooltip.getBoundingClientRect();
        const left = Math.min(
          Math.max(12, rect.left + rect.width / 2 - tooltipRect.width / 2),
          window.innerWidth - tooltipRect.width - 12
        );
        let top = Math.max(12, rect.top - tooltipRect.height - 4);
        if (top < 12 && rect.bottom + tooltipRect.height + 4 < window.innerHeight - 12) {
          top = rect.bottom + 4;
        }
        sizeTooltip.style.left = `${left}px`;
        sizeTooltip.style.top = `${top}px`;
      }
      function hideSizeTooltip() {
        sizeTooltip.hidden = true;
      }
      document.querySelectorAll('input[name="size"]').forEach((input) => {
        const choice = input.closest(".choice");
        choice.addEventListener("mouseenter", () => showSizeTooltip(choice));
        choice.addEventListener("mouseleave", hideSizeTooltip);
        input.addEventListener("focus", () => showSizeTooltip(choice));
        input.addEventListener("blur", hideSizeTooltip);
      });
      function createImageCard(image, meta = {}, compact = false) {
        const figure = document.createElement("figure");
        const img = document.createElement("img");
        const link = document.createElement("a");
        const download = document.createElement("a");
        const src = image.file || image.url;
        const caption = document.createElement("figcaption");
        const actions = document.createElement("span");
        img.src = src;
        img.alt = meta.prompt || "generated image";
        link.href = src;
        link.target = "_blank";
        link.rel = "noreferrer";
        link.textContent = "打开原图";
        download.href = src;
        download.download = src.split("/").pop() || "image.png";
        download.textContent = "下载";
        actions.className = "actions";
        actions.append(link, download);

        if (compact) {
          const prompt = document.createElement("div");
          const detail = document.createElement("div");
          const reuse = document.createElement("button");
          prompt.className = "prompt-line";
          prompt.title = meta.prompt || "";
          prompt.textContent = meta.prompt || "历史图片";
          detail.className = "muted";
          detail.textContent = [meta.size, formatQuality(meta), formatDate(meta.created_at)].filter(Boolean).join(" · ");
          reuse.type = "button";
          reuse.className = "text-action";
          reuse.textContent = "使用此 Prompt";
          reuse.addEventListener("click", () => {
            promptInput.value = meta.prompt || "";
            promptInput.focus();
          });
          actions.append(reuse);
          caption.append(prompt, detail, actions);
        } else {
          const detail = document.createElement("span");
          detail.textContent = [meta.size, formatQuality(meta)].filter(Boolean).join(" · ");
          caption.append(detail, actions);
        }

        if (!compact) figure.className = "result-card";
        figure.append(img, caption);
        return figure;
      }
      async function openHistoryModal(record) {
        modalPrompt.textContent = "加载中...";
        modalMeta.textContent = [record.size, formatQuality(record), formatDate(record.created_at)].filter(Boolean).join(" · ");
        modal.hidden = false;
        const response = await fetch(`/v1/history/${record.id}`);
        if (!response.ok) {
          modalPrompt.textContent = `历史详情加载失败：HTTP ${response.status}`;
          return;
        }
        const detail = await response.json();
        const src = detail.file || detail.url;
        if (!src) {
          modalPrompt.textContent = "这条历史记录没有可用图片地址";
          return;
        }
        record = detail;
        activeHistoryRecord = record;
        modalImage.src = src;
        modalImage.alt = record.prompt || "history image";
        modalMeta.textContent = [record.size, formatQuality(record), formatDate(record.created_at)].filter(Boolean).join(" · ");
        modalPrompt.textContent = record.prompt || "无 prompt";
        modalOpen.href = src;
        modalDownload.href = src;
        modalDownload.download = src.split("/").pop() || "image.png";
        modal.hidden = false;
      }
      function closeHistoryModal() {
        modal.hidden = true;
        modalImage.removeAttribute("src");
        activeHistoryRecord = null;
      }
      modalClose.addEventListener("click", closeHistoryModal);
      modal.addEventListener("click", (event) => {
        if (event.target === modal) closeHistoryModal();
      });
      modalReuse.addEventListener("click", () => {
        if (!activeHistoryRecord) return;
        promptInput.value = activeHistoryRecord.prompt || "";
        promptInput.focus();
        closeHistoryModal();
      });
      window.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && !modal.hidden) closeHistoryModal();
      });
      function updateSelectionUi() {
        history.classList.toggle("selecting", selectingHistory);
        selectHistory.textContent = selectingHistory ? "取消" : "选择";
        downloadSelected.hidden = !selectingHistory || selectedHistoryIds.size === 0;
        deleteSelected.hidden = !selectingHistory || selectedHistoryIds.size === 0;
        downloadSelected.textContent = `下载所选${selectedHistoryIds.size ? ` (${selectedHistoryIds.size})` : ""}`;
        deleteSelected.textContent = `删除所选${selectedHistoryIds.size ? ` (${selectedHistoryIds.size})` : ""}`;
      }
      selectHistory.addEventListener("click", () => {
        selectingHistory = !selectingHistory;
        selectedHistoryIds.clear();
        history.querySelectorAll(".history-check").forEach((input) => {
          input.checked = false;
        });
        updateSelectionUi();
      });
      downloadSelected.addEventListener("click", async () => {
        if (!selectedHistoryIds.size) return;
        const response = await fetch("/v1/history/download", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ids: Array.from(selectedHistoryIds) })
        });
        if (!response.ok) {
          alert(`下载失败：HTTP ${response.status}`);
          return;
        }
        const blob = await response.blob();
        const url = URL.createObjectURL(blob);
        const link = document.createElement("a");
        link.href = url;
        link.download = "history-images.zip";
        document.body.append(link);
        link.click();
        link.remove();
        URL.revokeObjectURL(url);
      });
      deleteSelected.addEventListener("click", async () => {
        if (!selectedHistoryIds.size) return;
        if (!confirm(`确定删除选中的 ${selectedHistoryIds.size} 条历史出图吗？本地图片文件也会一起删除。`)) return;
        const response = await fetch("/v1/history/delete", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ ids: Array.from(selectedHistoryIds) })
        });
        if (!response.ok) {
          alert(`删除失败：HTTP ${response.status}`);
          return;
        }
        selectingHistory = false;
        selectedHistoryIds.clear();
        await loadHistory();
      });
      function createHistoryItem(record) {
        const button = document.createElement("button");
        const check = document.createElement("input");
        const meta = document.createElement("div");
        const prompt = document.createElement("div");
        const detail = document.createElement("div");
        const arrow = document.createElement("span");
        button.type = "button";
        button.className = "history-item";
        check.type = "checkbox";
        check.className = "history-check";
        check.checked = selectedHistoryIds.has(record.id);
        check.addEventListener("click", (event) => {
          event.stopPropagation();
          if (check.checked) selectedHistoryIds.add(record.id);
          else selectedHistoryIds.delete(record.id);
          updateSelectionUi();
        });
        meta.className = "history-meta";
        prompt.className = "prompt-line";
        prompt.textContent = record.prompt || "历史图片";
        detail.className = "muted";
        detail.textContent = [record.size, formatQuality(record), formatDate(record.created_at)].filter(Boolean).join(" · ");
        arrow.className = "view-pill pill-action";
        arrow.textContent = "查看";
        meta.append(prompt, detail);
        button.append(check, meta, arrow);
        button.addEventListener("click", () => {
          if (selectingHistory) {
            check.checked = !check.checked;
            if (check.checked) selectedHistoryIds.add(record.id);
            else selectedHistoryIds.delete(record.id);
            updateSelectionUi();
            return;
          }
          openHistoryModal(record);
        });
        return button;
      }
      function renderHistory(records) {
        historyCount.textContent = `${records.length} item${records.length === 1 ? "" : "s"}`;
        if (!records.length) {
          history.replaceChildren(Object.assign(document.createElement("div"), {
            className: "empty",
            textContent: "还没有历史出图"
          }));
          return;
        }
        history.replaceChildren(...records.map((record) => createHistoryItem(record)));
        updateSelectionUi();
      }
      async function loadHistory() {
        try {
          const response = await fetch("/v1/history");
          if (!response.ok) throw new Error(`HTTP ${response.status}`);
          const data = await response.json();
          renderHistory(data.images || []);
        } catch (error) {
          historyCount.textContent = "Failed";
          history.replaceChildren(Object.assign(document.createElement("div"), {
            className: "error",
            textContent: `历史记录加载失败：${error.message}`
          }));
        }
      }
      function showJobState(job, body) {
        const state = document.createElement("div");
        state.className = "empty";
        const canCancel = job.status === "queued" || job.status === "running";
        state.innerHTML = `
          <div class="loader">
            ${canCancel ? '<div class="spinner"></div>' : ""}
            <div>Job ${job.status}</div>
            <div class="muted">${job.id}</div>
            ${canCancel ? '<button class="pill-action danger-action" type="button">取消生成</button>' : ""}
          </div>
        `;
        if (canCancel) {
          state.querySelector("button").addEventListener("click", async () => {
            await fetch(`/v1/jobs/${job.id}/cancel`, { method: "POST" });
            stopBusy("Cancelled");
            submit.disabled = false;
            await pollJob(job.id, body);
          });
        }
        result.replaceChildren(state);
      }
      async function pollJob(jobId, body) {
        let polls = 0;
        while (true) {
          const response = await fetch(`/v1/jobs/${jobId}`);
          polls += 1;
          const job = await response.json();
          if (!response.ok) {
            stopBusy("Failed");
            submit.disabled = false;
            result.replaceChildren(Object.assign(document.createElement("div"), {
              className: "error",
              textContent: `Job 查询失败 (${response.status})：\n${formatError(job)}`
            }));
            return;
          }
          if (job.status === "queued" || job.status === "running") {
            showJobState(job, body);
            const delay = polls < 5 ? 2000 : polls < 9 ? 5000 : 10000;
            await new Promise((resolve) => setTimeout(resolve, delay));
            continue;
          }
          submit.disabled = false;
          if (job.status === "cancelled") {
            stopBusy("Cancelled");
            result.replaceChildren(Object.assign(document.createElement("div"), {
              className: "empty",
              textContent: "生成已取消。本地不会保存这次结果；如果 provider 已经开始生成，它可能仍会在远端完成。"
            }));
            return;
          }
          if (job.status === "failed") {
            stopBusy("Failed");
            result.replaceChildren(Object.assign(document.createElement("div"), {
              className: "error",
              textContent: `生成失败 (${job.status_code || 500})：\n${JSON.stringify(job.error || job, null, 2)}`
            }));
            return;
          }
          const data = job.result;
          const meta = {
            prompt: body.prompt,
            size: body.size,
            quality: data?.provider_response?.quality || body.quality,
            requested_quality: body.quality,
            actual_quality: data?.provider_response?.quality || body.quality
          };
          const imageNodes = (data?.images || [])
            .filter((image) => image.file || image.url)
            .map((image) => createImageCard(image, meta));
          stopBusy(`${imageNodes.length} image${imageNodes.length === 1 ? "" : "s"}`);
          result.replaceChildren(...imageNodes);
          await loadHistory();
          return;
        }
      }
      async function resumeActiveJob() {
        const response = await fetch("/v1/jobs");
        if (!response.ok) return;
        const data = await response.json();
        const active = (data.jobs || []).find((job) => job.status === "queued" || job.status === "running");
        if (!active) return;
        const payload = active.payload || {};
        const body = {
          prompt: active.prompt || payload.prompt || "",
          size: payload.size || "",
          quality: payload.quality || "auto",
          n: payload.n || 1
        };
        setBusy("Generating");
        submit.disabled = true;
        showJobState(active, body);
        await pollJob(active.id, body);
      }

      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        setBusy("Generating");
        submit.disabled = true;
        const loader = document.createElement("div");
        loader.className = "empty";
        loader.innerHTML = '<div class="loader"><div class="spinner"></div><div>生成中，4K 或复杂提示词可能需要几分钟...</div></div>';
        result.replaceChildren(loader);
        const body = Object.fromEntries(new FormData(form).entries());
        body.n = Number(body.n);
        let response;
        let data;
        try {
          response = await fetch("/v1/jobs", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(body)
          });
          data = await response.json();
        } catch (error) {
          stopBusy("Failed");
          submit.disabled = false;
          result.replaceChildren(Object.assign(document.createElement("div"), {
            className: "error",
            textContent: `请求失败：${error.message}`
          }));
          return;
        }
        if (!response.ok) {
          stopBusy("Failed");
          submit.disabled = false;
          result.replaceChildren(Object.assign(document.createElement("div"), {
            className: "error",
            textContent: `生成失败 (${response.status})：\n${formatError(data)}`
          }));
          return;
        }
        showJobState(data, body);
        await pollJob(data.id, body);
      });
      loadHistory();
      resumeActiveJob();
    </script>
  </body>
</html>
""")


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
    return {
        "images": [
            _history_public(record, index)
            for index, record in enumerate(_load_history())
        ]
    }


@app.get("/v1/history/{history_id}")
def history_detail(history_id: int, request: Request) -> dict[str, Any]:
    _require_auth(request)
    records = _load_history()
    if history_id < 0 or history_id >= len(records):
        raise HTTPException(status_code=404, detail="History item not found")
    record = records[history_id].copy()
    record["id"] = history_id
    return record


@app.delete("/v1/history/{history_id}")
def delete_history(history_id: int, request: Request) -> dict[str, bool]:
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
    history_ids = [int(item) for item in raw_ids]
    deleted = _delete_history_items(history_ids)
    return {"ok": True, "deleted": deleted}


@app.post("/v1/history/download")
async def download_history_batch(request: Request) -> FileResponse:
    _require_auth(request)
    body = await request.json()
    raw_ids = body.get("ids", [])
    if not isinstance(raw_ids, list):
        raise HTTPException(status_code=400, detail="ids must be a list")
    history_ids = [int(item) for item in raw_ids]
    zip_path = _build_history_zip(history_ids)
    return FileResponse(
        zip_path,
        media_type="application/zip",
        filename="history-images.zip",
    )


def _payload_from_request(request: GenerateImageRequest) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": request.model or settings.model,
        "prompt": request.prompt,
        "size": request.size or settings.image_size,
        "quality": request.quality or settings.image_quality,
        "n": request.n,
        "response_format": request.response_format or settings.response_format,
    }
    payload.update(request.extra)
    return payload


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

    try:
        async with httpx.AsyncClient(timeout=settings.timeout_seconds) as client:
            response = await _post_with_retry(
                client, settings.images_url, headers, payload, request_id
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = _provider_error_detail(exc.response)
        _log_event(
            "generate_provider_http_error",
            request_id=request_id,
            status_code=exc.response.status_code,
            detail=detail,
            elapsed_ms=_elapsed_ms(started_at),
        )
        print(f"Provider returned {exc.response.status_code}: {detail}")
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    except httpx.HTTPError as exc:
        detail = {
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
            "provider_url": settings.images_url,
            "elapsed_ms": _elapsed_ms(started_at),
        }
        _log_event("generate_provider_request_error", request_id=request_id, detail=detail)
        print(f"Provider request failed: {exc!r}")
        raise HTTPException(status_code=502, detail=detail) from exc

    try:
        provider_json = response.json()
    except ValueError as exc:
        detail = {
            "error": {
                "type": "ProviderNonJsonResponse",
                "message": "Provider returned non-JSON response",
            },
            "status_code": response.status_code,
            "body_preview": response.text[:2000],
            "elapsed_ms": _elapsed_ms(started_at),
        }
        _log_event("generate_provider_non_json", request_id=request_id, detail=detail)
        print(f"Provider returned non-JSON response: {response.text[:1000]}")
        raise HTTPException(status_code=502, detail=detail) from exc

    _log_event(
        "generate_provider_response",
        request_id=request_id,
        status_code=response.status_code,
        provider_response=_redact_large_payloads(provider_json),
        elapsed_ms=_elapsed_ms(started_at),
    )

    try:
        images = await _normalize_images(provider_json)
    except HTTPException as exc:
        _log_event(
            "generate_normalize_error",
            request_id=request_id,
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
            detail=detail,
            provider_response=_redact_large_payloads(provider_json),
        )
        raise HTTPException(status_code=502, detail=detail) from exc

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
            "model": payload["model"],
            "created_at": now,
        }
        for image in images
        if image.file or image.url
    ]
    _append_history(history_records)

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
    _upsert_job(job)
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
    if not settings.api_key:
        _log_event(
            "generate_config_error",
            request_id=request_id,
            detail="OPENAI_API_KEY is not configured",
        )
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY is not configured")

    payload: dict[str, Any] = {
        "model": request.model or settings.model,
        "prompt": request.prompt,
        "size": request.size or settings.image_size,
        "quality": request.quality or settings.image_quality,
        "n": request.n,
        "response_format": request.response_format or settings.response_format,
    }
    payload.update(request.extra)

    headers = {
        "Authorization": f"Bearer {settings.api_key}",
        "Content-Type": "application/json",
    }

    _log_event(
        "generate_start",
        request_id=request_id,
        provider_url=settings.images_url,
        payload=payload,
        client=http_request.client.host if http_request.client else None,
    )

    try:
        async with httpx.AsyncClient(timeout=settings.timeout_seconds) as client:
            response = await _post_with_retry(
                client, settings.images_url, headers, payload, request_id
            )
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = _provider_error_detail(exc.response)
        _log_event(
            "generate_provider_http_error",
            request_id=request_id,
            status_code=exc.response.status_code,
            detail=detail,
            elapsed_ms=_elapsed_ms(started_at),
        )
        print(f"Provider returned {exc.response.status_code}: {detail}")
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    except httpx.HTTPError as exc:
        detail = {
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
            "provider_url": settings.images_url,
            "elapsed_ms": _elapsed_ms(started_at),
        }
        _log_event("generate_provider_request_error", request_id=request_id, detail=detail)
        print(f"Provider request failed: {exc!r}")
        raise HTTPException(status_code=502, detail=detail) from exc

    try:
        provider_json = response.json()
    except ValueError as exc:
        detail = {
            "error": {
                "type": "ProviderNonJsonResponse",
                "message": "Provider returned non-JSON response",
            },
            "status_code": response.status_code,
            "body_preview": response.text[:2000],
            "elapsed_ms": _elapsed_ms(started_at),
        }
        _log_event("generate_provider_non_json", request_id=request_id, detail=detail)
        print(f"Provider returned non-JSON response: {response.text[:1000]}")
        raise HTTPException(status_code=502, detail=detail) from exc

    _log_event(
        "generate_provider_response",
        request_id=request_id,
        status_code=response.status_code,
        provider_response=_redact_large_payloads(provider_json),
        elapsed_ms=_elapsed_ms(started_at),
    )

    try:
        images = await _normalize_images(provider_json)
    except HTTPException as exc:
        _log_event(
            "generate_normalize_error",
            request_id=request_id,
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
            detail=detail,
            provider_response=_redact_large_payloads(provider_json),
        )
        raise HTTPException(status_code=502, detail=detail) from exc
    now = int(time.time())
    requested_quality = str(payload["quality"])
    effective_quality = str(provider_json.get("quality") or payload["quality"])
    history_records = [
        {
            "file": image.file,
            "url": image.url,
            "prompt": request.prompt,
            "revised_prompt": image.revised_prompt,
            "size": payload["size"],
            "quality": effective_quality,
            "requested_quality": requested_quality,
            "actual_quality": effective_quality,
            "model": payload["model"],
            "created_at": now,
        }
        for image in images
        if image.file or image.url
    ]
    _append_history(history_records)

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


async def _post_with_retry(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    request_id: str,
) -> httpx.Response:
    last_error: httpx.HTTPError | None = None
    for attempt in range(2):
        try:
            _log_event(
                "provider_request_attempt",
                request_id=request_id,
                attempt=attempt + 1,
                url=url,
                payload=payload,
            )
            return await client.post(url, headers=headers, json=payload)
        except httpx.HTTPError as exc:
            last_error = exc
            _log_event(
                "provider_request_attempt_failed",
                request_id=request_id,
                attempt=attempt + 1,
                error_type=exc.__class__.__name__,
                error_message=str(exc) or repr(exc),
            )
            print(f"Provider request attempt {attempt + 1} failed: {exc!r}")
            if attempt == 0:
                await asyncio.sleep(1)
    assert last_error is not None
    raise last_error


async def _normalize_images(provider_json: dict[str, Any]) -> list[GeneratedImage]:
    data = provider_json.get("data")
    if not isinstance(data, list):
        raise HTTPException(status_code=502, detail="Provider response missing data array")

    images: list[GeneratedImage] = []
    async with httpx.AsyncClient(timeout=settings.timeout_seconds) as client:
        for index, item in enumerate(data):
            if not isinstance(item, dict):
                continue

            file_url = None
            if item.get("b64_json"):
                file_url = _save_b64_image(str(item["b64_json"]), index)
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


def _save_b64_image(b64_json: str, index: int) -> str:
    try:
        image_bytes = base64.b64decode(b64_json)
    except binascii.Error as exc:
        raise HTTPException(status_code=502, detail="Provider returned invalid base64") from exc

    filename = f"{uuid.uuid4().hex}-{index}.png"
    output_path = settings.output_dir / filename
    output_path.write_bytes(image_bytes)
    return f"/files/{filename}"


async def _download_image(client: httpx.AsyncClient, url: str, index: int) -> str:
    try:
        response = await client.get(url)
        response.raise_for_status()
    except httpx.HTTPError:
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
