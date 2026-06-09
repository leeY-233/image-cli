import hmac

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..auth import auth_enabled, is_authenticated, make_session_token
from ..config import settings
from ..constants import GALLERY_UNLOCK_COOKIE
from ..template_loader import read_template


router = APIRouter()


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "model": settings.model}


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if is_authenticated(request):
        return RedirectResponse("/", status_code=303)
    return HTMLResponse(read_template("login.html"))


@router.post("/login")
async def login(request: Request) -> JSONResponse:
    if not auth_enabled():
        return JSONResponse({"ok": True})

    body = await request.json()
    password = str(body.get("password", ""))
    if not hmac.compare_digest(password, settings.app_password):
        raise HTTPException(status_code=401, detail="密码不正确")

    response = JSONResponse({"ok": True})
    response.set_cookie(
        "image_cli_session",
        make_session_token(),
        max_age=settings.session_max_age_seconds,
        httponly=True,
        samesite="lax",
    )
    return response


@router.get("/logout")
def logout() -> RedirectResponse:
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie("image_cli_session")
    response.delete_cookie(GALLERY_UNLOCK_COOKIE)
    return response


@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(read_template("index.html"))


@router.get("/ui-preview", response_class=HTMLResponse)
def ui_preview_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(read_template("ui-preview.html"))


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    if not is_authenticated(request):
        return RedirectResponse("/login", status_code=303)
    return HTMLResponse(read_template("admin.html"))
