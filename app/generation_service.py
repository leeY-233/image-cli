import asyncio
import base64
import binascii
import json
import re
import time
import uuid
from typing import Any

import httpx
from fastapi import HTTPException

from .config import settings
from .constants import (
    CHAT_COMPLETION_IMAGE_OMITTED_KEYS,
    DATA_URL_IMAGE_RE,
    DEFAULT_GALLERY_ID,
    HTTP_URL_RE,
    MARKDOWN_IMAGE_RE,
    PROVIDER_EDIT_MODE_COMPLETIONS,
    PROVIDER_GENERATE_MODE_COMPLETIONS,
)
from .edit_uploads import (
    _cleanup_edit_job_files,
    _read_persisted_edit_file,
    _save_uploaded_source_copy,
)
from .image_files import (
    generate_history_thumbnail as _generate_history_thumbnail,
    history_file_metadata as _history_file_metadata,
)
from .library_service import (
    _append_history,
    _get_job,
    _resolve_gallery_id,
    _update_job,
)
from .providers import (
    get_provider as _get_provider,
    provider_payload as _provider_payload,
    provider_url as _provider_url,
)
from .schemas import GeneratedImage, GenerateImageRequest, GenerateImageResponse
from .telemetry import (
    elapsed_ms as _elapsed_ms,
    log_event as _log_event,
    provider_error_detail as _provider_error_detail,
    redact_large_payloads as _redact_large_payloads,
)
from .validators import (
    clamp_image_count as _clamp_image_count,
    safe_int as _safe_int,
    validate_size_budget as _validate_size_budget,
)


JOB_TASKS: dict[str, asyncio.Task] = {}


def _payload_from_request(request: GenerateImageRequest) -> dict[str, Any]:
    size = request.size or settings.image_size
    _validate_size_budget(size)
    payload: dict[str, Any] = {
        "model": request.model or settings.model,
        "provider_id": request.provider_id or "",
        "gallery_id": _resolve_gallery_id(request.gallery_id),
        "prompt": request.prompt,
        "size": size,
        "quality": request.quality or settings.image_quality,
        "output_format": request.output_format or settings.image_output_format,
        "n": request.n,
        "response_format": request.response_format or settings.response_format,
    }
    payload.update(request.extra)
    payload["n"] = _clamp_image_count(payload.get("n"))
    return payload

def _parse_extra_json(extra: str | None) -> dict[str, Any]:
    if not extra:
        return {}
    try:
        data = json.loads(extra)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="extra must be valid JSON") from exc
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="extra must be a JSON object")
    return data

def _payload_from_edit_form(
    prompt: str,
    provider_id: str | None,
    gallery_id: str | None,
    model: str | None,
    size: str | None,
    quality: str | None,
    output_format: str | None,
    n: int,
    response_format: str | None,
    extra: str | None,
) -> dict[str, Any]:
    if not prompt.strip():
        raise HTTPException(status_code=400, detail="prompt is required")
    if len(prompt) > 8000:
        raise HTTPException(status_code=400, detail="prompt must be 8000 characters or fewer")
    if output_format and output_format not in {"png", "jpeg", "webp"}:
        raise HTTPException(status_code=400, detail="output_format must be png, jpeg, or webp")
    if response_format and response_format not in {"b64_json", "url"}:
        raise HTTPException(status_code=400, detail="response_format must be b64_json or url")

    image_size = size or settings.image_size
    _validate_size_budget(image_size)
    payload: dict[str, Any] = {
        "model": model or settings.model,
        "provider_id": provider_id or "",
        "gallery_id": _resolve_gallery_id(gallery_id),
        "prompt": prompt,
        "size": image_size,
        "quality": quality or settings.image_quality,
        "output_format": output_format or settings.image_output_format,
        "n": _clamp_image_count(n),
        "response_format": response_format or settings.response_format,
    }
    payload.update(_parse_extra_json(extra))
    payload["n"] = _clamp_image_count(payload.get("n"))
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
            "model": payload.get("model"),
            "size": payload.get("size"),
            "quality": payload.get("quality"),
            "n": payload.get("n"),
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
    gallery_id = str(payload.get("gallery_id") or DEFAULT_GALLERY_ID)
    history_records = []
    for image in images:
        if not (image.file or image.url):
            continue
        history_id = uuid.uuid4().hex
        metadata = _history_file_metadata(str(image.file or ""))
        thumbnail_data = _generate_history_thumbnail(history_id, str(image.file or ""), _log_event)
        history_records.append(
            {
                "id": history_id,
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
                "provider_id": payload.get("provider_id", ""),
                "provider_name": provider_json.get("provider_name", ""),
                "gallery_id": gallery_id,
                "operation": operation,
                "source_file": source_file,
                "file_size_bytes": metadata["file_size_bytes"],
                "image_width": metadata["image_width"],
                "image_height": metadata["image_height"],
                "image_dimensions": metadata["image_dimensions"],
                **thumbnail_data,
                "created_at": now,
            }
        )
    _append_history(history_records)

