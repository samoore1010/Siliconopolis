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

    @property
    def truncated(self) -> bool:
        return getattr(self.raw, "stop_reason", None) == "max_tokens"

    def json(self) -> Any:
        try:
            return extract_json(self.text)
        except LLMError as e:
            if self.truncated:
                raise LLMError(f"model reply was cut off at the max_tokens limit, so its JSON is incomplete: {e}") from e
            raise


_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _loads(candidate: str) -> Any:
    """json.loads with the slips models make: raw newlines/tabs inside strings, trailing commas."""
    for attempt in (candidate, _TRAILING_COMMA_RE.sub(r"\1", candidate)):
        try:
            return json.loads(attempt, strict=False)
        except json.JSONDecodeError:
            continue
    raise json.JSONDecodeError("unparseable", candidate, 0)


def extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of a model reply: ```json fences, prose before/after,
    control characters inside strings and trailing commas are all tolerated."""
    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    candidates = [fenced.group(1).strip()] if fenced else []
    candidates.append(text.strip())
    for candidate in candidates:
        try:
            return _loads(candidate)
        except json.JSONDecodeError:
            pass
        for opener, closer in (("{", "}"), ("[", "]")):
            start, end = candidate.find(opener), candidate.rfind(closer)
            if start != -1 and end > start:
                try:
                    return _loads(candidate[start : end + 1])
                except json.JSONDecodeError:
                    continue
    log.warning("model reply was not valid JSON", reply=text[:3000])
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
        # `temperature` is accepted for interface compatibility but not sent: current Anthropic SDKs and
        # models no longer take sampling parameters on messages.create.
        # Streamed so large max_tokens values don't hit the SDK's non-streaming timeout guard.
        with self._client.messages.stream(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": content}],
        ) as stream:
            resp = stream.get_final_message()
        if getattr(resp, "stop_reason", None) == "max_tokens":
            log.warning("llm reply truncated at max_tokens", task=task, model=model, max_tokens=max_tokens)
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
                "market_analysis": {
                    "summary": f"Mock analysis: steady demand for {niche}; buyers favour simple, specific designs.",
                    "demand": "Consistent search interest year-round with a January bump.",
                    "competition": "Many listings, but most are generic bundles.",
                    "price_range": "$3-$9 for single products, $12-$25 for bundles",
                    "what_buyers_want": ["clean layouts", "instant download", "US Letter + A4"],
                    "gaps": ["few products aimed at a specific use-case"],
                    "data_basis": "Mock data (no Etsy search in tests).",
                },
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
                    "orientation": "portrait",
                    "fonts": {"heading": "Poppins", "body": "Inter"},
                    "style": "calm, airy, rounded boxes",
                    "pages": [
                        {"name": "Daily focus", "purpose": "plan one day",
                         "elements": ["large outlined focus box labelled 'Today's One Thing'",
                                      "3 checkbox lines labelled 'If there's time'", "ruled notes area"]},
                        {"name": "Weekly habits", "purpose": "track habits",
                         "elements": ["6 habit rows each with 7 circles labelled M T W T F S S"]},
                    ],
                },
                "spreadsheet_spec": _mock_sheet_spec(short.title()),
                "merch_spec": {
                    "item": "mug11" if product_type == "mug" else "tshirt",
                    "colors": ["White"] if product_type == "mug" else ["Black", "Navy", "Heather Gray", "Sage"],
                    "design_brief": "Family name in large serif, small 'EST.' year above, thin rule and motto below.",
                    "ink_colors": ["#F4E9D8", "#E8A04C"] if product_type != "mug" else ["#2B2B2B", "#C8743A"],
                    "illustration_prompt": None,
                    "personalization": {"enabled": True, "instructions": "Family name, then year",
                                        "fields": [{"key": "name", "label": "Family name", "max_chars": 14,
                                                    "example": "Henderson"},
                                                   {"key": "year", "label": "Year", "max_chars": 4, "example": "1987"}]},
                },
                "product_type": product_type,
            }
        if task == "creator_sheet_fix":
            return _mock_sheet_spec("Fixed Workbook")
        if task in ("creator_merch_html", "creator_merch_fix"):
            mug = "Ceramic Mug" in user
            ink, accent = ("#2B2B2B", "#C8743A") if mug else ("#F4E9D8", "#E8A04C")
            small, big, motto, width = (22, 84, 26, "40%") if mug else (34, 150, 44, "100%")
            return ("```html\n<html><body><div class='design' style='display:flex;flex-direction:column;"
                    "align-items:center;justify-content:" + ("center" if mug else "flex-start") + ";padding:.3in .3in 0;"
                    "font-family:\"DM Serif Display\",serif;color:" + ink + "'>"
                    f"<div style='font-family:Poppins,sans-serif;letter-spacing:.3em;font-size:{small}px;color:" + accent + "'>"
                    f"EST. {{{{year}}}}</div><div data-fit style='width:{width};text-align:center;font-size:{big}px;line-height:1.05'>"
                    "{{name}}</div><div style='width:30%;height:4px;background:" + accent + ";margin:12px 0'></div>"
                    f"<div style='font-family:Poppins,sans-serif;font-size:{motto}px;letter-spacing:.18em'>FAMILY REUNION</div>"
                    "</div></body></html>\n```")
        if task == "order_check":
            from pipeline.fulfillment import simple_parse

            fields = json.loads(_grab(user, r"Fields: (\[.*\])") or "[]")
            text = json.loads(_grab(user, r"Buyer's personalisation text: (\".*\")") or '""')
            values = simple_parse(text, fields)
            concerns = [f"{f.get('label')} is longer than {f.get('max_chars')} characters" for f in fields
                        if len(values.get(f["key"], "")) > int(f.get("max_chars") or 99)]
            return {"ok": not concerns, "values": values, "concerns": concerns}
        if task in ("creator_page_html", "creator_page_fix"):
            return ("```html\n<html><head><style>.page{padding:.6in;background:#F7F3EC;display:flex;flex-direction:column;gap:.3in}"
                    "h1{font-family:Poppins,sans-serif;margin:0}.box{flex:1;border:3px solid #A3B18A;border-radius:18px}"
                    ".row{display:flex;gap:.12in}.c{width:.32in;height:.32in;border:2px solid #2B2B2B;border-radius:50%}"
                    "footer{font-size:9pt;opacity:.6}</style></head><body>"
                    "<div class='page'><h1>Today's One Thing</h1><div class='box'></div><footer>Mock planner</footer></div>"
                    "<div class='page'><h1>Weekly Habits</h1><div class='row'>" + "<span class='c'></span>" * 7 +
                    "</div><footer>Mock planner</footer></div></body></html>\n```")
        if task in ("editor_pages", "editor_merch"):
            base = MockLLM._canned("creator_page_html" if task == "editor_pages" else "creator_merch_html", user)
            html = base.replace("Mock planner", "Polished planner").replace("FAMILY REUNION", "FAMILY REUNION 2026")
            meta = {"changes_made": ["Applied the owner's note", "Tightened spacing and hierarchy"],
                    "kept": ["Page structure and palette"], "title_hint": None, "palette": None}
            return "```json\n" + json.dumps(meta) + "\n```\n" + html
        if task == "editor_sheet":
            spec = _mock_sheet_spec("Polished Workbook")
            return {"changes_made": ["Renamed the workbook", "Added clearer instructions"], "kept": ["All tabs"],
                    "spreadsheet_spec": spec}
        if task == "editor_art":
            return {"changes_made": ["Warmed the palette"], "kept": ["Composition"],
                    "image_prompt": (_grab(user, r"Current image prompt: ([^\n]+)") or "original composition") + "; warmer tones",
                    "negative_prompt": "text, watermark, logo", "edit_prompt": "warm the palette, keep the composition",
                    "regenerate": False}
        if task == "qa_vision":
            return {
                "pass": True,
                "scores": {"quality": 8, "legibility": 9, "artifacts": 9, "matches_brief": 8,
                           "design_polish": 8, "commercial_viability": 8},
                "verdict": "Mock QA: would hold its own against typical listings at this price.",
                "improvements": [],
                "requested_changes": [{"change": c.strip()[:120], "done": True} for c in re.findall(
                    r"^- (.+)$", re.split(r"\n(?:The Editor declared|Previous version)", user.split("REQUESTED CHANGES", 1)[1])[0], re.M)
                    if not c.startswith("(none")]
                if "REQUESTED CHANGES" in user else [],
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


def _mock_sheet_spec(title: str) -> dict[str, Any]:
    return {
        "title": title, "subtitle": "Income, expenses and profit at a glance", "font": "Calibri",
        "instructions": ["Rename your categories on the Settings tab.", "Log sales on Income and costs on Expenses.",
                         "Check the Dashboard for totals and charts."],
        "sheets": [
            {"name": "Income", "purpose": "Every payment you receive", "rows": 100, "totals": True,
             "columns": [{"name": "Date", "type": "date"}, {"name": "Client", "type": "text"},
                         {"name": "Category", "type": "choice", "options": ["Services", "Products", "Other"]},
                         {"name": "Amount", "type": "currency"}, {"name": "Paid?", "type": "checkbox"}],
             "sample_rows": [["2026-01-05", "Maple Studio", "Services", 450, True],
                             ["2026-02-12", "Birch & Co", "Products", 1200, False]]},
            {"name": "Expenses", "purpose": "Every business cost",
             "columns": [{"name": "Date", "type": "date"}, {"name": "Vendor", "type": "text"},
                         {"name": "Category", "type": "choice", "options": ["Supplies", "Software", "Marketing"]},
                         {"name": "Amount", "type": "currency"}, {"name": "Receipt?", "type": "checkbox"}],
             "sample_rows": [["2026-01-03", "Paper Supply", "Supplies", 38.5, True]]},
            {"name": "Mileage Log", "purpose": "Business trips", "totals": True,
             "columns": [{"name": "Date", "type": "date"}, {"name": "Miles", "type": "number"},
                         {"name": "Rate", "type": "currency"},
                         {"name": "Deduction", "type": "formula", "formula": "=B{r}*C{r}", "format": "currency"}],
             "sample_rows": [["2026-01-09", 42, 0.7, 29.4]]},
        ],
        "dashboard": {"name": "Dashboard", "kpis": [
            {"label": "Income", "agg": "sum", "sheet": "Income", "column": "Amount"},
            {"label": "Expenses", "agg": "sum", "sheet": "Expenses", "column": "Amount"},
            {"label": "Profit", "agg": "difference", "of": ["Income", "Expenses"]}],
            "breakdowns": [{"title": "Income by month", "sheet": "Income", "group_by": "Date",
                            "value_column": "Amount", "agg": "sum", "chart": "column"}]},
    }


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
