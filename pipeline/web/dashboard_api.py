"""Owner dashboard API: what the agents did, in plain English, and the products they made.

Everything shown comes from the database and storage the agents write to. The only writes are the
owner's own input: run briefs for the Researcher (each steers exactly one research run), notes and
change requests on a product (a rebuild by the Creator, or a polish by the Editor), the order of the
build and polish queues, Stop, and deleting products that are not on Etsy. Writes need the custom
X-Siliconopolis header, which a cross-site form cannot send, so a logged-in browser cannot be tricked
into posting them.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from pipeline.config import get_settings
from pipeline.db import session_scope
from pipeline.db.events import EventKind, record_event
from pipeline.db.locking import QUEUE_ORDER, transition
from pipeline.db.models import (
    BriefStatus,
    Event,
    FulfillmentOrder,
    JobRequest,
    JobStatus,
    OrderStatus,
    OwnerNote,
    OwnerNoteKind,
    Product,
    ProductState,
    ProductType,
    ResearchNote,
    ResearchRequest,
    Run,
    utcnow,
)
from pipeline.generators.mockups import short_title
from pipeline.guardrails.caps import briefs_this_week, drafts_today, todays_spend
from pipeline.schedule import next_run
from pipeline.storage import get_storage
from pipeline.web.auth import require_viewer, require_write
from pipeline.web.village_state import AGENT_META, _aware, product_summary, worker_info

router = APIRouter(prefix="/api/dashboard", dependencies=[Depends(require_viewer)])

# states the owner can send back to the Creator (rebuild) or to the Editor (polish): never anything on Etsy
REVISABLE = (ProductState.CREATED, ProductState.APPROVED, ProductState.REJECTED)
MAX_NOTE_CHARS = 2000


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_NOTE_CHARS)


class CommentIn(NoteIn):
    revise: bool = False


class EditIn(BaseModel):
    note: str = Field("", max_length=MAX_NOTE_CHARS)
    include_qa: bool = True  # pass QA's improvement suggestions along with the note


class BriefIn(BaseModel):
    focus: str = Field(min_length=3, max_length=MAX_NOTE_CHARS)
    product_types: list[str] = Field(default_factory=list)
    idea_count: int = Field(6, ge=1, le=20)


class OrderIn(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=1000)


class DeleteIn(BaseModel):
    ids: list[int] = Field(default_factory=list, max_length=500)
    state: str | None = None  # e.g. "rejected": delete every product in that state


UNDELETABLE = ProductState.ON_ETSY


def _note(n: OwnerNote) -> dict[str, Any]:
    return {"id": n.id, "kind": n.kind, "product_id": n.product_id, "text": n.text, "active": n.active,
            "status": n.status, "created_at": _aware(n.created_at).isoformat() if n.created_at else None,
            "used_at": _aware(n.used_at).isoformat() if n.used_at else None}

STAGES = [
    {"state": ProductState.BRIEF, "label": "Ideas", "help": "Researched product ideas waiting to be built"},
    {"state": ProductState.CREATED, "label": "Built", "help": "Files and listing images made, waiting for QA"},
    {"state": ProductState.EDITING, "label": "With the Editor", "help": "Being polished by the Editor, then back through QA"},
    {"state": ProductState.APPROVED, "label": "Approved", "help": "Passed QA, waiting to be drafted on Etsy"},
    {"state": ProductState.DRAFTED, "label": "Etsy drafts", "help": "Draft listings waiting for you to publish"},
    {"state": ProductState.PUBLISHED, "label": "Live", "help": "Published on Etsy"},
    {"state": ProductState.REJECTED, "label": "Rejected", "help": "Failed QA or could not be built"},
]
STATE_LABEL = {s["state"]: s["label"] for s in STAGES} | {ProductState.RETIRED: "Retired"}
TYPE_LABEL = {"wall_art": "Wall art", "planner": "Planner", "tracker": "Tracker", "spreadsheet": "Spreadsheet",
              "apparel": "Apparel", "mug": "Mug"}
AGENT_LABEL = {name: meta["title"] for name, meta in AGENT_META.items()}
FILE_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".xlsx":
              "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".html": "text/html"}


def _title(p: Product) -> str:
    plan = (p.brief_json or {}).get("creator_plan") or {}
    listing = p.listing_json or {}
    return str(listing.get("title") or plan.get("title_hint") or (p.brief_json or {}).get("concept") or f"Product {p.id}")


def _card(p: Product) -> dict[str, Any]:
    brief = p.brief_json or {}
    qa = p.qa_result_json or {}
    reasons = qa.get("reasons") or []
    return {
        **product_summary(p), "title": _title(p)[:120], "stage": STATE_LABEL.get(p.state, p.state),
        "type_label": TYPE_LABEL.get(p.product_type, p.product_type),
        "price": (p.listing_json or {}).get("price") or brief.get("target_price_usd"),
        "thumb": f"/api/village/image/{p.id}/0?w=480" if p.image_paths else None,
        "one_liner": str(brief.get("rationale") or brief.get("concept") or "")[:220],
        "verdict": None if not qa else ("passed" if qa.get("pass") else "failed"),
        "top_reason": str(reasons[0])[:220] if reasons and not qa.get("pass") else None,
        "created_at": _aware(p.created_at).isoformat() if p.created_at else None,
        "revision_pending": bool(brief.get("revision")),
        "edit_pending": bool(brief.get("edit")),
        "edit_failed": (brief.get("edits_done") or [{}])[-1].get("failed", False) and not brief.get("edit"),
        "polished": any(not e.get("failed") for e in brief.get("edits_done") or []),
        "deletable": p.state not in UNDELETABLE and not p.locked_by,
        "from_brief": brief.get("from_brief"),
    }


def _sentence(e: Event, titles: dict[int, str]) -> str | None:
    """Turn an event into something a person can read."""
    t = f"“{titles.get(e.product_id, f'product {e.product_id}')}”" if e.product_id else ""
    who = AGENT_LABEL.get(e.agent or "", e.agent or "System")
    if e.kind == EventKind.TRANSITION:
        if e.agent == "owner":  # the owner's own moves carry a ready-made sentence
            return e.message
        if e.agent == "editor":
            return f"Editor polished {t}" if e.to_state == ProductState.CREATED else f"Editor gave {t} back: {e.message[:160]}"
        return {
            ProductState.BRIEF: f"Researcher proposed {t}",
            ProductState.CREATED: f"Creator built {t}",
            ProductState.APPROVED: f"QA approved {t}",
            ProductState.REJECTED: f"{who} rejected {t}",
            ProductState.DRAFTED: f"Lister created an Etsy draft for {t}",
            ProductState.PUBLISHED: f"{t} is live on Etsy",
        }.get(e.to_state or "", f"{who} moved {t} to {e.to_state}")
    if e.kind == EventKind.RUN_FINISHED:
        m = re.search(r"(\w+): (\d+) items, \$([\d.]+), (\d+) errors", e.message or "")
        if not m:
            return f"{who} finished a run"
        status, n, cost, errs = m.group(1), int(m.group(2)), float(m.group(3)), int(m.group(4))
        verb = {"researcher": "proposed", "creator": "built", "editor": "polished", "qa": "reviewed", "lister": "drafted",
                "analyst": "analysed"}.get(e.agent or "", "handled")
        text = f"{who} {verb} {n} product{'s' if n != 1 else ''}" if e.agent != "researcher" else f"{who} proposed {n} idea{'s' if n != 1 else ''}"
        if status == "failed":
            text = f"{who}'s run failed"
        elif status == "cap_reached":
            text += " and stopped at a safety limit"
        extra = [f"${cost:.2f}"] + ([f"{errs} problem{'s' if errs != 1 else ''}"] if errs else [])
        return f"{text} ({', '.join(extra)})"
    if e.kind == EventKind.CAP_REACHED:
        return f"{who} stopped: a safety limit was reached ({e.message})"
    if e.kind == EventKind.ERROR:
        return f"{who} hit a problem: {e.message[:200]}"
    if e.kind == EventKind.INFO:
        return f"{who}: {e.message[:200]}"
    return None  # run_started is noise for the owner


@router.get("/overview")
def overview():
    settings = get_settings()
    with session_scope() as s:
        counts = dict(s.execute(select(Product.state, func.count(Product.id)).group_by(Product.state)).all())
        week_ago = datetime.now(UTC) - timedelta(days=7)
        spend_week = float(s.execute(select(func.coalesce(func.sum(Run.cost_usd), 0.0))
                                     .where(Run.started_at >= week_ago)).scalar_one())
        spend_total = float(s.execute(select(func.coalesce(func.sum(Run.cost_usd), 0.0))).scalar_one())
        events = s.execute(select(Event).where(Event.kind != EventKind.RUN_STARTED)
                           .order_by(Event.id.desc()).limit(150)).scalars().all()
        ids = {e.product_id for e in events if e.product_id}
        titles = {p.id: short_title(_title(p)) for p in s.execute(select(Product).where(Product.id.in_(ids))).scalars()} if ids else {}
        activity = [{"ts": _aware(e.ts).isoformat(), "agent": e.agent, "kind": e.kind, "product_id": e.product_id,
                     "to_state": e.to_state, "text": txt}
                    for e in events if (txt := _sentence(e, titles))]
        last_runs = {}
        for name in AGENT_LABEL:
            r = s.execute(select(Run).where(Run.agent == name).order_by(Run.started_at.desc()).limit(1)).scalar_one_or_none()
            if r:
                last_runs[name] = {"status": r.status, "at": _aware(r.started_at).isoformat(), "items": r.items_processed,
                                   "cost_usd": round(r.cost_usd or 0, 4), "errors": len(r.errors_json or [])}
        order_counts = dict(s.execute(select(FulfillmentOrder.status, func.count(FulfillmentOrder.id))
                                      .group_by(FulfillmentOrder.status)).all())
        briefs_week = briefs_this_week(s)
        briefs_queued = s.execute(select(func.count(ResearchRequest.id))
                                  .where(ResearchRequest.status == BriefStatus.QUEUED)).scalar_one()
        spent_today = todays_spend(s)
        drafted_today = drafts_today(s)

        etsy_live = settings.adapter_mode("etsy") == "live"
        etsy_connected = True
        if etsy_live:
            from pipeline.adapters.etsy import get_etsy_client
            try:
                etsy_connected = get_etsy_client().is_connected()
            except Exception:  # noqa: BLE001
                etsy_connected = False

    # What needs the owner, most urgent first. Each item has ONE clear next step inside the app.
    needs: list[dict[str, Any]] = []

    def need(level: str, title: str, detail: str, cta: str | None = None, route: str | None = None) -> None:
        needs.append({"level": level, "title": title, "detail": detail, "cta": cta, "route": route})

    n_check = order_counts.get(OrderStatus.NEEDS_REVIEW, 0)
    if n_check:
        need("action", f"{n_check} order{'s' if n_check != 1 else ''} to check before printing",
             "The buyer's personalisation is unclear or unusual. Fix it or approve it so the print shop can go ahead.",
             "Check orders", "orders")
    if counts.get(ProductState.DRAFTED):
        n = counts[ProductState.DRAFTED]
        need("action", f"{n} Etsy draft{'s' if n != 1 else ''} ready for you to publish",
             "Review each draft in the Etsy app and publish the ones you like. Nothing is published without you.",
             "See drafts", "products/etsy")
    if counts.get(ProductState.REJECTED):
        n = counts[ProductState.REJECTED]
        need("review", f"{n} rejected idea{'s' if n != 1 else ''}",
             "See why QA said no, ask the Creator to rebuild the ones worth saving, and delete the rest.",
             "Review rejected", "products/rejected")
    failed = [AGENT_LABEL[a] for a, r in last_runs.items() if r["status"] == "failed"]
    if failed:
        need("warn", f"{', '.join(failed)} hit a problem on the last run",
             "The next scheduled run will try again. Open the team page to see the error.", "See team", "team")
    if briefs_week >= settings.weekly_brief_cap:
        need("warn", "The Researcher reached this week's idea limit",
             f"{settings.weekly_brief_cap} ideas per week (WEEKLY_BRIEF_CAP in Railway). New ideas resume on Monday.",
             None, None)
    if spent_today >= settings.max_daily_spend_usd * 0.8:
        need("warn", f"AI spend today is ${spent_today:.2f} of ${settings.max_daily_spend_usd:.0f}",
             "Agents stop for the day when the limit is reached.", None, None)
    if etsy_live and not etsy_connected:
        need("info", "Etsy isn't connected yet",
             f"Waiting for app approval. {counts.get(ProductState.APPROVED, 0)} approved product(s) will be drafted "
             "automatically once it is.", None, None)
    waiting_print = order_counts.get(OrderStatus.READY, 0) + order_counts.get(OrderStatus.PRINTING, 0)
    if waiting_print:
        need("info", f"{waiting_print} order{'s' if waiting_print != 1 else ''} with the print shop",
             "Being printed. Tracking goes to the buyer automatically when they ship.", "See orders", "orders")
    if settings.image_provider == "mock" and settings.app_env != "local":
        need("info", "Wall art is paused", "It needs an image provider (OpenAI) to be configured.", None, None)
    worker = worker_info()
    if not worker["enabled"]:
        need("warn", "The in-app worker is off",
             "Agents only run from Railway cron services and \"Run now\" needs a Railway token. Set WORKER_ENABLED=true "
             "on the web service so QA, the Editor and the Lister react to their queues immediately.", None, None)
    elif not worker["alive"]:
        need("warn", "The in-app worker isn't running", f"{worker.get('error') or 'It has not started yet.'} "
             "Redeploy the web service if this persists.", None, None)
    with session_scope() as s:
        queued_jobs = s.execute(select(JobRequest).where(JobRequest.status.in_([JobStatus.QUEUED, JobStatus.RUNNING]))
                                .order_by(JobRequest.id.asc())).scalars().all()
        jobs = [{"id": j.id, "agent": j.agent, "label": AGENT_LABEL.get(j.agent, j.agent), "status": j.status,
                 "requested_at": _aware(j.requested_at).isoformat()} for j in queued_jobs]

    now = datetime.now(UTC)
    with session_scope() as s:
        working = _working(s)
    schedule = [{"key": k, "label": label, "at": next_run(k, now).isoformat()} for k, label in (
        ("researcher", "Research run"), ("creator", "Build, review & list"), ("fulfiller", "Order check"),
        ("analyst", "Sales analysis"))]
    if counts.get(ProductState.EDITING):
        schedule.insert(1, {"key": "editor", "label": "Polish (Editor)", "at": next_run("editor", now).isoformat()})

    stages = [{**st, "count": counts.get(st["state"], 0)} for st in STAGES]
    return {
        "working": working, "stages": stages, "needs_you": needs, "schedule": schedule, "briefs_queued": briefs_queued,
        "worker": {**worker, "jobs": jobs}, "run_now_available": settings.run_now_enabled,
        "activity": activity, "last_runs": last_runs,
        "orders": {k: order_counts.get(k, 0) for k in OrderStatus.ALL},
        "spend": {"today": round(spent_today, 2), "week": round(spend_week, 2), "total": round(spend_total, 2),
                  "daily_limit": settings.max_daily_spend_usd},
        "limits": {"ideas_this_week": briefs_week, "ideas_cap": settings.weekly_brief_cap,
                   "drafts_today": drafted_today, "drafts_cap": settings.daily_draft_cap},
        "settings": {"app_env": settings.app_env, "etsy": "connected" if etsy_connected and etsy_live
                     else ("waiting for approval" if etsy_live else "mock"),
                     "images": settings.image_provider, "dry_run": settings.dry_run},
    }


@router.get("/products")
def products(state: str | None = Query(None), q: str | None = Query(None), limit: int = Query(200, ge=1, le=500)):
    with session_scope() as s:
        stmt = select(Product).order_by(Product.updated_at.desc()).limit(limit)
        if state:
            stmt = stmt.where(Product.state == state)
        rows = s.execute(stmt).scalars().all()
        cards = [_card(p) for p in rows]
    if q:
        ql = q.lower()
        cards = [c for c in cards if ql in (c["title"] + " " + (c["niche"] or "") + " " + c["one_liner"]).lower()]
    return {"products": cards}


@router.get("/product/{product_id}")
def product(product_id: int):
    with session_scope() as s:
        p = s.get(Product, product_id)
        if not p:
            raise HTTPException(404, "unknown product")
        brief = {k: v for k, v in (p.brief_json or {}).items()
                 if k not in ("creator_plan", "market", "run_id", "create_attempts", "last_create_error", "niche",
                              "revision", "revisions_done", "edit", "edits_done", "last_edit_error")}
        notes = s.execute(select(OwnerNote).where(OwnerNote.product_id == product_id)
                          .order_by(OwnerNote.id.asc())).scalars().all()
        plan = (p.brief_json or {}).get("creator_plan") or {}
        market_ref = (p.brief_json or {}).get("market") or {}
        market = None
        if market_ref.get("note_id"):
            note = s.get(ResearchNote, market_ref["note_id"])
            if note:
                market = {**json.loads(note.content), "id": note.id, "created_at": _aware(note.created_at).isoformat()}
        history = s.execute(select(Event).where(Event.product_id == product_id).order_by(Event.id.asc())).scalars().all()
        titles = {p.id: short_title(_title(p))}
        qa = p.qa_result_json or {}
        files = [{"name": PurePosixPath(f).name, "url": f"/api/dashboard/file/{p.id}/{i}",
                  "kind": PurePosixPath(f).suffix.lstrip(".").upper()} for i, f in enumerate(p.file_paths or [])]
        design = plan.get("design_html")
        if design:
            files.append({"name": "design source (HTML)", "url": f"/api/dashboard/file/{p.id}/design", "kind": "HTML"})
        full = p.brief_json or {}
        edits = [{"round": e.get("round"), "note": e.get("note"), "improvements": e.get("improvements") or [],
                  "changes_made": e.get("changes_made") or [], "kept": e.get("kept") or [], "failed": bool(e.get("failed")),
                  "error": e.get("error"), "cost_usd": e.get("cost_usd"), "model": e.get("model"),
                  "requested_at": e.get("requested_at"), "previous_scores": (e.get("previous_qa") or {}).get("scores") or {},
                  "before": [f"/api/dashboard/product/{p.id}/before/{i}/{j}" for j in range(len(e.get("previous_images") or []))]}
                 for i, e in enumerate(full.get("edits_done") or [])]
        pending_edit = full.get("edit")
        return {
            **_card(p),
            "brief": brief,
            "why": {"concept": brief.get("concept"), "rationale": brief.get("rationale"), "audience": brief.get("audience"),
                    "style": brief.get("style"), "keywords": brief.get("keywords") or [],
                    "target_price_usd": brief.get("target_price_usd"), "original_elements": brief.get("original_elements")},
            "market": market, "market_summary": market_ref or None,
            "design": {"palette": plan.get("palette"), "planner_spec": plan.get("planner_spec"),
                       "spreadsheet_spec": plan.get("spreadsheet_spec"), "image_prompt": plan.get("image_prompt")},
            "qa": {"passed": qa.get("pass"), "scores": qa.get("scores") or {}, "reasons": qa.get("reasons") or [],
                   "ip_concerns": qa.get("ip_concerns") or [], "checks": qa.get("checks") or {},
                   "verdict": qa.get("verdict"), "improvements": qa.get("improvements") or [],
                   "reviewed": qa.get("reviewed")} if qa else None,
            "file_check": plan.get("file_check"),
            "comments": [_note(n) for n in notes],
            "revision_pending": bool(full.get("revision")),
            "can_revise": p.state in REVISABLE and not p.locked_by,
            "can_edit": p.state in REVISABLE and not p.locked_by and bool(p.file_paths) and not full.get("revision"),
            "edit_pending": {"note": pending_edit.get("note"), "improvements": pending_edit.get("improvements") or [],
                             "requested_at": pending_edit.get("requested_at"), "attempts": pending_edit.get("attempts", 0)}
                            if pending_edit else None,
            "edits": edits,
            "last_edit_error": full.get("last_edit_error"),
            "listing": {k: v for k, v in (p.listing_json or {}).items() if k in ("title", "description", "tags", "price", "etsy_url")},
            "images": [{"thumb": f"/api/village/image/{p.id}/{i}?w=600", "full": f"/api/village/image/{p.id}/{i}?w=1200"}
                       for i in range(len(p.image_paths or []))],
            "files": files,
            "errors": [full.get("last_create_error")] if full.get("last_create_error") else [],
            "timeline": [{"ts": _aware(e.ts).isoformat(), "text": txt} for e in history if (txt := _sentence(e, titles))],
        }


@router.get("/file/{product_id}/{index}")
def product_file(product_id: int, index: str):
    with session_scope() as s:
        p = s.get(Product, product_id)
        if not p:
            raise HTTPException(404, "unknown product")
        if index == "design":
            key = ((p.brief_json or {}).get("creator_plan") or {}).get("design_html")
        else:
            try:
                key = (p.file_paths or [])[int(index)]
            except (ValueError, IndexError):
                key = None
    if not key:
        raise HTTPException(404, "no such file")
    name = PurePosixPath(key).name
    data = get_storage().get(key)
    media = FILE_TYPES.get(PurePosixPath(name).suffix.lower(), "application/octet-stream")
    disposition = "inline" if media in ("application/pdf", "image/png") else "attachment"
    if media == "text/html":  # never render model-written HTML on our origin
        media, disposition = "text/plain; charset=utf-8", "inline"
    return Response(data, media_type=media, headers={"Content-Disposition": f'{disposition}; filename="{name}"'})


@router.get("/product/{product_id}/before/{edit_index}/{index}")
def product_before_image(product_id: int, edit_index: int, index: int, w: int = Query(600, ge=32, le=1200)):
    """A listing image from the version the Editor started from (kept so you can compare)."""
    import io

    from PIL import Image

    with session_scope() as s:
        p = s.get(Product, product_id)
        edits = (p.brief_json or {}).get("edits_done") or [] if p else []
        try:
            key = edits[edit_index]["previous_images"][index]
        except (IndexError, KeyError, TypeError):
            raise HTTPException(404, "no such image")
    img = Image.open(io.BytesIO(get_storage().get(key))).convert("RGB")
    img.thumbnail((w, w))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})


def _brief(b: ResearchRequest) -> dict[str, Any]:
    iso = lambda d: _aware(d).isoformat() if d else None  # noqa: E731
    return {"id": b.id, "focus": b.focus, "product_types": b.product_types or [], "idea_count": b.idea_count,
            "status": b.status, "run_id": b.run_id, "created_at": iso(b.created_at), "started_at": iso(b.started_at),
            "finished_at": iso(b.finished_at), "product_ids": [int(i) for i in b.product_ids or []], "note": b.note}


def _types_available() -> list[dict[str, str]]:
    settings = get_settings()
    types = list(ProductType.DIGITAL) + list(ProductType.PHYSICAL)
    if settings.image_provider == "mock" and settings.app_env != "local":
        types.remove(ProductType.WALL_ART)
    return [{"key": t, "label": TYPE_LABEL.get(t, t)} for t in types]


@router.get("/research")
def research(limit: int = Query(15, ge=1, le=100)):
    """Research, organised by run: what was asked (the brief, if any), what was found, what was made."""
    settings = get_settings()
    with session_scope() as s:
        runs = s.execute(select(Run).where(Run.agent == "researcher").order_by(Run.started_at.desc())
                         .limit(limit)).scalars().all()
        run_ids = {r.id for r in runs}
        briefs = s.execute(select(ResearchRequest).order_by(ResearchRequest.created_at.desc()).limit(100)).scalars().all()
        by_run_brief = {b.run_id: b for b in briefs if b.run_id}
        markets: dict[int, list[dict]] = {}
        for n in s.execute(select(ResearchNote).where(ResearchNote.source == "market")
                           .order_by(ResearchNote.created_at.desc()).limit(300)).scalars():
            try:
                data = json.loads(n.content)
            except json.JSONDecodeError:
                continue
            if data.get("run_id") in run_ids:
                markets.setdefault(data["run_id"], []).append({**data, "id": n.id})
        ideas: dict[int, list[dict]] = {}
        for p in s.execute(select(Product).order_by(Product.id.asc())).scalars():
            rid = (p.brief_json or {}).get("run_id")
            if rid in run_ids:
                ideas.setdefault(rid, []).append(_card(p))
        out_runs = []
        for r in runs:
            b = by_run_brief.get(r.id)
            out_runs.append({
                "run_id": r.id, "started_at": _aware(r.started_at).isoformat(), "status": r.status,
                "cost_usd": round(r.cost_usd or 0, 3), "errors": r.errors_json or [],
                "brief": _brief(b) if b else None, "ideas": ideas.get(r.id, []), "markets": markets.get(r.id, [])})
        queued = [_brief(b) for b in sorted(briefs, key=lambda b: b.created_at) if b.status in
                  (BriefStatus.QUEUED, BriefStatus.RUNNING)]
        used = briefs_this_week(s)
    return {
        "queued": queued, "runs": out_runs, "next_run": next_run("researcher").isoformat(),
        "run_now_available": settings.run_now_enabled, "types": _types_available(),
        "weekly": {"used": used, "cap": settings.weekly_brief_cap},
    }


@router.post("/research/briefs", dependencies=[Depends(require_write)])
def add_brief(body: BriefIn):
    allowed = {t["key"] for t in _types_available()}
    with session_scope() as s:
        b = ResearchRequest(focus=body.focus.strip(), product_types=[t for t in body.product_types if t in allowed],
                            idea_count=body.idea_count, status=BriefStatus.QUEUED, product_ids=[])
        s.add(b)
        record_event(s, kind=EventKind.INFO, agent="owner", message=f"Run brief queued: {b.focus[:160]}")
        s.flush()
        return {"brief": _brief(b)}


@router.patch("/research/briefs/{brief_id}", dependencies=[Depends(require_write)])
def edit_brief(brief_id: int, body: BriefIn):
    allowed = {t["key"] for t in _types_available()}
    with session_scope() as s:
        b = s.get(ResearchRequest, brief_id)
        if not b:
            raise HTTPException(404, "unknown brief")
        if b.status != BriefStatus.QUEUED:
            raise HTTPException(409, "This brief has already been used by a run, so it can't be changed.")
        b.focus, b.idea_count = body.focus.strip(), body.idea_count
        b.product_types = [t for t in body.product_types if t in allowed]
        s.flush()
        return {"brief": _brief(b)}


@router.delete("/research/briefs/{brief_id}", dependencies=[Depends(require_write)])
def cancel_brief(brief_id: int):
    with session_scope() as s:
        b = s.get(ResearchRequest, brief_id)
        if not b:
            raise HTTPException(404, "unknown brief")
        if b.status != BriefStatus.QUEUED:
            raise HTTPException(409, "Only a brief that is still waiting for its run can be cancelled.")
        b.status = BriefStatus.CANCELLED
        return {"brief": _brief(b)}


@router.post("/research/run-now", dependencies=[Depends(require_write)])
def research_run_now():
    from pipeline.web.village_api import village_run_now

    return village_run_now("researcher")


# ---------------------------------------------------------------------------------------------- delete
def _delete_product(s, p: Product, storage) -> str | None:
    """Remove a product, its notes and its files. Returns a reason when it can't be deleted."""
    if p.state in UNDELETABLE:
        return "it is on Etsy; delete it there"
    if p.locked_by:
        return "an agent is working on it right now"
    keys = set()
    previous = [k for e in (p.brief_json or {}).get("edits_done") or []
                for k in (e.get("previous_files") or []) + (e.get("previous_images") or [])]
    for path in (p.file_paths or []) + (p.image_paths or []) + previous:
        key = storage.key_from_uri(path)
        keys.add(key)
        parts = key.split("/")
        if len(parts) >= 3 and parts[0] == "products":  # products/<id>-<slug>/... : the whole product folder
            keys.update(storage.list("/".join(parts[:2]) + "/"))
    for key in keys:
        try:
            storage.delete(key)
        except Exception:  # noqa: BLE001 - a missing file must not block deleting the record
            pass
    for n in s.execute(select(OwnerNote).where(OwnerNote.product_id == p.id)).scalars():
        s.delete(n)
    record_event(s, kind=EventKind.INFO, agent="owner", message=f"Owner deleted {short_title(_title(p))!r}")
    s.delete(p)
    return None


