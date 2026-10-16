"""Etsy Open API v3 adapter interface + factory."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from pipeline.config import get_settings

from .guard import DraftOnlyViolation, assert_draft_only  # noqa: F401


@dataclass
class ListingSearchResult:
    listing_id: str
    title: str
    price_usd: float
    num_favorers: int
    views: int
    tags: list[str] = field(default_factory=list)
    taxonomy_id: int | None = None
    url: str | None = None


@dataclass
class TaxonomyNode:
    id: int
    name: str
    full_path: str
    children: list[TaxonomyNode] = field(default_factory=list)


@dataclass
class DraftListing:
    listing_id: str
    state: str
    title: str
    url: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class Receipt:
    receipt_id: str
    created_at: str
    listing_ids: list[str]
    total_usd: float


@dataclass
class ListingStats:
    listing_id: str
    views: int
    num_favorers: int
    state: str


class EtsyClient(ABC):
    name = "base"

    # --- reads
    @abstractmethod
    def search_listings(self, keywords: str, *, limit: int = 25) -> list[ListingSearchResult]: ...

    @abstractmethod
    def get_taxonomy_nodes(self) -> list[TaxonomyNode]: ...

    @abstractmethod
    def get_shop_receipts(self, *, min_created: int | None = None, limit: int = 100) -> list[Receipt]: ...

    @abstractmethod
    def get_listing_stats(self, listing_ids: list[str]) -> list[ListingStats]: ...

    # --- writes (draft only!)
    @abstractmethod
    def create_draft_listing(self, payload: dict[str, Any]) -> DraftListing: ...

    @abstractmethod
    def upload_listing_image(self, listing_id: str, data: bytes, filename: str, rank: int = 1) -> dict[str, Any]: ...

    @abstractmethod
    def upload_listing_file(self, listing_id: str, data: bytes, filename: str, name: str) -> dict[str, Any]: ...

    @abstractmethod
    def update_listing(self, listing_id: str, payload: dict[str, Any]) -> DraftListing: ...

    @abstractmethod
    def ping(self) -> None: ...

    def flatten_taxonomy(self) -> list[TaxonomyNode]:
        out: list[TaxonomyNode] = []

        def walk(nodes: list[TaxonomyNode]):
            for n in nodes:
                out.append(n)
                walk(n.children)

        walk(self.get_taxonomy_nodes())
        return out


@lru_cache(maxsize=1)
def get_etsy_client() -> EtsyClient:
    settings = get_settings()
    if settings.adapter_mode("etsy") == "live":
        from .live import LiveEtsyClient

        return LiveEtsyClient.from_settings(settings)
    from .mock import MockEtsyClient

    return MockEtsyClient()


def reset_etsy_cache() -> None:
    get_etsy_client.cache_clear()
