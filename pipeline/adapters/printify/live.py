"""Live Printify client — Phase 2 stub.

Base: https://api.printify.com/v1 ; Auth: `Authorization: Bearer <token>`.
TODO(owner): verify endpoints/fields against https://developers.printify.com before Milestone 4.
"""

from __future__ import annotations

from typing import Any

import httpx

from .base import PrintifyClient

BASE_URL = "https://api.printify.com/v1"


class LivePrintifyClient(PrintifyClient):
    name = "live"

    def __init__(self, token: str, shop_id: str):
        if not token:
            raise RuntimeError("PRINTIFY_API_TOKEN is required for live mode")
        self.shop_id = shop_id
        self._http = httpx.Client(base_url=BASE_URL, headers={"Authorization": f"Bearer {token}"}, timeout=30)

    def list_shops(self) -> list[dict[str, Any]]:
        r = self._http.get("/shops.json")
        r.raise_for_status()
        return r.json()

    def list_blueprints(self) -> list[dict[str, Any]]:
        r = self._http.get("/catalog/blueprints.json")
        r.raise_for_status()
        return r.json()

    def list_print_providers(self, blueprint_id: int) -> list[dict[str, Any]]:
        r = self._http.get(f"/catalog/blueprints/{blueprint_id}/print_providers.json")
        r.raise_for_status()
        return r.json()

    def upload_image(self, filename: str, data: bytes) -> dict[str, Any]:
        raise NotImplementedError("TODO(Milestone 4): POST /uploads/images.json with base64 contents")

    def create_product(self, shop_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError("TODO(Milestone 4): POST /shops/{shop_id}/products.json")

    def publish_product(self, shop_id: str, product_id: str) -> dict[str, Any]:
        raise NotImplementedError("Publishing is the human step in v1.")

    def ping(self) -> None:
        self.list_shops()
