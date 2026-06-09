import json
import time
from typing import Any

import httpx

from .constants import DATA_URL_IMAGE_RE
from .logging_config import logger


def elapsed_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)


def provider_error_detail(response: httpx.Response) -> Any:
    try:
        return response.json()
    except ValueError:
        return response.text


def redact_large_payloads(value: Any) -> Any:
    if isinstance(value, dict):
        redacted = {}
        for key, item in value.items():
            if key in {"b64_json", "base64"} and isinstance(item, str):
                redacted[key] = f"<redacted base64 image, {len(item)} chars>"
            else:
                redacted[key] = redact_large_payloads(item)
        return redacted
    if isinstance(value, list):
        return [redact_large_payloads(item) for item in value]
    if isinstance(value, str):
        return DATA_URL_IMAGE_RE.sub(
            lambda match: (
                f"data:image/{match.group('format')};base64,"
                f"<redacted image, {len(match.group('data'))} chars>"
            ),
            value,
        )
    return value


def log_event(event: str, **fields: Any) -> None:
    payload = {
        "ts": int(time.time()),
        "event": event,
        **fields,
    }
    logger.info(json.dumps(redact_large_payloads(payload), ensure_ascii=False, default=str))
