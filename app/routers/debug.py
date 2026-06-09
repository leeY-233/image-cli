from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse

from ..auth import is_authenticated, require_auth
from ..constants import DEBUG_LOG_SERVICE_SLOTS, DEBUG_LOG_TYPE_FILE
from ..debug_logs import (
    debug_log_command,
    get_debug_log_service,
    normalize_debug_log_services,
    stream_command_lines,
    stream_file_lines,
    validate_debug_log_service,
)
from ..template_loader import read_template


router = APIRouter()


@router.get("/debug", response_class=HTMLResponse)
def debug_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(read_template("debug.html"))


@router.get("/v1/debug/logs/{source}")
async def debug_logs(source: str, request: Request) -> StreamingResponse:
    require_auth(request)
    service = get_debug_log_service(source)
    log_type, target = validate_debug_log_service(service)
    if log_type == DEBUG_LOG_TYPE_FILE:
        stream = stream_file_lines(target, source)
    else:
        command, unavailable_message = debug_log_command(service)
        stream = stream_command_lines(command, unavailable_message, source)
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/v1/debug/log-services")
def debug_log_services(request: Request) -> dict[str, Any]:
    require_auth(request)
    return {
        "services": [
            service
            for service in normalize_debug_log_services()
            if service.get("enabled")
        ],
        "slots": DEBUG_LOG_SERVICE_SLOTS,
    }