@router.delete("/product/{product_id}", dependencies=[Depends(require_write)])
def delete_product(product_id: int):
    with session_scope() as s:
        p = s.get(Product, product_id)
        if not p:
            raise HTTPException(404, "unknown product")
        reason = _delete_product(s, p, get_storage())
        if reason:
            raise HTTPException(409, f"Can't delete: {reason}.")
        return {"deleted": [product_id]}


@router.post("/products/delete", dependencies=[Depends(require_write)])
def delete_products(body: DeleteIn):
    """Bulk delete: a list of ids, or every product in one state (e.g. all rejected)."""
    if not body.ids and body.state not in (ProductState.REJECTED, ProductState.BRIEF):
        raise HTTPException(422, "Pass ids, or state 'rejected' / 'brief'.")
    storage = get_storage()
    deleted, skipped = [], []
    with session_scope() as s:
        stmt = select(Product).where(Product.id.in_(body.ids)) if body.ids else \
            select(Product).where(Product.state == body.state)
        for p in s.execute(stmt).scalars().all():
            reason = _delete_product(s, p, storage)
            (skipped.append({"id": p.id, "reason": reason}) if reason else deleted.append(p.id))
    return {"deleted": deleted, "skipped": skipped}


@router.post("/product/{product_id}/comments", dependencies=[Depends(require_write)])
def add_comment(product_id: int, body: CommentIn):
    """Leave a note on a product. With revise=true the product goes back to the Creator, which rebuilds
    it with this feedback (and QA checks the feedback was applied)."""
    text = body.text.strip()
    with session_scope() as s:
        p = s.get(Product, product_id)
        if not p:
            raise HTTPException(404, "unknown product")
        if body.revise:
            if p.state not in REVISABLE:
                raise HTTPException(409, {
                    ProductState.BRIEF: "This idea hasn't been built yet; your note will be used when it is.",
                    ProductState.EDITING: "This product is with the Editor. Take it back from the polish queue first.",
                    ProductState.DRAFTED: "This product is already an Etsy draft; edit or delete it in Etsy.",
                    ProductState.PUBLISHED: "This product is live on Etsy; edit it in Etsy.",
                }.get(p.state, f"Products in '{p.state}' can't be revised."))
            if p.locked_by:
                raise HTTPException(409, "An agent is working on this product right now; try again in a few minutes.")
        note = OwnerNote(kind=OwnerNoteKind.REVISION if body.revise else OwnerNoteKind.COMMENT, product_id=p.id,
                         text=text, status="open" if body.revise else None)
        s.add(note)
        if body.revise:
            brief = dict(p.brief_json or {})
            done = brief.get("revisions_done") or []
            open_notes = s.execute(select(OwnerNote).where(OwnerNote.product_id == p.id,
                                                           OwnerNote.kind == OwnerNoteKind.COMMENT,
                                                           OwnerNote.status.is_(None))).scalars().all()
            # earlier plain comments on this version ride along with the revision request
            feedback = "\n".join(f"- {n.text}" for n in open_notes) + ("\n" if open_notes else "") + f"- {text}"
            for n in open_notes:
                n.status = "included"
            brief["revision"] = {"feedback": feedback, "round": len(done) + 1,
                                 "requested_at": utcnow().isoformat(), "previous_plan": brief.get("creator_plan") or {},
                                 "previous_qa": {k: (p.qa_result_json or {}).get(k) for k in ("pass", "scores", "reasons")}}
            brief.pop("creator_plan", None)
            brief.pop("create_attempts", None)
            brief.pop("last_create_error", None)
            p.brief_json = brief
            p.qa_result_json = None
            p.priority = _top_priority(s, ProductState.BRIEF)  # your changes are built first
            transition(s, p, ProductState.BRIEF, agent="owner",
                       message=f"You asked the Creator to rebuild “{short_title(_title(p))}”")
        else:
            record_event(s, kind=EventKind.INFO, agent="owner", product_id=p.id, message=f"Owner commented: {text[:160]}")
        s.flush()
        return {"comment": _note(note), "state": p.state}


