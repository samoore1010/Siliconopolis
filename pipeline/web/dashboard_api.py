"""Owner dashboard API: what the agents did, in plain English, and the products they made.

Everything shown comes from the database and storage the agents write to. The only writes are the
owner's own input: shop-wide directives for the Researcher and comments / revision requests on a
product (which send it back to the Creator). Writes need the custom X-Siliconopolis header, which a
cross-site form cannot send, so a logged-in browser cannot be tricked into posting them.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from pipeline.config import get_settings
from pipeline.db import session_scope
from pipeline.db.events import EventKind
from pipeline.db.events import record_event
from pipeline.db.locking import transition
from pipeline.db.models import Event, OwnerNote, OwnerNoteKind, Product, ProductState, ResearchNote, Run, utcnow
from pipeline.generators.mockups import short_title
from pipeline.guardrails.caps import briefs_this_week, drafts_today, todays_spend
from pipeline.storage import get_storage
from pipeline.web.auth import require_viewer
from pipeline.web.village_state import _aware, product_summary

router = APIRouter(prefix="/api/dashboard", dependencies=[Depends(require_viewer)])

REVISABLE = (ProductState.CREATED, ProductState.APPROVED, ProductState.REJECTED)
MAX_NOTE_CHARS = 2000


def require_write(x_siliconopolis: str | None = Header(None)) -> None:
    if x_siliconopolis != "1":
        raise HTTPException(403, "missing X-Siliconopolis header")


class NoteIn(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_NOTE_CHARS)


class CommentIn(NoteIn):
    revise: bool = False


class DirectivePatch(BaseModel):
    text: str | None = Field(None, min_length=1, max_length=MAX_NOTE_CHARS)
    active: bool | None = None


def _note(n: OwnerNote) -> dict[str, Any]:
    return {"id": n.id, "kind": n.kind, "product_id": n.product_id, "text": n.text, "active": n.active,
            "status": n.status, "created_at": _aware(n.created_at).isoformat() if n.created_at else None,
            "used_at": _aware(n.used_at).isoformat() if n.used_at else None}

STAGES = [
    {"state": ProductState.BRIEF, "label": "Ideas", "help": "Researched product ideas waiting to be built"},
    {"state": ProductState.CREATED, "label": "Built", "help": "Files and listing images made, waiting for QA"},
    {"state": ProductState.APPROVED, "label": "Approved", "help": "Passed QA, waiting to be drafted on Etsy"},
    {"state": ProductState.DRAFTED, "label": "Etsy drafts", "help": "Draft listings waiting for you to publish"},
    {"state": ProductState.PUBLISHED, "label": "Live", "help": "Published on Etsy"},
    {"state": ProductState.REJECTED, "label": "Rejected", "help": "Failed QA or could not be built"},
]
STATE_LABEL = {s["state"]: s["label"] for s in STAGES} | {ProductState.RETIRED: "Retired"}
TYPE_LABEL = {"wall_art": "Wall art", "planner": "Planner", "tracker": "Tracker", "spreadsheet": "Spreadsheet"}
AGENT_LABEL = {"researcher": "Researcher", "creator": "Creator", "qa": "QA", "lister": "Lister", "analyst": "Analyst"}
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
    }


def _sentence(e: Event, titles: dict[int, str]) -> str | None:
    """Turn an event into something a person can read."""
    t = f"“{titles.get(e.product_id, f'product {e.product_id}')}”" if e.product_id else ""
    who = AGENT_LABEL.get(e.agent or "", e.agent or "System")
    if e.kind == EventKind.TRANSITION:
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
        verb = {"researcher": "proposed", "creator": "built", "qa": "reviewed", "lister": "drafted",
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
                           .order_by(Event.id.desc()).limit(40)).scalars().all()
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
        briefs_week = briefs_this_week(s)
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

    attention = []
    if counts.get(ProductState.DRAFTED):
        attention.append({"level": "action", "text": f"{counts[ProductState.DRAFTED]} Etsy draft(s) are waiting for you "
                          "to review and publish in the Etsy app."})
    if etsy_live and not etsy_connected:
        attention.append({"level": "warn", "text": "Etsy isn't connected yet (app approval / authorization pending). "
                          f"{counts.get(ProductState.APPROVED, 0)} approved product(s) will be drafted automatically once it is."})
    if briefs_week >= settings.weekly_brief_cap:
        attention.append({"level": "warn", "text": f"The Researcher hit its weekly limit of {settings.weekly_brief_cap} ideas. "
                          "Raise WEEKLY_BRIEF_CAP in Railway for more this week."})
    if spent_today >= settings.max_daily_spend_usd * 0.8:
        attention.append({"level": "warn", "text": f"Today's AI spend is ${spent_today:.2f} of the ${settings.max_daily_spend_usd:.0f} daily limit."})
    if settings.image_provider == "mock" and settings.app_env != "local":
        attention.append({"level": "info", "text": "Wall art is paused until an image provider (OpenAI) is configured."})
    failed = [AGENT_LABEL[a] for a, r in last_runs.items() if r["status"] == "failed"]
    if failed:
        attention.append({"level": "warn", "text": "Last run failed for: " + ", ".join(failed) + ". See Activity for details."})

    stages = [{**st, "count": counts.get(st["state"], 0)} for st in STAGES]
    return {
        "stages": stages, "attention": attention, "activity": activity, "last_runs": last_runs,
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
                              "revision", "revisions_done")}
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
            "revision_pending": bool((p.brief_json or {}).get("revision")),
            "can_revise": p.state in REVISABLE and not p.locked_by,
            "listing": {k: v for k, v in (p.listing_json or {}).items() if k in ("title", "description", "tags", "price", "etsy_url")},
            "images": [{"thumb": f"/api/village/image/{p.id}/{i}?w=600", "full": f"/api/village/image/{p.id}/{i}?w=1200"}
                       for i in range(len(p.image_paths or []))],
            "files": files,
            "errors": [(p.brief_json or {}).get("last_create_error")] if (p.brief_json or {}).get("last_create_error") else [],
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


@router.get("/research")
def research(limit: int = Query(30, ge=1, le=200)):
    with session_scope() as s:
        notes = s.execute(select(ResearchNote).order_by(ResearchNote.created_at.desc()).limit(limit)).scalars().all()
        market, notes_out = [], []
        for n in notes:
            if n.source == "market":
                try:
                    data = json.loads(n.content)
                except json.JSONDecodeError:
                    continue
                count = s.execute(select(func.count(Product.id)).where(
                    Product.niche == data.get("niche"))).scalar_one()
                market.append({**data, "id": n.id, "created_at": _aware(n.created_at).isoformat(), "ideas_in_niche": count})
            else:
                notes_out.append({"source": n.source, "created_at": _aware(n.created_at).isoformat(), "content": n.content})
    return {"market": market, "notes": notes_out}


# ---------------------------------------------------------------------------------------------- owner input
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
            transition(s, p, ProductState.BRIEF, message=f"{short_title(_title(p))}: owner requested changes")
        else:
            record_event(s, kind=EventKind.INFO, agent="owner", product_id=p.id, message=f"Owner commented: {text[:160]}")
        s.flush()
        return {"comment": _note(note), "state": p.state}


@router.get("/directives")
def list_directives():
    with session_scope() as s:
        rows = s.execute(select(OwnerNote).where(OwnerNote.kind == OwnerNoteKind.DIRECTIVE)
                         .order_by(OwnerNote.active.desc(), OwnerNote.id.desc())).scalars().all()
        return {"directives": [_note(n) for n in rows]}


@router.post("/directives", dependencies=[Depends(require_write)])
def add_directive(body: NoteIn):
    with session_scope() as s:
        n = OwnerNote(kind=OwnerNoteKind.DIRECTIVE, text=body.text.strip(), active=True)
        s.add(n)
        record_event(s, kind=EventKind.INFO, agent="owner", message=f"New direction for the team: {n.text[:160]}")
        s.flush()
        return {"directive": _note(n)}


@router.patch("/directives/{note_id}", dependencies=[Depends(require_write)])
def update_directive(note_id: int, body: DirectivePatch):
    with session_scope() as s:
        n = s.get(OwnerNote, note_id)
        if not n or n.kind != OwnerNoteKind.DIRECTIVE:
            raise HTTPException(404, "unknown directive")
        if body.text is not None:
            n.text = body.text.strip()
        if body.active is not None:
            n.active = body.active
        s.flush()
        return {"directive": _note(n)}


@router.delete("/directives/{note_id}", dependencies=[Depends(require_write)])
def delete_directive(note_id: int):
    with session_scope() as s:
        n = s.get(OwnerNote, note_id)
        if not n or n.kind != OwnerNoteKind.DIRECTIVE:
            raise HTTPException(404, "unknown directive")
        s.delete(n)
        return {"deleted": note_id}
