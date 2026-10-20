"""Editor: editing -> created. A senior designer that polishes an existing product.

The owner sends a built, approved or rejected product here (never anything already on Etsy), with an
optional note; QA's improvement suggestions ride along. The Editor starts from the product's current
design (the page HTML, spreadsheet spec, print design or artwork), applies every requested change,
keeps what works, and raises the overall polish. It uses a stronger model than the Creator
(MODEL_EDITOR, Opus by default) with a per-product budget (EDITOR_MAX_COST_PER_PRODUCT_USD). The
result goes back through QA, which checks the requested changes were actually made.

It is a separate worker with its own queue (state=editing, ordered by `priority`) so polishes never
wait behind new builds, and it honours the owner's Stop button between items like the Creator.
"""

from __future__ import annotations

import io
import json
import re

from PIL import Image
from sqlalchemy import select
from sqlalchemy.orm import Session

from pipeline.adapters.images import get_image_provider
from pipeline.catalog import get_catalog
from pipeline.db.locking import claim_products, release, transition
from pipeline.db.models import OwnerNote, OwnerNoteKind, Product, ProductState, ProductType
from pipeline.guardrails import CapReached, get_blocklist
from pipeline.llm import ImageInput, LLMError, extract_json
from pipeline.storage import get_storage

from .creator import INTERNAL_KEYS, CreatorAgent

MAX_EDIT_ATTEMPTS = 2
# plan keys the Editor may change; everything else (slug, source paths...) is kept
EDITABLE_PLAN_KEYS = ("title_hint", "palette", "planner_spec", "spreadsheet_spec", "merch_spec",
                      "image_prompt", "negative_prompt")
EDIT_RECORD_KEYS = ("note", "improvements", "round", "requested_at", "previous_state", "previous_qa",
                    "previous_files", "previous_images")


class EditBudgetExceeded(RuntimeError):
    pass


def split_reply(text: str) -> tuple[dict, str | None]:
    """The Editor answers with a ```json block (what changed, plan changes) and, for HTML designs,
    a ```html block with the full revised document."""
    jm = re.search(r"```json\s*(.*?)```", text, re.DOTALL)
    hm = re.search(r"```html\s*(.*?)```", text, re.DOTALL)
    if jm:
        try:
            meta = json.loads(jm.group(1).strip())
        except json.JSONDecodeError as e:
            raise LLMError(f"editor reply had invalid JSON: {e}") from e
    elif hm:
        meta = {}
    else:
        meta = extract_json(text)
    if not isinstance(meta, dict):
        raise LLMError("editor reply JSON was not an object")
    return meta, (hm.group(1).strip() if hm else None)


def edit_request(brief: dict) -> dict:
    """The change request the Editor works from (pending `edit`, else the last one completed)."""
    return brief.get("edit") or (brief.get("edits_done") or [{}])[-1] or {}


def requested_changes(req: dict) -> list[str]:
    """Owner note + QA suggestions as one checklist, the way QA will verify it."""
    out = [str(req.get("note") or "").strip()] if req.get("note") else []
    return out + [str(i) for i in req.get("improvements") or [] if str(i).strip()]


def _shrink(png: bytes, long_side: int = 1200) -> bytes:
    img = Image.open(io.BytesIO(png)).convert("RGB")
    img.thumbnail((long_side, long_side))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()


