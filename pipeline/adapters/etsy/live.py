"""Live Etsy Open API v3 client.

Base URL: https://openapi.etsy.com/v3/application
Auth: `x-api-key: <ETSY_API_KEY>:<ETSY_SHARED_SECRET>` on every call (Etsy requires both); `Authorization: Bearer <access_token>` on shop calls.
Tokens live in the oauth_tokens table and are refreshed automatically (Etsy rotates refresh tokens).

TODO(owner): before go-live, verify every endpoint, field name and allowed value against the
current docs at https://developers.etsy.com/documentation/reference . Details marked TODO below.
"""

from __future__ import annotations

import threading
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from pipeline.config import Settings
from pipeline.logging_setup import get_logger

from . import tokens as token_store
from .base import (
    DraftListing,
    EtsyClient,
    ListingSearchResult,
    ListingStats,
    Receipt,
    TaxonomyNode,
    assert_draft_only,
)

log = get_logger(__name__)

API_BASE = "https://openapi.etsy.com/v3/application"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
AUTH_URL = "https://www.etsy.com/oauth/connect"
SCOPES = ["listings_r", "listings_w", "listings_d", "shops_r", "transactions_r"]


def manage_url(listing_id: str) -> str:
    """The Shop Manager edit page: works for drafts (the public URL does not) and for active listings."""
    return f"https://www.etsy.com/your/shops/me/tools/listings/{listing_id}"


class Throttle:
    """Simple token-bucket style client-side limiter (Etsy ~10 req/s, ~10k/day)."""

    def __init__(self, per_second: float):
        self.interval = 1.0 / max(per_second, 0.1)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            if now < self._next:
                time.sleep(self._next - now)
                now = time.monotonic()
            self._next = now + self.interval


class EtsyAPIError(RuntimeError):
    pass


