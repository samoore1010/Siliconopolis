"""OpenAI image generation (GPT Image) behind the ImageProvider interface.

Docs: https://platform.openai.com/docs/api-reference/images/create . Returns base64 PNG.
"""

from __future__ import annotations

import base64

import httpx

from pipeline.logging_setup import get_logger

from .base import GeneratedImage, ImageProvider

log = get_logger(__name__)
API = "https://api.openai.com/v1"

# Approximate USD per image (1024x1024) by model/quality; non-square sizes cost ~1.5x.
# TODO(owner): confirm against https://openai.com/api/pricing when prices change.
PRICES = {
    "gpt-image-1.5": {"low": 0.009, "medium": 0.034, "high": 0.133},
    "gpt-image-1-mini": {"low": 0.005, "medium": 0.011, "high": 0.036},
    "gpt-image-1": {"low": 0.011, "medium": 0.042, "high": 0.167},
}
SIZES = {"square": "1024x1024", "portrait": "1024x1536", "landscape": "1536x1024"}


def pick_size(size: tuple[int, int]) -> str:
    w, h = size
    if abs(w - h) / max(w, h) < 0.1:
        return SIZES["square"]
    return SIZES["portrait"] if h > w else SIZES["landscape"]


def estimate_cost(model: str, quality: str, size: str) -> float:
    table = PRICES.get(model) or PRICES["gpt-image-1.5"]
    base = table.get(quality, table["medium"])
    return round(base * (1.0 if size == SIZES["square"] else 1.5), 4)


class OpenAIImageProvider(ImageProvider):
    name = "openai"

    def __init__(self, *, api_key: str, model: str = "gpt-image-1.5", quality: str = "high", timeout: float = 180):
        if not api_key:
            raise RuntimeError("IMAGE_API_KEY (an OpenAI API key) is required for IMAGE_PROVIDER=openai")
        self.model, self.quality = model, quality
        self._http = httpx.Client(base_url=API, timeout=timeout,
                                  headers={"Authorization": f"Bearer {api_key}"})

    def generate(self, prompt, *, size=(1024, 1024), style=None, negative_prompt=None, quality: str | None = None,
                 transparent: bool = False) -> GeneratedImage:
        q = quality or self.quality
        api_size = pick_size(size)
        full = prompt if not style else f"{prompt}\nStyle: {style}"
        if negative_prompt:
            full += f"\nAvoid: {negative_prompt}."
        body = {"model": self.model, "prompt": full[:30000], "size": api_size, "quality": q, "n": 1}
        if transparent:  # artwork for printing on products
            body.update({"background": "transparent", "output_format": "png"})
        resp = self._http.post("/images/generations", json=body)
        if resp.status_code >= 400:
            raise RuntimeError(f"OpenAI image generation failed: {resp.status_code} {resp.text[:300]}")
        data = resp.json()["data"][0]
        png = base64.b64decode(data["b64_json"])
        w, h = (int(x) for x in api_size.split("x"))
        cost = estimate_cost(self.model, q, api_size)
        log.info("image generated", model=self.model, size=api_size, quality=q, cost_usd=cost)
        return GeneratedImage(data=png, media_type="image/png", width=w, height=h, cost_usd=cost, model=self.model,
                              metadata={"prompt": prompt, "revised_prompt": data.get("revised_prompt"), "quality": q})

    def edit(self, prompt, image, *, size=(1024, 1024), quality: str | None = None) -> GeneratedImage:
        """images/edits: keeps the input (our exact product mockup) and restages it as a photo."""
        q = quality or self.quality
        api_size = pick_size(size)
        data = {"model": self.model, "prompt": prompt[:30000], "size": api_size, "quality": q, "n": "1",
                "input_fidelity": "high"}
        files = {"image[]": ("mockup.png", image, "image/png")}
        resp = self._http.post("/images/edits", data=data, files=files)
        if resp.status_code == 400 and "input_fidelity" in resp.text:  # model without that option
            data.pop("input_fidelity")
            resp = self._http.post("/images/edits", data=data, files=files)
        if resp.status_code >= 400:
            raise RuntimeError(f"OpenAI image edit failed: {resp.status_code} {resp.text[:300]}")
        out = base64.b64decode(resp.json()["data"][0]["b64_json"])
        w, h = (int(x) for x in api_size.split("x"))
        cost = round(estimate_cost(self.model, q, api_size) * 1.3, 4)  # input image tokens on top
        log.info("image edited", model=self.model, size=api_size, quality=q, cost_usd=cost)
        return GeneratedImage(data=out, media_type="image/png", width=w, height=h, cost_usd=cost, model=self.model,
                              metadata={"prompt": prompt, "quality": q, "edit": True})

    def ping(self) -> None:
        resp = self._http.get(f"/models/{self.model}")
        if resp.status_code >= 400:
            raise RuntimeError(f"OpenAI ping failed: {resp.status_code} {resp.text[:200]}")
