"""Provider-agnostic image generation interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from pipeline.config import get_settings


@dataclass
class GeneratedImage:
    data: bytes
    media_type: str
    width: int
    height: int
    cost_usd: float
    model: str
    metadata: dict[str, Any] = field(default_factory=dict)


class ImageProvider(ABC):
    name = "base"

    @abstractmethod
    def generate(self, prompt: str, *, size: tuple[int, int] = (1024, 1024), style: str | None = None,
                 negative_prompt: str | None = None, quality: str | None = None,
                 transparent: bool = False) -> GeneratedImage: ...

    def edit(self, prompt: str, image: bytes, *, size: tuple[int, int] = (1024, 1024),
             quality: str | None = None) -> GeneratedImage:
        """Turn an input image into a new one (e.g. a flat product mockup into a lifestyle photo)."""
        raise NotImplementedError(f"{self.name} does not support image edits")

    @property
    def is_real(self) -> bool:
        return self.name != "mock"

    @abstractmethod
    def ping(self) -> None: ...


@lru_cache(maxsize=1)
def get_image_provider() -> ImageProvider:
    settings = get_settings()
    provider = settings.image_provider.lower()
    if provider in ("", "mock"):
        from .mock import MockImageProvider

        return MockImageProvider()
    if provider == "openai":
        from .openai import OpenAIImageProvider

        return OpenAIImageProvider(api_key=settings.image_api_key, model=settings.image_model,
                                   edit_model=settings.image_edit_model or settings.image_model,
                                   quality=settings.image_quality)
    raise NotImplementedError(
        f"IMAGE_PROVIDER={provider!r} is not implemented yet. Supported: mock, openai. "
        "Add a class in pipeline/adapters/images/ and register it here."
    )


def reset_image_provider_cache() -> None:
    get_image_provider.cache_clear()