# ---------------------------------------------------------------------------------------------- send to the Editor
@router.post("/product/{product_id}/edit", dependencies=[Depends(require_write)])
def send_to_editor(product_id: int, body: EditIn):
    """Send a built, approved or rejected product to the Editor for a polish. Your note (optional) and
    QA's improvement suggestions go with it; the Editor starts from the current files. Never anything
    already on Etsy. Nothing goes to the Editor unless you send it."""
    note = body.note.strip()
    with session_scope() as s:
        p = s.get(Product, product_id)
        if not p:
            raise HTTPException(404, "unknown product")
        if p.state not in REVISABLE:
            raise HTTPException(409, {
                ProductState.BRIEF: "This idea hasn't been built yet, so there is nothing to polish.",
                ProductState.EDITING: "It's already in the Editor's queue.",
                ProductState.DRAFTED: "This product is already an Etsy draft; edit it in Etsy.",
                ProductState.PUBLISHED: "This product is live on Etsy; edit it in Etsy.",
            }.get(p.state, f"Products in '{p.state}' can't be sent to the Editor."))
        if p.locked_by:
            raise HTTPException(409, "An agent is working on this product right now; try again in a few minutes.")
        brief = dict(p.brief_json or {})
        if brief.get("revision"):
            raise HTTPException(409, "The Creator is about to rebuild this with your changes; wait for that first.")
        if not p.file_paths or not brief.get("creator_plan"):
            raise HTTPException(409, "This product has no built files to polish; ask the Creator to rebuild it.")
        qa = p.qa_result_json or {}
        improvements = [str(i) for i in (qa.get("improvements") or [])] if body.include_qa else []
        if not note and not improvements:
            raise HTTPException(422, "Add a note, or include QA's suggestions, so the Editor knows what to improve.")
        if note:
            s.add(OwnerNote(kind=OwnerNoteKind.EDIT, product_id=p.id, text=note, status="open"))
        brief["edit"] = {"note": note, "improvements": improvements, "round": len(brief.get("edits_done") or []) + 1,
                         "requested_at": utcnow().isoformat(), "previous_state": p.state,
                         "previous_qa": {k: qa.get(k) for k in ("pass", "scores", "reasons", "verdict") if k in qa}}
        brief.pop("last_edit_error", None)
        p.brief_json = brief
        p.priority = _top_priority(s, ProductState.EDITING)  # the latest request is polished first
        transition(s, p, ProductState.EDITING, agent="owner", message=f"You sent “{short_title(_title(p))}” to the Editor")
        s.flush()
        return {"state": p.state, "edit": brief["edit"]}