def _save_failure_to_history(
    job_id: str,
    job: dict[str, Any],
    error: Any,
    status_code: int,
) -> None:
    payload = dict(job.get("payload") or {})
    edit_inputs = job.get("edit_inputs") if isinstance(job.get("edit_inputs"), dict) else {}
    try:
        provider_name = _get_provider(str(payload.get("provider_id") or "")).name
    except HTTPException:
        provider_name = ""
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
                "provider_id": payload.get("provider_id", ""),
                "provider_name": provider_name,
                "gallery_id": str(payload.get("gallery_id") or DEFAULT_GALLERY_ID),
                "operation": job.get("operation") or "generate",
                "source_file": edit_inputs.get("source_file"),
                "source_history_id": edit_inputs.get("source_history_id"),
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
                file_size_bytes=image.file_size_bytes,
                image_width=image.image_width,
                image_height=image.image_height,
                image_dimensions=image.image_dimensions,
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

def _extract_provider_error_message(
    provider_responses: list[dict[str, Any]],
) -> str:
    for response in provider_responses:
        if not isinstance(response, dict):
            continue
        for field in ("error", "warning", "moderation", "detail", "message"):
            value = response.get(field)
            if not value:
                continue
            if isinstance(value, dict):
                message = value.get("message") or value.get("detail") or ""
                if message:
                    return str(message)
                return json.dumps(value, ensure_ascii=False)[:400]
            if isinstance(value, str):
                return value
            return json.dumps(value, ensure_ascii=False, default=str)[:400]
    return ""

def _provider_no_images_detail(
    payload: dict[str, Any],
    provider_responses: list[dict[str, Any]],
    operation: str,
) -> dict[str, Any]:
    inferred = _extract_provider_error_message(provider_responses)
    summaries = [_provider_response_summary(response) for response in provider_responses]
    return {
        "error": {
            "type": "ProviderNoImagesReturned",
            "message": (
                inferred
                or (
                    "Provider 返回 0 张图片。可能被内容审核拦截、触发安全策略、配额耗尽，"
                    "或上游 provider 临时故障。请调整 prompt 或换一个 provider 后重试。"
                )
            ),
            "hint": (
                "上游 provider 在重试后仍未返回任何图片。常见原因：内容审核拒绝、安全策略命中、"
                "上游配额或额度问题、provider 临时不稳定。可以换一个 provider 再试。"
            ),
        },
        "request": {
            "operation": operation,
            "model": payload.get("model"),
            "size": payload.get("size"),
            "quality": payload.get("quality"),
            "n": payload.get("n"),
        },
        "provider_request_count": len(provider_responses),
        "provider_response_summaries": summaries,
    }

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
    provider_url: str,
    headers: dict[str, str],
    provider_payload: dict[str, Any],
    request_id: str,
    started_at: float,
    provider_request_index: int,
) -> tuple[dict[str, Any], list[GeneratedImage]]:
    try:
        response = await _post_with_retry(
            client, provider_url, headers, provider_payload, request_id
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
            exc, provider_payload, provider_url, started_at
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

def _chat_completion_payload(provider_payload: dict[str, Any]) -> dict[str, Any]:
    chat_payload = {
        key: value
        for key, value in provider_payload.items()
        if key not in CHAT_COMPLETION_IMAGE_OMITTED_KEYS and value is not None
    }
    messages = chat_payload.get("messages")
    if not isinstance(messages, list) or not messages:
        chat_payload["messages"] = [
            {"role": "user", "content": str(provider_payload.get("prompt") or "")}
        ]
    chat_payload["stream"] = False
    return chat_payload

def _chat_completion_image_part(
    image_data: dict[str, str | bytes],
) -> dict[str, Any] | None:
    content = image_data.get("content")
    if not isinstance(content, bytes):
        return None
    content_type = str(image_data.get("content_type") or "image/png")
    b64_image = base64.b64encode(content).decode("ascii")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{content_type};base64,{b64_image}"},
    }

