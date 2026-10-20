"""Read-only village API (+ the optional, authenticated Run-now trigger via Railway's API)."""

from __future__ import annotations

import io
import json
from pathlib import PurePosixPath

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import select

from pipeline.agents import AGENT_NAMES, prompt_file_for
from pipeline.config import get_settings
from pipeline.db import session_scope
from pipeline.db.events import EventKind, record_event
from pipeline.db.models import Event, Product, ProductState, Run
from pipeline.logging_setup import get_logger
from pipeline.storage import get_storage
from pipeline.web.auth import require_viewer, require_write
from pipeline.web.village_state import (
    AGENT_META,
    SERVICE_FOR_AGENT,
    RunInfo,
    collect_village_state,
    product_summary,
    serialize_event,
)

log = get_logger("pipeline.web.village")
router = APIRouter(prefix="/api/village", dependencies=[Depends(require_viewer)])


@router.get("/state")
def village_state():
    with session_scope() as s:
        return collect_village_state(s, get_settings())


@router.get("/events")
def village_events(since: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)):
    with session_scope() as s:
        if since:
            rows = s.execute(select(Event).where(Event.id > since).order_by(Event.id.asc()).limit(limit)).scalars().all()
        else:  # initial load: newest first, returned in chronological order
            rows = list(reversed(s.execute(select(Event).order_by(Event.id.desc()).limit(limit)).scalars().all()))
        events = [serialize_event(e) for e in rows]
        return {"events": events, "last_id": events[-1]["id"] if events else since}


@router.get("/agent/{name}")
def village_agent(name: str):
    if name not in AGENT_NAMES:
        raise HTTPException(404, "unknown agent")
    with session_scope() as s:
        runs = s.execute(select(Run).where(Run.agent == name).order_by(Run.started_at.desc()).limit(20)).scalars().all()
        errors = [{"run_id": r.id, "at": r.started_at.isoformat(), "errors": r.errors_json} for r in runs if r.errors_json]
        state = next(a for a in collect_village_state(s, get_settings())["agents"] if a["name"] == name)
        return {**state, "prompt_file": prompt_file_for(name), "meta": AGENT_META[name],
                "runs": [RunInfo.from_row(r).__dict__ for r in runs], "errors": errors}


@router.get("/product/{product_id}")
def village_product(product_id: int):
    with session_scope() as s:
        p = s.get(Product, product_id)
        if not p:
            raise HTTPException(404, "unknown product")
        history = s.execute(select(Event).where(Event.product_id == product_id).order_by(Event.id.asc())).scalars().all()
        brief = {k: v for k, v in (p.brief_json or {}).items() if k != "creator_plan"}
        plan = (p.brief_json or {}).get("creator_plan") or {}
        listing = dict(p.listing_json or {})
        return {
            **product_summary(p), "brief": brief,
            "creator_plan": {k: plan.get(k) for k in ("slug", "title_hint", "image_prompt", "palette", "planner_spec", "spreadsheet_spec")},
            "qa_result": p.qa_result_json, "listing": listing,
            "images": [f"/api/village/image/{p.id}/{i}" for i in range(len(p.image_paths or []))],
            "files": [PurePosixPath(f).name for f in (p.file_paths or [])],
            "history": [serialize_event(e) for e in history],
            "locked_by": p.locked_by,
        }


@router.get("/image/{product_id}/{index}")
def village_image(product_id: int, index: int, w: int = Query(400, ge=32, le=1200)):
    from PIL import Image

    with session_scope() as s:
        p = s.get(Product, product_id)
        if not p or index >= len(p.image_paths or []):
            raise HTTPException(404, "no such image")
        data = get_storage().get(p.image_paths[index])
    img = Image.open(io.BytesIO(data)).convert("RGB")
    img.thumbnail((w, w))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=82)
    return Response(buf.getvalue(), media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})