@router.delete("/queue/editor/{product_id}", dependencies=[Depends(require_write)])
def take_back_from_editor(product_id: int):
    """Cancel a polish that has not started: the product returns to where it was, unchanged."""
    with session_scope() as s:
        p = s.get(Product, product_id)
        if not p or p.state != ProductState.EDITING:
            raise HTTPException(404, "That product isn't waiting for the Editor.")
        if p.locked_by:
            raise HTTPException(409, "The Editor is polishing it right now; it will be back shortly.")
        brief = dict(p.brief_json or {})
        edit = brief.pop("edit", None) or {}
        p.brief_json = brief
        for n in s.execute(select(OwnerNote).where(OwnerNote.product_id == p.id, OwnerNote.kind == OwnerNoteKind.EDIT,
                                                   OwnerNote.status == "open")).scalars():
            n.status = "cancelled"
        back = edit.get("previous_state") or ProductState.CREATED
        transition(s, p, back, agent="owner", message=f"You took “{short_title(_title(p))}” back from the Editor")
        return {"id": p.id, "state": p.state}


# ---------------------------------------------------------------------------------------------- build / polish queues & stop
STOPPABLE = ("researcher", "creator", "editor")
LIVE_RUN_HOURS = 6  # an unfinished run older than this crashed; don't show it as working
QUEUES = {"creator": ProductState.BRIEF, "editor": ProductState.EDITING}  # worker -> the state it claims from


