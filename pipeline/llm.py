"""Thin LLM client around the Anthropic SDK with cost accounting, JSON extraction and a mock.

The mock is used automatically when no ANTHROPIC_API_KEY is present (CI, first local run) so the
whole pipeline can be exercised end to end. Set LLM_PROVIDER=anthropic|mock to force either.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, field
from typing import Any

from pipeline.config import Settings, get_settings
from pipeline.logging_setup import get_logger

log = get_logger(__name__)


class LLMError(RuntimeError):
    pass


@dataclass
class ImageInput:
    data: bytes
    media_type: str = "image/png"


@dataclass
class LLMResult:
    text: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    raw: Any = field(default=None, repr=False)

    def json(self) -> Any:
        return extract_json(self.text)


def extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of a model reply (handles ```json fences)."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidate = fenced.group(1) if fenced else text
    candidate = candidate.strip()
    try:
        return json.loads(candidate)
    except json.JSONDecodeError:
        pass
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = candidate.find(opener), candidate.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise LLMError(f"model reply did not contain valid JSON: {text[:200]!r}")


def estimate_cost(model: str, input_tokens: int, output_tokens: int, settings: Settings | None = None) -> float:
    settings = settings or get_settings()
    for prefix, (inp, out) in settings.pricing_table().items():
        if model.startswith(prefix):
            return input_tokens / 1_000_000 * inp + output_tokens / 1_000_000 * out
    return 0.0


class LLMClient:
    """Interface. `task` is a hint (e.g. "research_briefs") the mock uses to pick a canned reply."""

    provider = "base"

    def complete(
        self,
        *,
        model: str,
        system: str,
        user: str,
        task: str,
        max_tokens: int = 4096,
        images: list[ImageInput] | None = None,
        temperature: float = 0.7,
    ) -> LLMResult:
        raise NotImplementedError

    def ping(self) -> None:
        raise NotImplementedError


class AnthropicLLM(LLMClient):
    provider = "anthropic"

    def __init__(self, api_key: str):
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key, max_retries=3)

    def complete(self, *, model, system, user, task, max_tokens=4096, images=None, temperature=0.7) -> LLMResult:
        content: list[dict[str, Any]] = []
        for img in images or []:
            content.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": img.media_type,
                        "data": base64.b64encode(img.data).decode("ascii"),
                    },
                }
            )
        content.append({"type": "text", "text": user})
        resp = self._client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": content}],
        )
        text = "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")
        usage = resp.usage
        result = LLMResult(
            text=text,
            model=model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cost_usd=estimate_cost(model, usage.input_tokens, usage.output_tokens),
            raw=resp,
        )
        log.info("llm call", task=task, model=model, input_tokens=result.input_tokens,
                 output_tokens=result.output_tokens, cost_usd=round(result.cost_usd, 5))
        return result

    def ping(self) -> None:
        # cheapest possible round-trip
        self._client.messages.create(
            model=get_settings().model_cheap, max_tokens=5, messages=[{"role": "user", "content": "ping"}]
        )


class MockLLM(LLMClient):
    """Deterministic canned replies keyed by `task` so the full pipeline runs without an API key."""

    provider = "mock"

    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    def complete(self, *, model, system, user, task, max_tokens=4096, images=None, temperature=0.7) -> LLMResult:
        self.calls.append({"task": task, "model": model, "user": user[:500], "n_images": len(images or [])})
        payload = self._canned(task, user)
        text = json.dumps(payload, indent=2) if not isinstance(payload, str) else payload
        return LLMResult(text=text, model=f"mock:{model}", input_tokens=len(user) // 4, output_tokens=len(text) // 4, cost_usd=0.0)

    def ping(self) -> None:
        return None

    @staticmethod
    def _canned(task: str, user: str) -> Any:
        if task == "research_briefs":
            niche = _grab(user, r'"niche":\s*"([^"]+)"') or "minimalist botanical wall art"
            product_type = _grab(user, r'"product_types":\s*\[\s*"([^"]+)"') or "wall_art"
            n = int(_grab(user, r"Produce exactly (\d+)") or 2)
            allowed = (_grab(user, r"Allowed product_type values: ([^\n]+)") or product_type).replace(".", "").split(", ")
            motifs = ["eucalyptus stem", "single fern frond", "river stones", "quiet moon phases", "wheat sprig",
                      "folded paper crane", "soft arch shapes", "tide lines", "seed heads", "morning window light"]
            return {
                "briefs": [
                    {
                        "concept": f"Original {niche} concept {i + 1}: {motifs[i % len(motifs)]}, calm and giftable",
                        "product_type": allowed[i % len(allowed)].strip(),
                        "style": "soft neutral palette, generous whitespace, hand-drawn line quality",
                        "audience": "renters and first-apartment decorators aged 25-40",
                        "keywords": [niche.split()[0], "printable", "digital download", "minimalist", "home decor"],
                        "target_price_usd": 6.5,
                        "rationale": "Demand signal: many active listings with favorites in this niche; gap for a simpler variant.",
                        "original_elements": "Composition and motif are invented; no reference to existing sellers.",
                    }
                    for i in range(n)
                ]
            }
        if task == "creator_plan":
            product_type = _grab(user, r'"product_type":\s*"([^"]+)"') or "wall_art"
            concept = _grab(user, r'"concept":\s*"([^"]+)"') or "calm line botanical"
            slug = re.sub(r"[^a-z0-9]+", "-", concept.lower()).strip("-")[:40] or "product"
            short = " ".join(concept.split()[:6])
            return {
                "slug": slug,
                "title_hint": short.title(),
                "image_prompt": f"original minimalist composition for: {concept}; thin charcoal line on warm off-white paper, centered, generous negative space, no text",
                "negative_prompt": "text, watermark, logo, signature, brand, people, celebrities",
                "palette": ["#F7F3EC", "#2B2B2B", "#A3B18A"],
                "planner_spec": {
                    "title": short.title(),
                    "subtitle": "One page, one day, zero overwhelm",
                    "sections": ["Top 3 priorities", "Time blocks", "Brain dump", "Water + movement", "Wins today"],
                    "pages": 3,
                },
                "spreadsheet_spec": {
                    "title": short.title(),
                    "sheets": [
                        {"name": "Income", "columns": ["Date", "Client", "Description", "Amount", "Paid?"]},
                        {"name": "Expenses", "columns": ["Date", "Vendor", "Category", "Amount", "Receipt?"]},
                        {"name": "Summary", "columns": ["Month", "Income", "Expenses", "Profit"]},
                    ],
                },
                "product_type": product_type,
            }
        if task == "qa_vision":
            return {
                "pass": True,
                "scores": {"quality": 8, "legibility": 9, "artifacts": 9, "matches_brief": 8},
                "ip_concerns": [],
                "reasons": ["Mock QA: placeholder images render, no text artifacts, matches brief."],
            }
        if task == "lister_copy":
            return {
                "title": "Minimalist Botanical Line Art Print, Printable Wall Art, Neutral Home Decor Digital Download",
                "tags": [
                    "botanical print", "line art print", "minimalist wall art", "printable wall art",
                    "neutral decor", "digital download", "eucalyptus art", "bedroom wall art",
                    "living room decor", "boho print", "instant download", "modern art print", "gift for her",
                ],
                "description": (
                    "A calm, hand-drawn style botanical line print designed to bring quiet to any room. "
                    "You will receive high-resolution files sized for common frames.\n\nWhat's included:\n"
                    "- PNG and PDF at print resolution\n- Ratios for 8x10, 11x14, 16x20 and A-series\n\n"
                    "How it works: purchase, download instantly, print at home or at any print shop."
                ),
                "materials": ["digital file", "PNG", "PDF"],
                "taxonomy_hint": "wall decor > prints > digital prints",
            }
        if task == "analyst_summary":
            return {
                "summary": "Mock analyst: catalog is young; no sales yet. Keep volume low and vary niches.",
                "top_performers": [],
                "dead_listings": [],
                "guidance_for_researcher": (
                    "Prioritise planners/trackers with a clear single use-case; avoid generic quotes; "
                    "test one new niche per week."
                ),
            }
        if task == "classify":
            return {"label": "ok", "confidence": 0.9}
        return {"ok": True}


def _grab(text: str, pattern: str) -> str | None:
    m = re.search(pattern, text)
    return m.group(1) if m else None


_client: LLMClient | None = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        settings = get_settings()
        if settings.effective_llm_provider == "anthropic":
            _client = AnthropicLLM(settings.anthropic_api_key)
        else:
            _client = MockLLM()
        log.info("llm provider selected", provider=_client.provider)
    return _client


def reset_llm_cache() -> None:
    global _client
    _client = None