def _chat_completion_edit_messages(
    prompt: str,
    source_images: list[dict[str, str | bytes]],
    mask_image: dict[str, str | bytes] | None,
) -> list[dict[str, Any]]:
    instruction = (
        f"{prompt.strip()}\n\n"
        "Edit the attached source image(s) according to the instruction. "
        "Use the image attachments as the visual source/reference."
    )
    if mask_image is not None:
        instruction += " A mask image is attached after the source image(s); use it as the edit mask if supported."

    content: list[dict[str, Any]] = [{"type": "text", "text": instruction}]
    for source_image in source_images:
        image_part = _chat_completion_image_part(source_image)
        if image_part is not None:
            content.append(image_part)
    if mask_image is not None:
        content.append({"type": "text", "text": "Mask image:"})
        mask_part = _chat_completion_image_part(mask_image)
        if mask_part is not None:
            content.append(mask_part)

    return [{"role": "user", "content": content}]

def _image_format_from_mime_label(format_name: str) -> str:
    normalized = format_name.strip().lower()
    if normalized in {"jpg", "jpeg"}:
        return "jpeg"
    if normalized == "webp":
        return "webp"
    return "png"

def _clean_image_url(url: Any) -> str:
    cleaned = str(url or "").strip().strip("<>").strip()
    while cleaned.endswith((".", ",", ";")):
        cleaned = cleaned[:-1]
    return cleaned

def _looks_like_raw_image_url(url: str) -> bool:
    lower_url = url.lower()
    path = lower_url.split("?", 1)[0].split("#", 1)[0]
    return path.endswith((".png", ".jpg", ".jpeg", ".webp")) or "image" in lower_url or "img" in lower_url

def _looks_like_base64_payload(value: str) -> bool:
    text = value.strip()
    return len(text) > 80 and re.fullmatch(r"[A-Za-z0-9+/_=-]+", text) is not None

def _append_chat_image_item(
    items: list[dict[str, Any]],
    seen: set[tuple[str, str]],
    item: dict[str, Any],
) -> None:
    url = _clean_image_url(item.get("url"))
    b64_json = item.get("b64_json")
    if isinstance(b64_json, str) and b64_json.strip():
        key = ("b64", b64_json[:120])
        if key in seen:
            return
        seen.add(key)
        cleaned = {
            "b64_json": b64_json.strip(),
            "revised_prompt": item.get("revised_prompt"),
        }
        if item.get("output_format"):
            cleaned["output_format"] = item.get("output_format")
        items.append(cleaned)
        return
    if url:
        key = ("url", url)
        if key in seen:
            return
        seen.add(key)
        items.append({"url": url, "revised_prompt": item.get("revised_prompt")})

def _append_chat_image_items_from_text(
    items: list[dict[str, Any]],
    seen: set[tuple[str, str]],
    text: str,
) -> None:
    if not text:
        return
    for match in DATA_URL_IMAGE_RE.finditer(text):
        _append_chat_image_item(
            items,
            seen,
            {
                "b64_json": match.group("data"),
                "output_format": _image_format_from_mime_label(match.group("format")),
            },
        )

    text_without_data_urls = DATA_URL_IMAGE_RE.sub("", text)
    for match in MARKDOWN_IMAGE_RE.finditer(text_without_data_urls):
        url = _clean_image_url(match.group("url"))
        if url:
            _append_chat_image_item(items, seen, {"url": url})

    for match in HTTP_URL_RE.finditer(text_without_data_urls):
        url = _clean_image_url(match.group(0))
        if url and _looks_like_raw_image_url(url):
            _append_chat_image_item(items, seen, {"url": url})

