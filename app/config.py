import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field


load_dotenv(override=True)


def env_int(name: str, default: int, minimum: int = 1) -> int:
    value = os.getenv(name, "")
    if str(value).strip() == "":
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, parsed)


def env_str(name: str, default: str) -> str:
    value = os.getenv(name, "")
    return str(value).strip() or default


class Settings(BaseModel):
    api_key: str = Field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    base_url: str = Field(
        default_factory=lambda: env_str("OPENAI_BASE_URL", "https://api.openai.com/v1")
    )
    model: str = Field(default_factory=lambda: env_str("IMAGE_MODEL", "gpt-image-2"))
    image_size: str = Field(default_factory=lambda: env_str("IMAGE_SIZE", "1024x1024"))
    image_quality: str = Field(default_factory=lambda: env_str("IMAGE_QUALITY", "auto"))
    image_output_format: str = Field(
        default_factory=lambda: env_str("IMAGE_OUTPUT_FORMAT", "png")
    )
    response_format: str = Field(
        default_factory=lambda: env_str("IMAGE_RESPONSE_FORMAT", "b64_json")
    )
    edit_image_field: str = Field(
        default_factory=lambda: env_str("IMAGE_EDIT_IMAGE_FIELD", "image[]")
    )
    image_providers: str = Field(default_factory=lambda: os.getenv("IMAGE_PROVIDERS", ""))
    output_dir: Path = Field(
        default_factory=lambda: Path(env_str("OUTPUT_DIR", "outputs"))
    )
    log_dir: Path = Field(default_factory=lambda: Path(env_str("LOG_DIR", "logs")))
    timeout_seconds: float = Field(
        default_factory=lambda: float(os.getenv("REQUEST_TIMEOUT_SECONDS") or "180")
    )
    provider_max_attempts: int = Field(
        default_factory=lambda: env_int("PROVIDER_MAX_ATTEMPTS", 2)
    )
    history_limit: int = Field(
        default_factory=lambda: env_int("HISTORY_LIMIT", 50)
    )
    max_active_jobs: int = Field(
        default_factory=lambda: env_int("MAX_ACTIVE_JOBS", 10)
    )
    trash_retention_days: int = Field(
        default_factory=lambda: env_int("TRASH_RETENTION_DAYS", 3)
    )
    app_password: str = Field(default_factory=lambda: os.getenv("APP_PASSWORD", ""))
    admin_password: str = Field(default_factory=lambda: os.getenv("ADMIN_PASSWORD", ""))
    session_secret: str = Field(
        default_factory=lambda: os.getenv("APP_SESSION_SECRET", "")
    )
    systemd_unit: str = Field(default_factory=lambda: env_str("SYSTEMD_UNIT", "image-cli"))
    debug_log_services: str = Field(default_factory=lambda: os.getenv("DEBUG_LOG_SERVICES", ""))
    session_max_age_seconds: int = 60 * 60 * 24 * 7

    @property
    def images_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/images/generations"

    @property
    def image_edits_url(self) -> str:
        return f"{self.base_url.rstrip('/')}/images/edits"


ENV_CONFIG_KEYS = {
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "IMAGE_MODEL",
    "IMAGE_SIZE",
    "IMAGE_QUALITY",
    "IMAGE_OUTPUT_FORMAT",
    "IMAGE_RESPONSE_FORMAT",
    "IMAGE_EDIT_IMAGE_FIELD",
    "IMAGE_PROVIDERS",
    "OUTPUT_DIR",
    "LOG_DIR",
    "REQUEST_TIMEOUT_SECONDS",
    "PROVIDER_MAX_ATTEMPTS",
    "HISTORY_LIMIT",
    "MAX_ACTIVE_JOBS",
    "TRASH_RETENTION_DAYS",
    "APP_PASSWORD",
    "ADMIN_PASSWORD",
    "APP_SESSION_SECRET",
    "SYSTEMD_UNIT",
    "DEBUG_LOG_SERVICES",
}
SECRET_ENV_KEYS = {
    "OPENAI_API_KEY",
    "APP_PASSWORD",
    "ADMIN_PASSWORD",
    "APP_SESSION_SECRET",
    "IMAGE_PROVIDERS",
}


settings = Settings()
settings.output_dir.mkdir(parents=True, exist_ok=True)
settings.log_dir.mkdir(parents=True, exist_ok=True)
APP_ROOT = Path(__file__).resolve().parent.parent


def runtime_defaults() -> dict[str, Any]:
    return {
        "OPENAI_BASE_URL": settings.base_url,
        "IMAGE_PROVIDERS": settings.image_providers,
        "IMAGE_MODEL": settings.model,
        "IMAGE_SIZE": settings.image_size,
        "IMAGE_QUALITY": settings.image_quality,
        "IMAGE_OUTPUT_FORMAT": settings.image_output_format,
        "IMAGE_RESPONSE_FORMAT": settings.response_format,
        "IMAGE_EDIT_IMAGE_FIELD": settings.edit_image_field,
        "OUTPUT_DIR": str(settings.output_dir),
        "LOG_DIR": str(settings.log_dir),
        "REQUEST_TIMEOUT_SECONDS": str(int(settings.timeout_seconds)),
        "PROVIDER_MAX_ATTEMPTS": str(settings.provider_max_attempts),
        "HISTORY_LIMIT": str(settings.history_limit),
        "MAX_ACTIVE_JOBS": str(settings.max_active_jobs),
        "TRASH_RETENTION_DAYS": str(settings.trash_retention_days),
        "SYSTEMD_UNIT": settings.systemd_unit,
        "DEBUG_LOG_SERVICES": settings.debug_log_services,
    }
