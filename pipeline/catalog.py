"""In-house product catalog (catalog.yaml): what the print shop can make, in which colours/sizes."""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import yaml

from pipeline.config import get_settings


@dataclass
class CatalogItem:
    key: str
    product_type: str
    label: str
    blank: str
    method: str
    material: str
    sizes: list[str]
    colors: dict[str, str]
    print_area: dict[str, Any]
    unit_cost: float
    ship_cost: float
    price: float
    size_upcharge: dict[str, float] = field(default_factory=dict)
    mockup: dict[str, Any] = field(default_factory=dict)

    @property
    def print_px(self) -> tuple[int, int]:
        dpi = int(get_catalog().min_dpi)
        return round(float(self.print_area["width_in"]) * dpi), round(float(self.print_area["height_in"]) * dpi)

    def price_for(self, size: str | None) -> float:
        return round(float(self.price) + float(self.size_upcharge.get(size or "", 0)), 2)

    def summary(self) -> dict[str, Any]:
        return {"item": self.key, "product_type": self.product_type, "label": self.label, "method": self.method,
                "sizes": self.sizes, "colors": list(self.colors), "print_area_in": [self.print_area["width_in"],
                self.print_area["height_in"]], "price_usd": self.price,
                "est_margin_usd": round(self.price - self.unit_cost - self.ship_cost * 0.3, 2)}


@dataclass
class Catalog:
    items: dict[str, CatalogItem]
    partner: dict[str, Any]

    @property
    def min_dpi(self) -> int:
        return int(self.partner.get("min_dpi", 300))

    @property
    def processing_days(self) -> list[int]:
        return list(self.partner.get("processing_days") or [2, 5])

    def for_type(self, product_type: str) -> list[CatalogItem]:
        return [i for i in self.items.values() if i.product_type == product_type]

    def get(self, key: str | None) -> CatalogItem | None:
        return self.items.get(str(key or ""))

    def summary(self) -> list[dict[str, Any]]:
        return [i.summary() for i in self.items.values()]


@lru_cache(maxsize=1)
def get_catalog() -> Catalog:
    settings = get_settings()
    path = settings.resolve(settings.catalog_file)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else {}
    items = {}
    for key, raw in (data.get("items") or {}).items():
        items[key] = CatalogItem(
            key=key, product_type=raw["product_type"], label=raw.get("label", key), blank=raw.get("blank", ""),
            method=raw.get("method", ""), material=raw.get("material", ""), sizes=[str(s) for s in raw.get("sizes") or []],
            colors={str(k): str(v) for k, v in (raw.get("colors") or {}).items()}, print_area=dict(raw["print_area"]),
            unit_cost=float(raw.get("unit_cost", 0)), ship_cost=float(raw.get("ship_cost", 0)),
            price=float(raw.get("price", 0)), size_upcharge={str(k): float(v) for k, v in (raw.get("size_upcharge") or {}).items()},
            mockup=dict(raw.get("mockup") or {}))
    return Catalog(items=items, partner=dict(data.get("production_partner") or {}))


def reset_catalog_cache() -> None:
    get_catalog.cache_clear()