def _top_priority(s, state: str) -> int:
    lowest = s.execute(select(func.min(Product.priority)).where(Product.state == state)).scalar()
    return min(0, (lowest if lowest is not None else 0)) - 1


def _working(s) -> list[dict[str, Any]]:
    """Agents that are mid-run right now, with progress and whether Stop was pressed."""
    since = datetime.now(UTC) - timedelta(hours=LIVE_RUN_HOURS)
    runs = s.execute(select(Run).where(Run.ended_at.is_(None), Run.status == "running", Run.started_at >= since)
                     .order_by(Run.started_at.desc())).scalars().all()
    out, seen = [], set()
    for r in runs:
        if r.agent in seen:
            continue
        seen.add(r.agent)
        current = s.execute(select(Product).where(Product.locked_by == f"{r.agent}:{r.id}")).scalars().first()
        planned = None
        if r.agent in QUEUES:
            from pipeline.agents import registry

            planned = registry()[r.agent].DEFAULT_BATCH
        out.append({"agent": r.agent, "label": AGENT_LABEL.get(r.agent, r.agent), "run_id": r.id,
                    "started_at": _aware(r.started_at).isoformat(), "items": r.items_processed, "planned": planned,
                    "current": {"id": current.id, "title": short_title(_title(current))} if current else None,
                    "stoppable": r.agent in STOPPABLE, "stop_requested": bool(r.stop_requested_at)})
    return out


