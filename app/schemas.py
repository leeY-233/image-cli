from typing import Any, Literal

from pydantic import BaseModel, Field

from .constants import (
    MAX_IMAGE_COUNT,
    PROVIDER_API_IMAGES,
    PROVIDER_EDIT_MODE_EDIT,
    PROVIDER_GENERATE_MODE_GENERATE,
)


class GenerateImageRequest(BaseModel):
    prompt: str = Field(..., min_length=1, max_length=8000)
    provider_id: str | None = None
    gallery_id: str | None = None
    model: str | None = None
    size: str | None = None
    quality: str | None = None
    output_format: Literal["png", "jpeg", "webp"] | None = None
    n: int = Field(default=1, ge=1, le=MAX_IMAGE_COUNT)
    response_format: Literal["b64_json", "url"] | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class GeneratedImage(BaseModel):
    index: int
    url: str | None = None
    file: str | None = None
    revised_prompt: str | None = None
    file_size_bytes: int | None = None
    image_width: int | None = None
    image_height: int | None = None
    image_dimensions: str = ""


class GenerateImageResponse(BaseModel):
    model: str
    images: list[GeneratedImage]
    provider_response: dict[str, Any]


class ProviderConfig(BaseModel):
    id: str
    name: str
    base_url: str
    api_key: str
    model: str = ""
    generate_model: str = ""
    edit_model: str = ""
    note: str = ""
    api_type: Literal["images", "chat_completions"] = PROVIDER_API_IMAGES
    generate_mode: Literal["generate", "completions"] = PROVIDER_GENERATE_MODE_GENERATE
    edit_mode: Literal["edit", "completions"] = PROVIDER_EDIT_MODE_EDIT
