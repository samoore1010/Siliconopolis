from __future__ import annotations

import hashlib
from typing import Any

from .base import PrintifyClient


class MockPrintifyClient(PrintifyClient):
    name = "mock"

    def __init__(self):
        self.products: dict[str, dict[str, Any]] = {}

    def list_shops(self):
        return [{"id": 1, "title": "Mock Etsy Shop", "sales_channel": "etsy"}]

    def list_blueprints(self):
        return [
            {"id": 282, "title": "Matte Vertical Posters"},
            {"id": 400, "title": "Kiss-Cut Stickers"},
            {"id": 68, "title": "Ceramic Mug 11oz"},
            {"id": 6, "title": "Unisex Heavy Cotton Tee"},
        ]

    def list_print_providers(self, blueprint_id: int):
        return [{"id": 99, "title": "Mock Print Provider"}]

    def upload_image(self, filename: str, data: bytes):
        return {"id": hashlib.sha1(data).hexdigest()[:24], "file_name": filename}

    def create_product(self, shop_id: str, payload: dict[str, Any]):
        pid = f"mockpod{len(self.products) + 1:06d}"
        self.products[pid] = {**payload, "id": pid, "visible": False}
        return self.products[pid]

    def publish_product(self, shop_id: str, product_id: str):
        raise NotImplementedError("Phase 2: publishing POD products is not enabled in v1 (human checkpoint).")

    def ping(self) -> None:
        return None