def _queue(worker: str) -> dict[str, Any]:
    """Products waiting for a worker (Creator: ideas to build; Editor: products to polish), in order."""
    from pipeline.agents import registry

    state = QUEUES[worker]
    with session_scope() as s:
        working_now = s.execute(select(Product).where(Product.state == state,
                                                      Product.locked_by.like(f"{worker}:%"))).scalars().all()
        waiting = s.execute(select(Product).where(Product.state == state, Product.locked_by.is_(None))
                            .order_by(*QUEUE_ORDER)).scalars().all()
        live = next((w for w in _working(s) if w["agent"] == worker), None)
        return {"building": [_card(p) for p in working_now], "queue": [_card(p) for p in waiting],
                "batch_size": registry()[worker].DEFAULT_BATCH, "worker": live, "next_run_at": next_run(worker).isoformat()}


def _reorder(worker: str, ids: list[int]) -> dict[str, Any]:
    """The ids given come first, in this order; anything not listed follows."""
    with session_scope() as s:
        rows = {p.id: p for p in s.execute(select(Product).where(Product.id.in_(ids),
                                                                Product.state == QUEUES[worker])).scalars()}
        order = [i for i in ids if i in rows]
        for pos, pid in enumerate(order):
            rows[pid].priority = pos
        return {"order": order}