def _append_chat_image_value(
    items: list[dict[str, Any]],
    seen: set[tuple[str, str]],
    value: Any,
) -> None:
    if value is None:
        return
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("http://", "https://")):
            _append_chat_image_item(items, seen, {"url": text})
        else:
            _append_chat_image_items_from_text(items, seen, text)
        return
    if isinstance(value, list):
        for item in value:
            _append_chat_image_value(items, seen, item)
        return
    if not isinstance(value, dict):
        return

    if value.get("b64_json") or value.get("base64"):
        _append_chat_image_item(
            items,
            seen,
            {
                "b64_json": value.get("b64_json") or value.get("base64"),
                "output_format": value.get("output_format") or value.get("format"),
                "revised_prompt": value.get("revised_prompt"),
            },
        )
    for key in ("image_url", "url", "file_url"):
        if key in value:
            _append_chat_image_value(items, seen, value.get(key))
    if "image" in value:
        image_value = value.get("image")
        if isinstance(image_value, str) and _looks_like_base64_payload(image_value):
            _append_chat_image_item(
                items,
                seen,
                {
                    "b64_json": image_value,
                    "output_format": value.get("output_format") or value.get("format"),
                    "revised_prompt": value.get("revised_prompt"),
                },
            )
        else:
            _append_chat_image_value(items, seen, image_value)
    for key in ("content", "text", "data"):
        if key in value:
            _append_chat_image_value(items, seen, value.get(key))