class LiveEtsyClient(EtsyClient):
    name = "live"

    def __init__(self, *, api_key: str, shop_id: str, dry_run: bool, rate_limit_per_sec: float = 4.0,
                 seed_access: str = "", seed_refresh: str = "", shared_secret: str = ""):
        if not api_key:
            raise RuntimeError("ETSY_API_KEY is required for live mode")
        self.api_key = api_key  # keystring; also the OAuth client_id
        self.shared_secret = shared_secret
        self.shop_id = shop_id
        self.dry_run = dry_run
        self.throttle = Throttle(rate_limit_per_sec)
        self._http = httpx.Client(base_url=API_BASE, timeout=60)
        token_store.seed_tokens_from_env(seed_access, seed_refresh)

    @classmethod
    def from_settings(cls, s: Settings) -> LiveEtsyClient:
        return cls(api_key=s.etsy_api_key, shared_secret=s.etsy_shared_secret, shop_id=s.etsy_shop_id, dry_run=s.dry_run,
                   rate_limit_per_sec=s.etsy_rate_limit_per_sec,
                   seed_access=s.etsy_oauth_access_token, seed_refresh=s.etsy_oauth_refresh_token)

    # ------------------------------------------------------------------ auth
    def _access_token(self) -> str:
        row = token_store.load_tokens()
        if row is None:
            raise EtsyAPIError("no Etsy OAuth tokens stored; run `python scripts/etsy_oauth.py` first")
        if row.expires_at is None or row.expires_at <= datetime.now(UTC) + timedelta(minutes=2):
            row = self.refresh_tokens(row.refresh_token)
        return row.access_token

    def refresh_tokens(self, refresh_token: str):
        resp = httpx.post(TOKEN_URL, data={"grant_type": "refresh_token", "client_id": self.api_key,
                                           "refresh_token": refresh_token}, timeout=30)
        if resp.status_code >= 400:
            raise EtsyAPIError(f"token refresh failed: {resp.status_code} {resp.text[:200]}")
        data = resp.json()
        return token_store.save_tokens(data["access_token"], data["refresh_token"], data.get("expires_in"))

    # ------------------------------------------------------------------ http
    def _request(self, method: str, path: str, *, auth: bool = True, write: bool = False, **kwargs) -> Any:
        if write and self.dry_run:
            log.info("DRY_RUN: skipping Etsy write", method=method, path=path)
            return {"dry_run": True}
        headers = {"x-api-key": self.api_key_header}
        if auth:
            headers["Authorization"] = f"Bearer {self._access_token()}"
        backoff = 1.0
        for attempt in range(6):
            self.throttle.wait()
            resp = self._http.request(method, path, headers=headers, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = float(resp.headers.get("retry-after", backoff))
                log.warning("etsy throttled/5xx, retrying", status=resp.status_code, attempt=attempt, sleep=retry_after)
                time.sleep(min(retry_after, 60))
                backoff = min(backoff * 2, 30)
                continue
            if resp.status_code == 401 and auth and attempt == 0:
                row = token_store.load_tokens()
                if row:
                    self.refresh_tokens(row.refresh_token)
                    headers["Authorization"] = f"Bearer {self._access_token()}"
                    continue
            if resp.status_code >= 400:
                raise EtsyAPIError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
            return resp.json() if resp.content else {}
        raise EtsyAPIError(f"{method} {path}: gave up after retries")

    # ------------------------------------------------------------------ reads
    def search_listings(self, keywords: str, *, limit: int = 25) -> list[ListingSearchResult]:
        data = self._request("GET", "/listings/active", auth=False,
                             params={"keywords": keywords, "limit": min(limit, 100), "sort_on": "score"})
        out = []
        for item in data.get("results", []):
            price = item.get("price") or {}
            amount = float(price.get("amount", 0)) / float(price.get("divisor", 100) or 100)
            out.append(ListingSearchResult(
                listing_id=str(item["listing_id"]), title=item.get("title", ""), price_usd=amount,
                num_favorers=int(item.get("num_favorers", 0)), views=int(item.get("views", 0)),
                tags=list(item.get("tags", [])), taxonomy_id=item.get("taxonomy_id"), url=item.get("url")))
        return out

    def get_taxonomy_nodes(self) -> list[TaxonomyNode]:
        data = self._request("GET", "/seller-taxonomy/nodes", auth=False)

        def build(n: dict[str, Any], parent_path: str = "") -> TaxonomyNode:
            path = f"{parent_path} > {n['name']}" if parent_path else n["name"]
            return TaxonomyNode(id=int(n["id"]), name=n["name"], full_path=path,
                                children=[build(c, path) for c in n.get("children", [])])

        return [build(n) for n in data.get("results", [])]

    def get_shop_receipts(self, *, min_created: int | None = None, limit: int = 100) -> list[Receipt]:
        params: dict[str, Any] = {"limit": min(limit, 100)}
        if min_created:
            params["min_created"] = min_created
        data = self._request("GET", f"/shops/{self.shop_id}/receipts", params=params)
        out = []
        for r in data.get("results", []):
            total = r.get("grandtotal") or {}
            out.append(Receipt(
                receipt_id=str(r["receipt_id"]),
                created_at=datetime.fromtimestamp(r.get("created_timestamp", 0), tz=UTC).isoformat(),
                listing_ids=[str(t.get("listing_id")) for t in r.get("transactions", [])],
                total_usd=float(total.get("amount", 0)) / float(total.get("divisor", 100) or 100)))
        return out

    def get_listing_stats(self, listing_ids: list[str]) -> list[ListingStats]:
        out = []
        for lid in listing_ids:
            # TODO(owner): verify getListing response fields (views, num_favorers, state).
            item = self._request("GET", f"/listings/{lid}", auth=False)
            out.append(ListingStats(listing_id=str(lid), views=int(item.get("views", 0)),
                                    num_favorers=int(item.get("num_favorers", 0)), state=item.get("state", "")))
        return out

    # ------------------------------------------------------------------ writes (draft only)
    def create_draft_listing(self, payload: dict[str, Any]) -> DraftListing:
        assert_draft_only(payload)
        payload = {**payload, "state": "draft"}
        # TODO(owner): confirm required fields: quantity, title, description, price, who_made,
        # when_made, taxonomy_id, type ("download" for digital), is_digital... per current docs.
        data = self._request("POST", f"/shops/{self.shop_id}/listings", write=True, data=payload)
        if data.get("dry_run"):
            return DraftListing(listing_id="dry-run", state="draft", title=payload.get("title", ""), raw=data)
        # Etsy's `url` is the public listing page, which says "unavailable" for a draft; link to Shop Manager.
        return DraftListing(listing_id=str(data["listing_id"]), state=data.get("state", "draft"),
                            title=data.get("title", ""), url=manage_url(str(data["listing_id"])), raw=data)

    def upload_listing_image(self, listing_id: str, data: bytes, filename: str, rank: int = 1) -> dict[str, Any]:
        return self._request("POST", f"/shops/{self.shop_id}/listings/{listing_id}/images", write=True,
                             files={"image": (filename, data, "image/png")}, data={"rank": rank})

    def upload_listing_file(self, listing_id: str, data: bytes, filename: str, name: str) -> dict[str, Any]:
        return self._request("POST", f"/shops/{self.shop_id}/listings/{listing_id}/files", write=True,
                             files={"file": (filename, data)}, data={"name": name})

    def update_listing(self, listing_id: str, payload: dict[str, Any]) -> DraftListing:
        assert_draft_only(payload)
        # TODO(owner): v3 updateListing uses PATCH; confirm content-type (form-encoded) and fields.
        data = self._request("PATCH", f"/shops/{self.shop_id}/listings/{listing_id}", write=True, data=payload)
        return DraftListing(listing_id=str(listing_id), state=data.get("state", "draft"), title=data.get("title", ""), raw=data)

    # ------------------------------------------------------------------ orders (in-house products)
    def get_open_orders(self, *, min_created: int | None = None) -> list:
        from .base import OrderLine

        params: dict[str, Any] = {"was_paid": "true", "was_shipped": "false", "was_canceled": "false", "limit": 100}
        if min_created:
            params["min_created"] = min_created
        data = self._request("GET", f"/shops/{self.shop_id}/receipts", params=params)
        out = []
        for r in data.get("results", []):
            ship_to = {k: str(r.get(k) or "") for k in ("name", "first_line", "second_line", "city", "state", "zip",
                                                         "country_iso")}
            created = datetime.fromtimestamp(r.get("created_timestamp", 0), tz=UTC).isoformat()
            for t in r.get("transactions", []):
                variations, personalization = {}, ""
                for v in t.get("variations") or []:
                    name, value = str(v.get("formatted_name") or ""), str(v.get("formatted_value") or "")
                    if name.lower().startswith("personali"):
                        personalization = value
                    else:
                        variations[name] = value
                out.append(OrderLine(receipt_id=str(r["receipt_id"]), transaction_id=str(t["transaction_id"]),
                                     listing_id=str(t.get("listing_id")), title=str(t.get("title") or ""),
                                     quantity=int(t.get("quantity") or 1), variations=variations,
                                     personalization=personalization, ship_to=ship_to, created_at=created,
                                     buyer_note=str(r.get("message_from_buyer") or "")))
        return out

    def update_listing_inventory(self, listing_id: str, inventory: dict[str, Any]) -> dict[str, Any]:
        # TODO(owner): verify against Etsy's updateListingInventory docs (custom properties 513/514).
        return self._request("PUT", f"/listings/{listing_id}/inventory", write=True, json=inventory)

    def create_receipt_shipment(self, receipt_id: str, *, carrier: str, tracking_code: str) -> dict[str, Any]:
        return self._request("POST", f"/shops/{self.shop_id}/receipts/{receipt_id}/tracking", write=True,
                             data={"tracking_code": tracking_code, "carrier_name": carrier})

    @property
    def api_key_header(self) -> str:
        # Etsy Open API v3: "x-api-key: <keystring>:<shared_secret>"
        return f"{self.api_key}:{self.shared_secret}" if self.shared_secret else self.api_key

    def is_connected(self) -> bool:
        if not (self.api_key and self.shop_id):
            return False
        try:
            return token_store.load_tokens() is not None
        except Exception:  # noqa: BLE001 - treat DB problems as "not connected" for gating
            return False

    def ping(self) -> None:
        # /openapi-ping needs only the API key
        self._request("GET", "/openapi-ping", auth=False)
        if self.shop_id:
            self._request("GET", f"/shops/{self.shop_id}", auth=False)
