import json
from typing import Any, Literal

from fastapi import HTTPException

from .config import settings
from .constants import (
    PROVIDER_API_CHAT_COMPLETIONS,
    PROVIDER_API_IMAGES,
    PROVIDER_API_TYPE_ALIASES,
    PROVIDER_EDIT_MODE_ALIASES,
    PROVIDER_EDIT_MODE_COMPLETIONS,
    PROVIDER_EDIT_MODE_EDIT,
    PROVIDER_GENERATE_MODE_ALIASES,
    PROVIDER_GENERATE_MODE_COMPLETIONS,
    PROVIDER_GENERATE_MODE_GENERATE,
)
from .schemas import ProviderConfig


def provider_url(provider: ProviderConfig, path: str) -> str:
    return f"{provider.base_url.rstrip('/')}/{path.lstrip('/')}"


def provider_models_url(base_url: str) -> str:
    return f"{base_url.rstrip('/')}/models"


def default_provider_config() -> ProviderConfig:
    return ProviderConfig(
        id="default",
        name="Default",
        base_url=settings.base_url,
        api_key=settings.api_key,
        note="Default provider from OPENAI_BASE_URL / OPENAI_API_KEY",
        api_type=PROVIDER_API_IMAGES,
        generate_mode=PROVIDER_GENERATE_MODE_GENERATE,
        edit_mode=PROVIDER_EDIT_MODE_EDIT,
    )


def safe_provider_id(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    if not text:
        return fallback
    safe = "".join(char if char.isalnum() or char in "-_" else "-" for char in text)
    return safe.strip("-_") or fallback


def safe_provider_api_type(value: Any) -> Literal["images", "chat_completions"]:
    text = str(value or "").strip().lower().replace("-", "_")
    api_type = PROVIDER_API_TYPE_ALIASES.get(text, PROVIDER_API_IMAGES)
    if api_type == PROVIDER_API_CHAT_COMPLETIONS:
        return PROVIDER_API_CHAT_COMPLETIONS
    return PROVIDER_API_IMAGES


def safe_provider_generate_mode(
    value: Any,
    api_type: str = PROVIDER_API_IMAGES,
) -> Literal["generate", "completions"]:
    text = str(value or "").strip().lower().replace("-", "_")
    if not text:
        if api_type == PROVIDER_API_CHAT_COMPLETIONS:
            return PROVIDER_GENERATE_MODE_COMPLETIONS
        return PROVIDER_GENERATE_MODE_GENERATE
    mode = PROVIDER_GENERATE_MODE_ALIASES.get(text)
    if mode == PROVIDER_GENERATE_MODE_COMPLETIONS:
        return PROVIDER_GENERATE_MODE_COMPLETIONS
    if mode == PROVIDER_GENERATE_MODE_GENERATE:
        return PROVIDER_GENERATE_MODE_GENERATE
    if api_type == PROVIDER_API_CHAT_COMPLETIONS:
        return PROVIDER_GENERATE_MODE_COMPLETIONS
    return PROVIDER_GENERATE_MODE_GENERATE


def safe_provider_edit_mode(
    value: Any,
    api_type: str = PROVIDER_API_IMAGES,
) -> Literal["edit", "completions"]:
    text = str(value or "").strip().lower().replace("-", "_")
    if not text:
        if api_type == PROVIDER_API_CHAT_COMPLETIONS:
            return PROVIDER_EDIT_MODE_COMPLETIONS
        return PROVIDER_EDIT_MODE_EDIT
    mode = PROVIDER_EDIT_MODE_ALIASES.get(text)
    if mode == PROVIDER_EDIT_MODE_COMPLETIONS:
        return PROVIDER_EDIT_MODE_COMPLETIONS
    if mode == PROVIDER_EDIT_MODE_EDIT:
        return PROVIDER_EDIT_MODE_EDIT
    if api_type == PROVIDER_API_CHAT_COMPLETIONS:
        return PROVIDER_EDIT_MODE_COMPLETIONS
    return PROVIDER_EDIT_MODE_EDIT


def provider_api_type_from_modes(
    generate_mode: str,
    edit_mode: str,
) -> Literal["images", "chat_completions"]:
    if (
        generate_mode == PROVIDER_GENERATE_MODE_COMPLETIONS
        and edit_mode == PROVIDER_EDIT_MODE_COMPLETIONS
    ):
        return PROVIDER_API_CHAT_COMPLETIONS
    return PROVIDER_API_IMAGES


def load_provider_configs(raw: str | None = None) -> list[ProviderConfig]:
    raw = (settings.image_providers if raw is None else raw).strip()
    providers: list[ProviderConfig] = []
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = []
        if isinstance(data, list):
            for index, item in enumerate(data, start=1):
                if not isinstance(item, dict):
                    continue
                provider_id = safe_provider_id(item.get("id"), f"provider-{index}")
                name = str(item.get("name") or provider_id).strip()
                base_url = str(item.get("base_url") or "").strip()
                api_key = str(item.get("api_key") or "").strip()
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
                note = str(item.get("note") or "").strip()
                api_type = safe_provider_api_type(
                    item.get("api_type") or item.get("type") or item.get("mode")
                )
                generate_mode = safe_provider_generate_mode(
                    item.get("generate_mode")
                    or item.get("generation_mode")
                    or item.get("generate_api_type")
                    or item.get("generate_type"),
                    api_type,
                )
                edit_mode = safe_provider_edit_mode(
                    item.get("edit_mode")
                    or item.get("edit_api_type")
                    or item.get("edit_type"),
                    api_type,
                )
                api_type = provider_api_type_from_modes(generate_mode, edit_mode)
                if not base_url:
                    continue
                providers.append(
                    ProviderConfig(
                        id=provider_id,
                        name=name,
                        base_url=base_url,
                        api_key=api_key,
                        model=model,
                        generate_model=generate_model,
                        edit_model=edit_model,
                        note=note,
                        api_type=api_type,
                        generate_mode=generate_mode,
                        edit_mode=edit_mode,
                    )
                )
    if not providers:
        providers.append(default_provider_config())
    return providers


def provider_public(provider: ProviderConfig) -> dict[str, Any]:
    return {
        "id": provider.id,
        "name": provider.name,
        "base_url": provider.base_url,
        "model": provider.model,
        "generate_model": provider.generate_model,
        "edit_model": provider.edit_model,
        "note": provider.note,
        "api_type": provider.api_type,
        "generate_mode": provider.generate_mode,
        "edit_mode": provider.edit_mode,
        "api_key_configured": bool(provider.api_key),
    }


def provider_model_ids(provider_json: Any) -> list[str]:
    data = provider_json.get("data") if isinstance(provider_json, dict) else provider_json
    if not isinstance(data, list):
        return []
    models: list[str] = []
    seen: set[str] = set()
    for item in data:
        model_id = ""
        if isinstance(item, dict):
            model_id = str(item.get("id") or item.get("name") or "").strip()
        elif isinstance(item, str):
            model_id = item.strip()
        if model_id and model_id not in seen:
            models.append(model_id)
            seen.add(model_id)
    return models


def get_provider(provider_id: str | None) -> ProviderConfig:
    providers = load_provider_configs()
    wanted = str(provider_id or "").strip() or providers[0].id
    for provider in providers:
        if provider.id == wanted:
            return provider
    raise HTTPException(status_code=400, detail=f"Provider '{wanted}' is not configured")


def provider_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key not in {"provider_id", "gallery_id"}
    }