@router.get("/outbox")
def village_outbox():
    settings = get_settings()
    with session_scope() as s:
        rows = s.execute(select(Product).where(Product.state.in_([ProductState.DRAFTED, ProductState.PUBLISHED]))
                         .order_by(Product.updated_at.desc()).limit(100)).scalars().all()
        drafts = []
        for p in rows:
            listing = p.listing_json or {}
            url = listing.get("etsy_url")
            if not url and p.etsy_listing_id and settings.adapter_mode("etsy") == "live":
                url = f"https://www.etsy.com/your/shops/me/tools/listings/{p.etsy_listing_id}"
            drafts.append({**product_summary(p), "url": url, "price": listing.get("price"), "dry_run": listing.get("dry_run", False),
                           "mock": settings.adapter_mode("etsy") == "mock"})
        return {"drafts": drafts, "mock_shop_state": "mock_etsy/shop_state.json" if settings.adapter_mode("etsy") == "mock" else None}


@router.get("/rejects")
def village_rejects():
    with session_scope() as s:
        rows = s.execute(select(Product).where(Product.state == ProductState.REJECTED)
                         .order_by(Product.updated_at.desc()).limit(100)).scalars().all()
        return {"rejects": [{**product_summary(p), "reasons": (p.qa_result_json or {}).get("reasons", [])} for p in rows]}


@router.post("/run/{agent}", dependencies=[Depends(require_write)])
def village_run_now(agent: str):
    """Start a normal run of an agent now. With the in-app worker (the default) this queues a job the
    worker picks up within seconds; otherwise it re-runs the agent's Railway cron service. Either way
    caps, locking and draft-only apply exactly like a scheduled run."""
    from pipeline.worker import request_run

    settings = get_settings()
    if agent not in AGENT_NAMES:
        raise HTTPException(404, "unknown agent")
    if settings.worker_enabled:
        with session_scope() as s:
            job = request_run(s, agent)
            record_event(s, kind=EventKind.INFO, agent="owner", message=f"You asked the {AGENT_META[agent]['title']} to run now")
            return {"ok": True, "agent": agent, "job_id": job.id, "via": "worker"}
    if not settings.railway_run_now_enabled:
        raise HTTPException(403, "Run now is disabled: enable the in-app worker (WORKER_ENABLED=true) or set "
                                 "RAILWAY_API_TOKEN and RAILWAY_SERVICE_IDS on the web service.")
    try:
        service_ids = json.loads(settings.railway_service_ids)
    except json.JSONDecodeError:
        raise HTTPException(500, "RAILWAY_SERVICE_IDS is not valid JSON")
    service_key = SERVICE_FOR_AGENT[agent]
    service_id = service_ids.get(service_key)
    if not service_id or not settings.railway_environment_id:
        raise HTTPException(500, f"no Railway service id configured for {service_key!r} or RAILWAY_ENVIRONMENT_ID missing")
    # TODO(owner): verify against the current Railway GraphQL schema. serviceInstanceRedeploy re-runs the
    # latest deployment of a cron service, which executes its start command once.
    mutation = {"query": "mutation($serviceId: String!, $environmentId: String!) { serviceInstanceRedeploy(serviceId: $serviceId, environmentId: $environmentId) }",
                "variables": {"serviceId": service_id, "environmentId": settings.railway_environment_id}}
    resp = httpx.post("https://backboard.railway.com/graphql/v2", json=mutation, timeout=30,
                      headers={"Authorization": f"Bearer {settings.railway_api_token}"})
    body = resp.json() if resp.content else {}
    if resp.status_code >= 400 or body.get("errors"):
        log.error("run-now failed", agent=agent, status=resp.status_code, errors=body.get("errors"))
        raise HTTPException(502, f"Railway API error: {body.get('errors') or resp.status_code}")
    log.info("run-now triggered", agent=agent, service=service_key)
    return {"ok": True, "agent": agent, "service": service_key, "via": "railway"}
