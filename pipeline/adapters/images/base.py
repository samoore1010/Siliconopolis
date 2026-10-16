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
                 negative_prompt: str | None = None) -> GeneratedImage: ...

    @abstractmethod
    def ping(self) -> None: ...


@lru_cache(maxsize=1)
def get_image_provider() -> ImageProvider:
    settings = get_settings()
    provider = settings.image_provider.lower()
    if provider in ("", "mock"):
        from .mock import MockImageProvider

        return MockImageProvider()
    # TODO(Milestone 3): add OpenAI / Google / Ideogram implementations behind this interface.
    raise NotImplementedError(
        f"IMAGE_PROVIDER={provider!r} is not implemented yet. Supported: mock. "
        "Add a class in pipeline/adapters/images/ and register it here."
    )


def reset_image_provider_cache() -> None:
    get_image_provider.cache_clear()
