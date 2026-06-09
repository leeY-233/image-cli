import base64
import binascii
import hashlib
import hmac
import time
import uuid

from fastapi import HTTPException, Request

from .config import settings


def auth_enabled() -> bool:
    return bool(settings.app_password and settings.session_secret)


def sign_session(payload: str) -> str:
    return hmac.new(
        settings.session_secret.encode("utf-8"),
        payload.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def make_session_token() -> str:
    expires_at = int(time.time()) + settings.session_max_age_seconds
    nonce = uuid.uuid4().hex
    payload = f"{expires_at}:{nonce}"
    signature = sign_session(payload)
    token = f"{payload}:{signature}".encode("utf-8")
    return base64.urlsafe_b64encode(token).decode("utf-8")


def is_authenticated(request: Request) -> bool:
    if not auth_enabled():
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

    expected = sign_session(payload)
    return hmac.compare_digest(signature, expected)


def require_auth(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="Authentication required")


def admin_password() -> str:
    return settings.admin_password or settings.app_password


def require_admin(request: Request) -> None:
    require_auth(request)
    password = admin_password()
    if not password:
        raise HTTPException(
            status_code=503,
            detail="ADMIN_PASSWORD or APP_PASSWORD is required to use the admin panel",
        )
    supplied = request.headers.get("x-admin-password", "")
    if not hmac.compare_digest(supplied, password):
        raise HTTPException(status_code=401, detail="Admin password is incorrect")