class EditorAgent(CreatorAgent):
    name = "editor"
    prompt_file = "editor"

    DEFAULT_BATCH = 5  # polishes per run; each one is bounded by EDITOR_MAX_COST_PER_PRODUCT_USD

    def __init__(self, *a, batch_size: int = DEFAULT_BATCH, **kw):
        super().__init__(*a, batch_size=batch_size, **kw)
        self._budget_start = 0.0

    @property
    def design_model(self) -> str:
        return self.settings.model_editor

    def _spend_check(self, product: Product) -> None:
        spent = float(product.cost_usd or 0.0) - self._budget_start
        cap = self.settings.editor_max_cost_per_product_usd
        if cap and spent >= cap:
            raise EditBudgetExceeded(f"this polish already cost ${spent:.2f}, over the ${cap:.2f} limit "
                                     "(EDITOR_MAX_COST_PER_PRODUCT_USD)")

    # ------------------------------------------------------------------ run loop
    def execute(self, session: Session) -> None:
        owner = f"editor:{self.run_id}"
        storage, blocklist, images = get_storage(), get_blocklist(), get_image_provider()
        tried: set[int] = set()
        for _ in range(self.batch_size):
            if self.should_stop():
                break
            claimed = claim_products(session, state=ProductState.EDITING, owner=owner, limit=1,
                                     lock_timeout_minutes=self.settings.lock_timeout_minutes, exclude_ids=tried)
            if not claimed:
                break
            product = claimed[0]
            tried.add(product.id)
            self.log.info("polishing", product_id=product.id)
            try:
                self._edit_one(session, product, storage, blocklist, images)
                transition(session, product, ProductState.CREATED)
                self.items_processed += 1
            except CapReached:
                release(session, product)
                session.commit()
                raise
            except Exception as e:  # noqa: BLE001
                self._give_up_or_retry(session, product, e)
            session.commit()
            self.report_progress()

    def _give_up_or_retry(self, session, product: Product, e: Exception) -> None:
        brief = dict(product.brief_json or {})
        edit = dict(brief.get("edit") or {})
        attempts = int(edit.get("attempts", 0)) + 1
        final = attempts >= MAX_EDIT_ATTEMPTS or isinstance(e, EditBudgetExceeded)
        self.note_error(f"editor failed for product {product.id} (attempt {attempts}): {e}", product_id=product.id)
        brief["last_edit_error"] = str(e)[:500]
        if not final:
            brief["edit"] = {**edit, "attempts": attempts}
            product.brief_json = brief
            release(session, product)
            product.priority = None  # back of the queue for one more try next run
            return
        # hand it back untouched: the owner sees why and can send it again or rebuild instead
        back = edit.get("previous_state") or ProductState.CREATED
        brief.pop("edit", None)
        brief["edits_done"] = list(brief.get("edits_done") or []) + [
            {**{k: edit.get(k) for k in EDIT_RECORD_KEYS if k in edit}, "failed": True, "error": str(e)[:300],
             "completed_run": self.run_id}]
        product.brief_json = brief
        transition(session, product, back, message=f"Editor could not polish product {product.id}: {str(e)[:160]}")

    # ------------------------------------------------------------------ one product
    def _edit_one(self, session, product: Product, storage, blocklist, images) -> None:
        full = dict(product.brief_json or {})
        brief = {k: v for k, v in full.items() if k not in INTERNAL_KEYS}
        plan = dict(full.get("creator_plan") or {})
        if not plan or not product.file_paths:
            raise RuntimeError("nothing to edit: this product has no built files (ask the Creator to build it)")
        req = dict(full.get("edit") or {})
        self._budget_start = float(product.cost_usd or 0.0)
        comments = self.open_comments(session, product)
        qa = req.get("previous_qa") or product.qa_result_json or {}

        changes = requested_changes(req) + [c.text for c in comments]
        checklist = "\n".join(f"- {c}" for c in changes) or "- (none given: raise the overall polish to best-seller level)"
        instructions = (f"REQUESTED CHANGES (apply every one; QA will check each was made):\n{checklist}\n\n"
                        f"Previous QA review: {json.dumps({k: qa.get(k) for k in ('scores', 'reasons', 'verdict') if qa.get(k)})}\n"
                        f"Brief:\n{json.dumps({**brief, 'product_type': product.product_type}, indent=2)}")
        if self._is_mock_run:
            instructions += "\n(This is a MOCK run; placeholder art is expected.)"
        listing = [ImageInput(data=_shrink(storage.get(p))) for p in (product.image_paths or [])[:3]]
        html = art = None
        kind = product.product_type

        if kind in (ProductType.PLANNER, ProductType.TRACKER):
            current = storage.get(plan["design_html"]).decode("utf-8") if plan.get("design_html") else None
            if not current:
                raise RuntimeError("the page design source is missing; rebuild this product instead")
            context = (f"{instructions}\n\nCurrent design brief (planner_spec):\n{json.dumps(plan.get('planner_spec') or {}, indent=2)}"
                       f"\nPalette (background, ink, accent): {self.palette_of(plan)}")
            meta, html = self._ask_editor(session, product, "editor_pages", f"{context}\n\nCURRENT HTML:\n```html\n{current}\n```", listing)
            if not html:
                raise LLMError("editor did not return the revised HTML document")
        elif kind == ProductType.SPREADSHEET:
            context = f"{instructions}\n\nCurrent spreadsheet_spec:\n{json.dumps(plan.get('spreadsheet_spec') or {}, indent=2)}"
            meta, _ = self._ask_editor(session, product, "editor_sheet", context, listing)
            if not isinstance(meta.get("spreadsheet_spec"), dict):
                raise LLMError("editor did not return a revised spreadsheet_spec")
        elif kind == ProductType.WALL_ART:
            context = (f"{instructions}\n\nCurrent image prompt: {plan.get('image_prompt')}\n"
                       f"Current negative prompt: {plan.get('negative_prompt')}")
            source = plan.get("source_image")
            current_art = storage.get(source) if source and storage.exists(storage.key_from_uri(source)) else None
            meta, _ = self._ask_editor(session, product, "editor_art", context,
                                       ([ImageInput(data=_shrink(current_art))] if current_art else []) + listing[:1])
            art = self._edit_art(session, product, images, meta, plan, current_art)
        elif kind in ProductType.PHYSICAL:
            template = (plan.get("merch") or {}).get("template")
            current = storage.get(template).decode("utf-8") if template else None
            if not current:
                raise RuntimeError("the print design source is missing; rebuild this product instead")
            item = get_catalog().get((plan.get("merch_spec") or {}).get("item"))
            context = (f"{instructions}\n\nCurrent merch_spec:\n{json.dumps(plan.get('merch_spec') or {}, indent=2)}"
                       + (f"\nProduct: {item.label} ({item.method}); print area {item.print_area['name']} "
                          f"{item.print_area['width_in']} x {item.print_area['height_in']} in; blank colours: "
                          f"{json.dumps(item.colors)}" if item else ""))
            meta, html = self._ask_editor(session, product, "editor_merch", f"{context}\n\nCURRENT HTML:\n```html\n{current}\n```", listing)
            if not html:
                raise LLMError("editor did not return the revised print design")
            art_path = (plan.get("merch") or {}).get("art")
            if art_path and storage.exists(storage.key_from_uri(art_path)):
                art = storage.get(art_path)
        else:
            raise RuntimeError(f"product_type {kind} cannot be edited")

        new_plan = {**plan, **{k: meta[k] for k in EDITABLE_PLAN_KEYS if k in meta and meta[k] is not None}}
        if kind in ProductType.PHYSICAL and isinstance(meta.get("merch_spec"), dict):
            new_plan["merch_spec"] = {**(plan.get("merch_spec") or {}), **meta["merch_spec"],
                                      "item": (plan.get("merch_spec") or {}).get("item")}  # the blank never changes
        self.check_plan(blocklist, new_plan)
        n_round = int(req.get("round") or len(full.get("edits_done") or []) + 1)
        base = f"products/{product.id}-{self.slug_of(plan, product)}/v{n_round + 1}"
        previous_files, previous_images = list(product.file_paths or []), list(product.image_paths or [])
        new_plan = self.produce(session, product, brief, new_plan, storage, blocklist, images,
                                note="\n\n" + instructions, base=base, context=context, html=html, art=art)

        record = {**{k: req.get(k) for k in EDIT_RECORD_KEYS if k in req}, "round": n_round,
                  "changes_made": [str(c) for c in meta.get("changes_made") or []][:12],
                  "kept": [str(c) for c in meta.get("kept") or []][:8],
                  "previous_files": previous_files, "previous_images": previous_images,
                  "completed_run": self.run_id, "model": self.design_model,
                  "cost_usd": round(float(product.cost_usd or 0.0) - self._budget_start, 4)}
        product.brief_json = {**brief, "creator_plan": new_plan,
                              **({"revisions_done": full["revisions_done"]} if full.get("revisions_done") else {}),
                              "edits_done": list(full.get("edits_done") or []) + [record]}
        product.qa_result_json = None  # the previous verdict no longer describes these files
        product.phash = None
        for c in comments:
            c.status = "included"
        for n in session.execute(select(OwnerNote).where(OwnerNote.product_id == product.id,
                                                         OwnerNote.kind == OwnerNoteKind.EDIT,
                                                         OwnerNote.status == "open")).scalars():
            n.status = "addressed"
        self.log.info("product polished", product_id=product.id, round=n_round, cost=record["cost_usd"],
                      changes=len(record["changes_made"]))

    def _ask_editor(self, session, product: Product, task: str, user: str, images: list[ImageInput]) -> tuple[dict, str | None]:
        self._spend_check(product)
        result = self.ask(session, task=task, model=self.design_model, user=user, images=images, max_tokens=16000)
        product.add_cost(result.cost_usd)
        return split_reply(result.text)

    def _edit_art(self, session, product: Product, images, meta: dict, plan: dict, current: bytes | None) -> bytes:
        if not images.is_real and not self._is_mock_run:
            raise RuntimeError("wall art needs a real IMAGE_PROVIDER (e.g. openai); mock images are not sellable")
        prompt = str(meta.get("image_prompt") or plan.get("image_prompt") or "")
        negative = meta.get("negative_prompt") or plan.get("negative_prompt")
        self._spend_check(product)
        if current and not meta.get("regenerate"):
            edit_prompt = str(meta.get("edit_prompt") or prompt)
            try:
                gen = images.edit(edit_prompt + (f"\nAvoid: {negative}" if negative else ""), current, size=(1024, 1536),
                                  quality=self.settings.image_quality)
            except (NotImplementedError, RuntimeError) as e:
                self.log.warning("image edit unavailable; regenerating from the revised prompt", error=str(e)[:200])
                gen = images.generate(prompt, size=(1024, 1536), negative_prompt=negative)
        else:
            gen = images.generate(prompt, size=(1024, 1536), negative_prompt=negative)
        product.add_cost(gen.cost_usd)
        self.record_cost(session, gen.cost_usd, kind="image")
        return gen.data



