from fastapi import FastAPI

from .library_service import _mark_interrupted_jobs_on_startup
from .routers.admin import create_admin_router
from .routers.debug import router as debug_router
from .routers.jobs import router as jobs_router
from .routers.library import router as library_router
from .routers.pages import router as pages_router
from .telemetry import (
    elapsed_ms as _elapsed_ms,
    log_event as _log_event,
    provider_error_detail as _provider_error_detail,
)

app = FastAPI(title="Image CLI Web Service", version="0.1.0")
app.include_router(pages_router)
app.include_router(debug_router)
app.include_router(library_router)
app.include_router(jobs_router)


@app.on_event("startup")
def mark_interrupted_jobs() -> None:
    _mark_interrupted_jobs_on_startup()


app.include_router(
    create_admin_router(
        elapsed_ms=_elapsed_ms,
        log_event=_log_event,
        provider_error_detail=_provider_error_detail,
    )
)
