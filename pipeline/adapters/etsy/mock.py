"""Mock Etsy client: deterministic search results, in-memory shop persisted to storage as JSON."""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime
from typing import Any

from pipeline.storage import get_storage

from .base import (
    DraftListing,
    EtsyClient,
    ListingSearchResult,
    ListingStats,
    Receipt,
    TaxonomyNode,
    assert_draft_only,
)

STATE_KEY = "mock_etsy/shop_state.json"

MOCK_TAXONOMY = [
    TaxonomyNode(
        id=1, name="Art & Collectibles", full_path="Art & Collectibles",
        children=[
            TaxonomyNode(id=2078, name="Prints", full_path="Art & Collectibles > Prints",
                         children=[TaxonomyNode(id=2079, name="Digital Prints",
                                                full_path="Art & Collectibles > Prints > Digital Prints")]),
        ],
    ),
    TaxonomyNode(
        id=69, name="Paper & Party Supplies", full_path="Paper & Party Supplies",
        children=[
            TaxonomyNode(id=2390, name="Paper", full_path="Paper & Party Supplies > Paper",
                         children=[
                             TaxonomyNode(id=2391, name="Calendars & Planners",
                                          full_path="Paper & Party Supplies > Paper > Calendars & Planners"),
                             TaxonomyNode(id=2392, name="Stationery",
                                          full_path="Paper & Party Supplies > Paper > Stationery"),
                         ]),
        ],
    ),
    TaxonomyNode(
        id=11, name="Books, Movies & Music", full_path="Books, Movies & Music",
        children=[TaxonomyNode(id=1200, name="Digital Templates",
                               full_path="Books, Movies & Music > Digital Templates",
                               children=[TaxonomyNode(id=1201, name="Spreadsheets",
                                                      full_path="Books, Movies & Music > Digital Templates > Spreadsheets")])],
    ),
]


class MockEtsyClient(EtsyClient):
    name = "mock"

    def __init__(self):
        self.storage = get_storage()
        self._state = self._load()

    # ---------------------------------------------------------------- persistence
    def _load(self) -> dict[str, Any]:
        if self.storage.exists(STATE_KEY):
            try:
                return json.loads(self.storage.get(STATE_KEY))
            except Exception:
                pass
        return {"listings": {}, "images": {}, "files": {}, "receipts": [], "next_id": 100000001}

    def _save(self) -> None:
        self.storage.put(STATE_KEY, json.dumps(self._state, indent=2).encode(), "application/json")

    # ---------------------------------------------------------------- reads
    def search_listings(self, keywords: str, *, limit: int = 25) -> list[ListingSearchResult]:
        rng = random.Random(keywords)
        words = [w for w in keywords.lower().split() if len(w) > 2] or ["printable"]
        results = []
        for i in range(min(limit, 12)):
            price = round(rng.uniform(3.0, 14.0), 2)
            results.append(
                ListingSearchResult(
                    listing_id=str(900000000 + rng.randint(0, 9_999_999)),
                    title=f"{' '.join(w.title() for w in words[:3])} Printable {['Set', 'Bundle', 'Template', 'Print'][i % 4]} {i + 1}",
                    price_usd=price,
                    num_favorers=rng.randint(0, 900),
                    views=rng.randint(50, 20000),
                    tags=list(dict.fromkeys(words + ["digital download", "printable", "instant download"]))[:13],
                    taxonomy_id=2079,
                    url=None,
                )
            )
        return results

    def get_taxonomy_nodes(self) -> list[TaxonomyNode]:
        return MOCK_TAXONOMY

    def get_shop_receipts(self, *, min_created: int | None = None, limit: int = 100) -> list[Receipt]:
        return [Receipt(**r) for r in self._state["receipts"][:limit]]

    def get_listing_stats(self, listing_ids: list[str]) -> list[ListingStats]:
        out = []
        for lid in listing_ids:
            listing = self._state["listings"].get(str(lid))
            if listing:
                rng = random.Random(lid)
                out.append(ListingStats(listing_id=str(lid), views=rng.randint(0, 40),
                                        num_favorers=rng.randint(0, 5), state=listing["state"]))
        return out

    # ---------------------------------------------------------------- writes
    def create_draft_listing(self, payload: dict[str, Any]) -> DraftListing:
        assert_draft_only(payload)
        payload = {**payload, "state": "draft"}
        lid = str(self._state["next_id"])
        self._state["next_id"] += 1
        listing = {**payload, "listing_id": lid, "created_at": datetime.now(UTC).isoformat()}
        self._state["listings"][lid] = listing
        self._save()
        return DraftListing(listing_id=lid, state="draft", title=payload.get("title", ""),
                            url=f"https://www.etsy.com/your/shops/me/tools/listings/{lid}", raw=listing)

    def upload_listing_image(self, listing_id: str, data: bytes, filename: str, rank: int = 1) -> dict[str, Any]:
        self._require(listing_id)
        key = f"mock_etsy/{listing_id}/images/{rank:02d}_{filename}"
        uri = self.storage.put(key, data, "image/png")
        self._state["images"].setdefault(listing_id, []).append({"rank": rank, "uri": uri})
        self._save()
        return {"listing_image_id": f"{listing_id}-{rank}", "uri": uri}

    def upload_listing_file(self, listing_id: str, data: bytes, filename: str, name: str) -> dict[str, Any]:
        self._require(listing_id)
        key = f"mock_etsy/{listing_id}/files/{filename}"
        uri = self.storage.put(key, data)
        self._state["files"].setdefault(listing_id, []).append({"name": name, "uri": uri, "size": len(data)})
        self._save()
        return {"listing_file_id": f"{listing_id}-{len(self._state['files'][listing_id])}", "uri": uri}

    def update_listing(self, listing_id: str, payload: dict[str, Any]) -> DraftListing:
        assert_draft_only(payload)
        listing = self._require(listing_id)
        listing.update(payload)
        self._save()
        return DraftListing(listing_id=listing_id, state=listing["state"], title=listing.get("title", ""), raw=listing)

    def ping(self) -> None:
        return None

    def _require(self, listing_id: str) -> dict[str, Any]:
        listing = self._state["listings"].get(str(listing_id))
        if not listing:
            raise KeyError(f"mock etsy: unknown listing {listing_id}")
        return listing