def _chat_completion_response_to_image_response(
    provider_json: dict[str, Any],
    provider_payload: dict[str, Any],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for key in ("data", "images", "image"):
        value = provider_json.get(key)
        if value is not None:
            _append_chat_image_value(items, seen, value)

    choices = provider_json.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") or choice.get("delta")
            if isinstance(message, dict):
                for key in ("images", "image", "image_url", "content"):
                    if key in message:
                        _append_chat_image_value(items, seen, message.get(key))
            for key in ("images", "image", "image_url", "content", "text"):
                if key in choice:
                    _append_chat_image_value(items, seen, choice.get(key))

    normalized = provider_json.copy()
    normalized["data"] = items
    normalized["chat_completion_response"] = True
    normalized.setdefault("output_format", provider_payload.get("output_format") or "png")
    return normalized

async def _request_provider_chat_completion_images(
    client: httpx.AsyncClient,
    provider_url: str,
    headers: dict[str, str],
    provider_payload: dict[str, Any],
    request_id: str,
    started_at: float,
    provider_request_index: int,
) -> tuple[dict[str, Any], list[GeneratedImage]]:
    chat_payload = _chat_completion_payload(provider_payload)
    try:
        response = await _post_with_retry(
            client, provider_url, headers, chat_payload, request_id
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = _provider_error_detail(exc.response)
        _log_event(
            "generate_chat_completion_provider_http_error",
            request_id=request_id,
            provider_request_index=provider_request_index,
            status_code=exc.response.status_code,
            detail=detail,
            elapsed_ms=_elapsed_ms(started_at),
        )
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    except httpx.HTTPError as exc:
        detail = _provider_request_error_detail(
            exc, provider_payload, provider_url, started_at
        )
        _log_event(
            "generate_chat_completion_provider_request_error",
            request_id=request_id,
            provider_request_index=provider_request_index,
            detail=detail,
        )
        raise HTTPException(status_code=502, detail=detail) from exc

    try:
        raw_provider_json = response.json()
    except ValueError as exc:
        detail = _provider_non_json_detail(response, started_at)
        _log_event(
            "generate_chat_completion_provider_non_json",
            request_id=request_id,
            provider_request_index=provider_request_index,
            detail=detail,
        )
        raise HTTPException(status_code=502, detail=detail) from exc

    _log_event(
        "generate_chat_completion_provider_response",
        request_id=request_id,
        provider_request_index=provider_request_index,
        status_code=response.status_code,
        provider_response=_redact_large_payloads(raw_provider_json),
        elapsed_ms=_elapsed_ms(started_at),
    )

    provider_json = _chat_completion_response_to_image_response(
        raw_provider_json,
        provider_payload,
    )
    try:
        images = await _normalize_images(
            provider_json,
            str(provider_payload.get("output_format") or "png"),
        )
    except HTTPException as exc:
        _log_event(
            "generate_chat_completion_normalize_error",
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
                "hint": "Provider returned chat completion JSON, but local image extraction or saving failed.",
            },
            "elapsed_ms": _elapsed_ms(started_at),
        }
        _log_event(
            "generate_chat_completion_normalize_unexpected_error",
            request_id=request_id,
            provider_request_index=provider_request_index,
            detail=detail,
            provider_response=_redact_large_payloads(provider_json),
        )
        raise HTTPException(status_code=502, detail=detail) from exc

    return provider_json, images

def _multipart_file_summary(
    files: list[tuple[str, tuple[str, bytes, str]]]
) -> list[dict[str, Any]]:
    return [
        {
            "field": field,
            "filename": file_tuple[0],
            "content_type": file_tuple[2],
            "bytes": len(file_tuple[1]),
        }
        for field, file_tuple in files
    ]

async def _post_multipart_with_retry(
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    data: dict[str, Any],
    files: list[tuple[str, tuple[str, bytes, str]]],
    request_id: str,
) -> httpx.Response:
    last_error: httpx.HTTPError | None = None
    last_response: httpx.Response | None = None
    for attempt in range(settings.provider_max_attempts):
        try:
            _log_event(
                "provider_edit_request_attempt",
                request_id=request_id,
                attempt=attempt + 1,
                max_attempts=settings.provider_max_attempts,
                url=url,
                payload=data,
                files=_multipart_file_summary(files),
            )
            response = await client.post(url, headers=headers, data=data, files=files)
            if (
                attempt < settings.provider_max_attempts - 1
                and _is_retryable_provider_response(response)
            ):
                last_response = response
                delay = _retry_delay_seconds(attempt)
                _log_event(
                    "provider_edit_request_retryable_status",
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
                "provider_edit_request_attempt_failed",
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
        raise RuntimeError("Provider edit request failed without a captured exception")
    raise last_error

async def _request_provider_edit_images(
    client: httpx.AsyncClient,
    provider_url: str,
    headers: dict[str, str],
    provider_payload: dict[str, Any],
    files: list[tuple[str, tuple[str, bytes, str]]],
    request_id: str,
    started_at: float,
) -> tuple[dict[str, Any], list[GeneratedImage]]:
    try:
        response = await _post_multipart_with_retry(
            client,
            provider_url,
            headers,
            provider_payload,
            files,
            request_id,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        detail = _provider_error_detail(exc.response)
        _log_event(
            "edit_provider_http_error",
            request_id=request_id,
            status_code=exc.response.status_code,
            detail=detail,
            elapsed_ms=_elapsed_ms(started_at),
        )
        raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
    except httpx.HTTPError as exc:
        detail = _provider_request_error_detail(
            exc, provider_payload, provider_url, started_at
        )
        _log_event("edit_provider_request_error", request_id=request_id, detail=detail)
        raise HTTPException(status_code=502, detail=detail) from exc

    try:
        provider_json = response.json()
    except ValueError as exc:
        detail = _provider_non_json_detail(response, started_at)
        _log_event("edit_provider_non_json", request_id=request_id, detail=detail)
        raise HTTPException(status_code=502, detail=detail) from exc

    _log_event(
        "edit_provider_response",
        request_id=request_id,
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
            "edit_normalize_error",
            request_id=request_id,
            detail=exc.detail,
            provider_response=_redact_large_payloads(provider_json),
            elapsed_ms=_elapsed_ms(started_at),
        )
        raise

    return provider_json, images

async def _execute_generation(
    request_id: str,
    payload: dict[str, Any],
    prompt: str,
    started_at: float,
    client_host: str | None = None,
) -> GenerateImageResponse:
    provider = _get_provider(str(payload.get("provider_id") or ""))
    if not provider.api_key:
        _log_event(
            "generate_config_error",
            request_id=request_id,
            provider_id=provider.id,
            detail=f"Provider '{provider.name}' API key is not configured",
        )
        raise HTTPException(status_code=500, detail=f"Provider '{provider.name}' API key is not configured")

    headers = {
        "Authorization": f"Bearer {provider.api_key}",
        "Content-Type": "application/json",
    }
    provider_payload = _provider_payload(payload)
    provider_model = provider.generate_model or provider.model
    if provider_model:
        provider_payload["model"] = provider_model
        payload = {**payload, "model": provider_model}
    uses_chat_completions = provider.generate_mode == PROVIDER_GENERATE_MODE_COMPLETIONS
    provider_url = _provider_url(
        provider,
        "chat/completions" if uses_chat_completions else "images/generations",
    )
    request_provider_images = (
        _request_provider_chat_completion_images
        if uses_chat_completions
        else _request_provider_images
    )

    _log_event(
        "generate_start",
        request_id=request_id,
        provider_id=provider.id,
        provider_name=provider.name,
        provider_api_type=provider.api_type,
        provider_generate_mode=provider.generate_mode,
        provider_generate_model=provider_model,
        provider_url=provider_url,
        payload=payload,
        client=client_host,
    )

    requested_n = max(1, _safe_int(payload.get("n"), 1))
    provider_responses: list[dict[str, Any]] = []
    images: list[GeneratedImage] = []

    async with httpx.AsyncClient(timeout=settings.timeout_seconds) as client:
        provider_json, provider_images = await request_provider_images(
            client,
            provider_url,
            headers,
            provider_payload,
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
            single_payload = provider_payload.copy()
            single_payload["n"] = 1
            provider_json, provider_images = await request_provider_images(
                client,
                provider_url,
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

    usable_images = [image for image in images if image.file or image.url]
    if not usable_images:
        detail = _provider_no_images_detail(payload, provider_responses, "generate")
        _log_event(
            "generate_no_images_returned",
            request_id=request_id,
            provider_id=provider.id,
            provider_name=provider.name,
            elapsed_ms=_elapsed_ms(started_at),
            detail=detail,
        )
        raise HTTPException(status_code=502, detail=detail)
    images = usable_images

    provider_json = _combine_provider_responses(provider_responses, provider_payload, len(images))
    provider_json["provider_id"] = provider.id
    provider_json["provider_name"] = provider.name
    provider_json["provider_generate_mode"] = provider.generate_mode
    provider_json["provider_edit_mode"] = provider.edit_mode
    provider_json["provider_generate_model"] = provider.generate_model
    provider_json["provider_edit_model"] = provider.edit_model

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

async def _execute_edit(
    request_id: str,
    payload: dict[str, Any],
    source_images: list[dict[str, str | bytes]],
    mask_image: dict[str, str | bytes] | None,
    started_at: float,
    client_host: str | None = None,
    source_file: str | None = None,
) -> GenerateImageResponse:
    provider = _get_provider(str(payload.get("provider_id") or ""))
    if not provider.api_key:
        _log_event(
            "edit_config_error",
            request_id=request_id,
            provider_id=provider.id,
            detail=f"Provider '{provider.name}' API key is not configured",
        )
        raise HTTPException(status_code=500, detail=f"Provider '{provider.name}' API key is not configured")

    headers = {"Authorization": f"Bearer {provider.api_key}"}
    provider_payload = _provider_payload(payload)
    provider_model = provider.edit_model or provider.model
    if provider_model:
        provider_payload["model"] = provider_model
        payload = {**payload, "model": provider_model}
    uses_chat_completions = provider.edit_mode == PROVIDER_EDIT_MODE_COMPLETIONS
    provider_url = _provider_url(
        provider,
        "chat/completions" if uses_chat_completions else "images/edits",
    )
    form_payload = {key: str(value) for key, value in provider_payload.items() if value is not None}
    files: list[tuple[str, tuple[str, bytes, str]]] = []
    for source_image in source_images:
        content = source_image.get("content")
        if not isinstance(content, bytes):
            continue
        files.append(
            (
                settings.edit_image_field,
                (
                    str(source_image.get("filename") or "image.png"),
                    content,
                    str(source_image.get("content_type") or "image/png"),
                ),
            )
        )
    if mask_image is not None:
        mask_content = mask_image.get("content")
        if isinstance(mask_content, bytes):
            files.append(
                (
                    "mask",
                    (
                        str(mask_image.get("filename") or "mask.png"),
                        mask_content,
                        str(mask_image.get("content_type") or "image/png"),
                    ),
                )
            )

    _log_event(
        "edit_start",
        request_id=request_id,
        provider_id=provider.id,
        provider_name=provider.name,
        provider_api_type=provider.api_type,
        provider_edit_mode=provider.edit_mode,
        provider_edit_model=provider_model,
        provider_url=provider_url,
        payload=payload,
        files=_multipart_file_summary(files),
        client=client_host,
    )

    async with httpx.AsyncClient(timeout=settings.timeout_seconds) as client:
        if uses_chat_completions:
            chat_payload = provider_payload.copy()
            chat_payload["messages"] = _chat_completion_edit_messages(
                str(payload["prompt"]),
                source_images,
                mask_image,
            )
            provider_json, images = await _request_provider_chat_completion_images(
                client,
                provider_url,
                {**headers, "Content-Type": "application/json"},
                chat_payload,
                request_id,
                started_at,
                provider_request_index=1,
            )
        else:
            provider_json, images = await _request_provider_edit_images(
                client,
                provider_url,
                headers,
                form_payload,
                files,
                request_id,
                started_at,
            )
    provider_json["provider_id"] = provider.id
    provider_json["provider_name"] = provider.name
    provider_json["provider_generate_mode"] = provider.generate_mode
    provider_json["provider_edit_mode"] = provider.edit_mode
    provider_json["provider_generate_model"] = provider.generate_model
    provider_json["provider_edit_model"] = provider.edit_model

    usable_images = [image for image in images if image.file or image.url]
    if not usable_images:
        detail = _provider_no_images_detail(payload, [provider_json], "edit")
        _log_event(
            "edit_no_images_returned",
            request_id=request_id,
            provider_id=provider.id,
            provider_name=provider.name,
            elapsed_ms=_elapsed_ms(started_at),
            detail=detail,
        )
        raise HTTPException(status_code=502, detail=detail)
    images = usable_images

    source_file = source_file or (
        _save_uploaded_source_copy(source_images[0]) if source_images else None
    )
    _save_images_to_history(
        images,
        provider_json,
        payload,
        str(payload["prompt"]),
        "edit",
        source_file=source_file,
    )

    _log_event(
        "edit_success",
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
    operation = str(job.get("operation") or "generate")
    succeeded = False
    _update_job(job_id, status="running", started_at=int(time.time()))
    _log_event(
        "job_running",
        job_id=job_id,
        request_id=job_id,
        operation=operation,
        payload=job.get("payload"),
    )
    try:
        if operation == "edit":
            edit_inputs = job.get("edit_inputs")
            if not isinstance(edit_inputs, dict):
                raise HTTPException(status_code=500, detail="Edit job is missing upload inputs")
            raw_source_files = edit_inputs.get("source_files")
            if not isinstance(raw_source_files, list) or not raw_source_files:
                raise HTTPException(status_code=500, detail="Edit job has no source images")
            source_files = [
                item for item in raw_source_files if isinstance(item, dict)
            ]
            source_images = [
                _read_persisted_edit_file(source_file) for source_file in source_files
            ]
            raw_mask_file = edit_inputs.get("mask_file")
            mask_image = (
                _read_persisted_edit_file(raw_mask_file)
                if isinstance(raw_mask_file, dict)
                else None
            )
            result = await _execute_edit(
                job_id,
                dict(job["payload"]),
                source_images,
                mask_image,
                started_at,
                None,
                source_file=str(source_files[0].get("file") or "") if source_files else None,
            )
        else:
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
        succeeded = True
        _update_job(
            job_id,
            status="succeeded",
            result=result.model_dump(),
            finished_at=int(time.time()),
            elapsed_ms=_elapsed_ms(started_at),
        )
        _log_event("job_succeeded", job_id=job_id, request_id=job_id, operation=operation)
    finally:
        if operation == "edit":
            _cleanup_edit_job_files(job, preserve_primary_source=succeeded)
        JOB_TASKS.pop(job_id, None)

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
            item_output_format = str(item.get("output_format") or output_format)
            if item.get("b64_json"):
                file_url = _save_b64_image(str(item["b64_json"]), index, item_output_format)
            elif item.get("url"):
                file_url = await _download_image(client, str(item["url"]), index)

            metadata = _history_file_metadata(str(file_url or ""))
            images.append(
                GeneratedImage(
                    index=index,
                    url=item.get("url"),
                    file=file_url,
                    revised_prompt=item.get("revised_prompt"),
                    file_size_bytes=metadata["file_size_bytes"],
                    image_width=metadata["image_width"],
                    image_height=metadata["image_height"],
                    image_dimensions=str(metadata["image_dimensions"] or ""),
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
