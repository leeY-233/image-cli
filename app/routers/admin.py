import json
import os
import shutil
import subprocess
import time
from typing import Any, Callable

import httpx
from dotenv import dotenv_values
from fastapi import APIRouter, HTTPException, Request

from ..auth import require_admin
from ..config import ENV_CONFIG_KEYS, SECRET_ENV_KEYS, settings
from ..debug_logs import (
    debug_log_services_env_value,
    list_docker_containers,
    normalize_debug_log_services,
)
from ..env_file import env_path, write_env_updates
from ..providers import (
    load_provider_configs,
    provider_api_type_from_modes,
    provider_model_ids,
    provider_models_url,
    provider_public,
    safe_provider_api_type,
    safe_provider_edit_mode,
    safe_provider_generate_mode,
    safe_provider_id,
)


ProviderErrorDetail = Callable[[httpx.Response], Any]
LogEvent = Callable[..., None]


def create_admin_router(
    *,
    elapsed_ms: Callable[[float], int],
    log_event: LogEvent,
    provider_error_detail: ProviderErrorDetail,
) -> APIRouter:
    router = APIRouter()

    def env_public_value(key: str, values: dict[str, Any]) -> dict[str, Any]:
        value = values.get(key)
        if value is None or str(value).strip() == "":
            runtime_defaults = {
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
            value = runtime_defaults.get(key, os.getenv(key, ""))
        value = str(value or "")
        if key in SECRET_ENV_KEYS:
            return {"value": "", "configured": bool(value), "secret": True}
        return {"value": value, "configured": bool(value), "secret": False}

    def admin_config_payload() -> dict[str, Any]:
        env_values = dict(dotenv_values(env_path()))
        raw_providers = str(env_values.get("IMAGE_PROVIDERS") or settings.image_providers or "")
        raw_debug_log_services = env_values.get("DEBUG_LOG_SERVICES")
        if raw_debug_log_services is None:
            raw_debug_log_services = settings.debug_log_services
        return {
            "env_file": str(env_path()),
            "needs_restart": True,
            "providers": [
                provider_public(provider)
                for provider in load_provider_configs(raw_providers)
            ],
            "debug_log_services": normalize_debug_log_services(raw_debug_log_services),
            "config": {
                key: env_public_value(key, env_values)
                for key in sorted(ENV_CONFIG_KEYS)
            },
        }

    @router.get("/v1/admin/config")
    def admin_config(request: Request) -> dict[str, Any]:
        require_admin(request)
        return admin_config_payload()

    @router.post("/v1/admin/config")
    async def update_admin_config(request: Request) -> dict[str, Any]:
        require_admin(request)
        body = await request.json()
        raw_config = body.get("config", {})
        if not isinstance(raw_config, dict):
            raise HTTPException(status_code=400, detail="config must be an object")
        updates: dict[str, str] = {}
        for key, value in raw_config.items():
            key = str(key)
            if key not in ENV_CONFIG_KEYS:
                raise HTTPException(status_code=400, detail=f"{key} is not configurable")
            text_value = str(value)
            if key in SECRET_ENV_KEYS and text_value == "":
                continue
            updates[key] = text_value
        raw_providers = body.get("providers")
        if raw_providers is not None:
            if not isinstance(raw_providers, list):
                raise HTTPException(status_code=400, detail="providers must be a list")
            env_values = dict(dotenv_values(env_path()))
            existing_raw_providers = str(
                env_values.get("IMAGE_PROVIDERS") or settings.image_providers or ""
            )
            existing_by_id = {
                provider.id: provider
                for provider in load_provider_configs(existing_raw_providers)
            }
            provider_updates: list[dict[str, str]] = []
            for index, item in enumerate(raw_providers, start=1):
                if not isinstance(item, dict):
                    continue
                provider_id = safe_provider_id(item.get("id"), f"provider-{index}")
                name = str(item.get("name") or provider_id).strip()
                base_url = str(item.get("base_url") or "").strip()
                model = str(item.get("model") or "").strip()
                generate_model = str(
                    item.get("generate_model")
                    or item.get("generation_model")
                    or item.get("generate_image_model")
                    or model
                ).strip()
                edit_model = str(
                    item.get("edit_model")
                    or item.get("edit_image_model")
                    or model
                ).strip()
                legacy_model = model or (generate_model if generate_model == edit_model else "")
                note = str(item.get("note") or "").strip()
                api_key = str(item.get("api_key") or "").strip()
                legacy_api_type = safe_provider_api_type(
                    item.get("api_type") or item.get("type") or item.get("mode")
                )
                generate_mode = safe_provider_generate_mode(
                    item.get("generate_mode")
                    or item.get("generation_mode")
                    or item.get("generate_api_type")
                    or item.get("generate_type"),
                    legacy_api_type,
                )
                edit_mode = safe_provider_edit_mode(
                    item.get("edit_mode")
                    or item.get("edit_api_type")
                    or item.get("edit_type"),
                    legacy_api_type,
                )
                api_type = provider_api_type_from_modes(generate_mode, edit_mode)
                if not base_url:
                    continue
                if not api_key and provider_id in existing_by_id:
                    api_key = existing_by_id[provider_id].api_key
                provider_updates.append(
                    {
                        "id": provider_id,
                        "name": name,
                        "base_url": base_url,
                        "api_key": api_key,
                        "model": legacy_model,
                        "generate_model": generate_model,
                        "edit_model": edit_model,
                        "note": note,
                        "api_type": api_type,
                        "generate_mode": generate_mode,
                        "edit_mode": edit_mode,
                    }
                )
            if provider_updates:
                updates["IMAGE_PROVIDERS"] = json.dumps(provider_updates, ensure_ascii=False)
        raw_debug_log_services = body.get("debug_log_services")
        if raw_debug_log_services is not None:
            if not isinstance(raw_debug_log_services, list):
                raise HTTPException(
                    status_code=400, detail="debug_log_services must be a list"
                )
            updates["DEBUG_LOG_SERVICES"] = debug_log_services_env_value(
                raw_debug_log_services
            )
        if updates:
            write_env_updates(updates)
            if "IMAGE_PROVIDERS" in updates:
                settings.image_providers = updates["IMAGE_PROVIDERS"]
            if "DEBUG_LOG_SERVICES" in updates:
                settings.debug_log_services = updates["DEBUG_LOG_SERVICES"]
            log_event("admin_config_updated", keys=sorted(updates))
        return {"ok": True, "updated": sorted(updates), **admin_config_payload()}

    @router.post("/v1/admin/provider-models")
    async def admin_provider_models(request: Request) -> dict[str, Any]:
        require_admin(request)
        body = await request.json()
        if not isinstance(body, dict):
            raise HTTPException(status_code=400, detail="request body must be an object")

        provider_id = safe_provider_id(body.get("id"), "")
        base_url = str(body.get("base_url") or "").strip()
        api_key = str(body.get("api_key") or "").strip()
        existing_by_id = {provider.id: provider for provider in load_provider_configs()}
        if provider_id in existing_by_id:
            existing = existing_by_id[provider_id]
            base_url = base_url or existing.base_url
            api_key = api_key or existing.api_key

        if not base_url:
            raise HTTPException(status_code=400, detail="Provider Base URL is required")
        if not base_url.startswith(("http://", "https://")):
            raise HTTPException(
                status_code=400,
                detail="Provider Base URL must start with http:// or https://",
            )

        headers: dict[str, str] = {}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        provider_url = provider_models_url(base_url)
        started_at = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=min(settings.timeout_seconds, 30)) as client:
                response = await client.get(provider_url, headers=headers)
            response.raise_for_status()
            provider_json = response.json()
        except httpx.HTTPStatusError as exc:
            detail = provider_error_detail(exc.response)
            log_event(
                "admin_provider_models_http_error",
                provider_id=provider_id,
                status_code=exc.response.status_code,
                detail=detail,
                elapsed_ms=elapsed_ms(started_at),
            )
            raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
        except httpx.HTTPError as exc:
            detail = {
                "error": {
                    "type": exc.__class__.__name__,
                    "message": str(exc) or repr(exc),
                    "hint": "Provider /models request failed. Check Base URL and API Key.",
                },
                "elapsed_ms": elapsed_ms(started_at),
                "url": provider_url,
            }
            log_event("admin_provider_models_request_error", provider_id=provider_id, detail=detail)
            raise HTTPException(status_code=502, detail=detail) from exc
        except ValueError as exc:
            detail = {
                "error": {
                    "type": "NonJsonResponse",
                    "message": "Provider /models did not return JSON.",
                },
                "elapsed_ms": elapsed_ms(started_at),
                "url": provider_url,
            }
            log_event("admin_provider_models_non_json", provider_id=provider_id, detail=detail)
            raise HTTPException(status_code=502, detail=detail) from exc

        models = provider_model_ids(provider_json)
        log_event(
            "admin_provider_models_success",
            provider_id=provider_id,
            model_count=len(models),
            elapsed_ms=elapsed_ms(started_at),
        )
        return {"ok": True, "models": models, "model_count": len(models)}

    @router.get("/v1/admin/docker-containers")
    def admin_docker_containers(request: Request) -> dict[str, Any]:
        require_admin(request)
        return list_docker_containers()

    @router.post("/v1/admin/restart")
    def restart_service(request: Request) -> dict[str, Any]:
        require_admin(request)
        systemctl = shutil.which("systemctl")
        if not systemctl:
            raise HTTPException(
                status_code=501,
                detail="systemctl is not available. Restart the uvicorn process manually.",
            )
        unit = settings.systemd_unit.strip()
        if not unit or any(char in unit for char in " \t\n\r;&|`$<>"):
            raise HTTPException(status_code=400, detail="SYSTEMD_UNIT is invalid")
        subprocess.Popen(
            [systemctl, "restart", unit],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log_event("admin_restart_requested", unit=unit)
        return {"ok": True, "unit": unit, "message": "Restart command issued"}

    return router