def _next(worker: str, product_id: int) -> dict[str, Any]:
    with session_scope() as s:
        p = s.get(Product, product_id)
        if not p or p.state != QUEUES[worker]:
            raise HTTPException(404, "That product isn't in this queue.")
        p.priority = _top_priority(s, QUEUES[worker])
        return {"id": p.id, "priority": p.priority}


@router.get("/queue")
def build_queue():
    """Ideas waiting for the Creator, in the order they will be built."""
    return _queue("creator")


@router.get("/queue/editor")
def polish_queue():
    """Products waiting for the Editor, in the order they will be polished."""
    return _queue("editor")


@router.post("/queue/order", dependencies=[Depends(require_write)])
def reorder_queue(body: OrderIn):
    return _reorder("creator", body.ids)


@router.post("/queue/editor/order", dependencies=[Depends(require_write)])
def reorder_polish_queue(body: OrderIn):
    return _reorder("editor", body.ids)


@router.post("/queue/editor/{product_id}/next", dependencies=[Depends(require_write)])
def polish_next(product_id: int):
    return _next("editor", product_id)


@router.post("/queue/{product_id}/next", dependencies=[Depends(require_write)])
def build_next(product_id: int):
    return _next("creator", product_id)


@router.post("/agents/{agent}/run", dependencies=[Depends(require_write)])
def run_agent_now(agent: str):
    """Run an agent now (a normal run: caps, locks, draft-only all apply). Picked up by the in-app worker."""
    from pipeline.web.village_api import village_run_now

    if agent not in AGENT_LABEL:
        raise HTTPException(404, "unknown agent")
    return village_run_now(agent)


@router.post("/agents/{agent}/stop", dependencies=[Depends(require_write)])
def stop_agent(agent: str):
    """Ask a running Researcher/Creator/Editor to stop: it finishes the item in hand, keeps everything
    done so far, and ends the run. The next scheduled run happens as normal."""
    if agent not in STOPPABLE:
        raise HTTPException(404, "Only the Researcher, the Creator and the Editor can be stopped mid-run.")
    with session_scope() as s:
        live = next((w for w in _working(s) if w["agent"] == agent), None)
        if not live:
            raise HTTPException(409, f"The {AGENT_LABEL[agent]} isn't running right now.")
        run = s.get(Run, live["run_id"])
        run.stop_requested_at = run.stop_requested_at or utcnow()
        record_event(s, kind=EventKind.INFO, agent="owner", message=f"Asked the {AGENT_LABEL[agent]} to stop")
        return {"stopping": agent, "run_id": run.id}
