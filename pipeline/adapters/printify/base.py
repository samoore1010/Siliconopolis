"""Printify adapter interface (Phase 2). Mock is default; live is a stub with TODOs."""

from __future__ import annotations

from abc import ABC, abstractmethod
from functools import lru_cache
from typing import Any

from pipeline.config import get_settings


class PrintifyClient(ABC):
    name = "base"

    @abstractmethod
    def list_shops(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def list_blueprints(self) -> list[dict[str, Any]]: ...

    @abstractmethod
    def list_print_providers(self, blueprint_id: int) -> list[dict[str, Any]]: ...

    @abstractmethod
    def upload_image(self, filename: str, data: bytes) -> dict[str, Any]: ...

    @abstractmethod
    def create_product(self, shop_id: str, payload: dict[str, Any]) -> dict[str, Any]: ...

    @abstractmethod
    def publish_product(self, shop_id: str, product_id: str) -> dict[str, Any]:
        """Publishes to the linked Etsy shop. Phase 2 only; still gated by the human checkpoint."""

    @abstractmethod
    def ping(self) -> None: ...


@lru_cache(maxsize=1)
def get_printify_client() -> PrintifyClient:
    settings = get_settings()
    if settings.adapter_mode("printify") == "live":
        from .live import LivePrintifyClient

        return LivePrintifyClient(settings.printify_api_token, settings.printify_shop_id)
    from .mock import MockPrintifyClient

    return MockPrintifyClient()


def reset_printify_cache() -> None:
    get_printify_client.cache_clear()
