import json
import logging
import time
from logging.handlers import RotatingFileHandler

from .config import settings


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
