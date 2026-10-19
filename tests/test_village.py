"""Milestone 5: events, state derivation, village API + auth, demo guard, draft-only stays absolute."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from pipeline.db import session_scope
from pipeline.db.models import Event, Product, ProductState, Run, RunStatus
from pipeline.web.village_state import AgentState, derive_agent_state

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ state derivation
@pytest.mark.parametrize(
    "kwargs, expected",
    [
        (dict(open_run=True, last_run_status=None, last_run_at=None, input_count=0, stale_locks=0, awaiting_human=0), AgentState.WORKING),
        (dict(open_run=False, last_run_status=RunStatus.FAILED, last_run_at=NOW, input_count=3, stale_locks=0, awaiting_human=0), AgentState.ERROR),
        (dict(open_run=False, last_run_status=RunStatus.CAP_REACHED, last_run_at=NOW, input_count=3, stale_locks=0, awaiting_human=0), AgentState.CAP_REACHED),
        (dict(open_run=False, last_run_status=RunStatus.SUCCESS, last_run_at=NOW, input_count=0, stale_locks=2, awaiting_human=0), AgentState.STALE_LOCK),
        (dict(open_run=False, last_run_status=RunStatus.SUCCESS, last_run_at=NOW, input_count=0, stale_locks=0, awaiting_human=0), AgentState.IDLE),
        (dict(open_run=False, last_run_status=RunStatus.SUCCESS, last_run_at=NOW - timedelta(days=3), input_count=0, stale_locks=0, awaiting_human=0), AgentState.SLEEPING),
        (dict(open_run=False, last_run_status=RunStatus.SUCCESS, last_run_at=NOW - timedelta(days=3), input_count=5, stale_locks=0, awaiting_human=0), AgentState.IDLE),
        (dict(open_run=False, last_run_status=None, last_run_at=None, input_count=0, stale_locks=0, awaiting_human=0), AgentState.SLEEPING),
    ],
)
def test_derive_creator_state(kwargs, expected):
    state, _reason = derive_agent_state(agent="creator", now=NOW, **kwargs)
    assert state == expected


def test_lister_waits_on_human_only_for_lister():
    common = dict(open_run=False, last_run_status=RunStatus.SUCCESS, last_run_at=NOW, input_count=0, stale_locks=0, awaiting_human=2, now=NOW)
    assert derive_agent_state(agent="lister", **common)[0] == AgentState.WAITING_HUMAN
    assert derive_agent_state(agent="qa", **common)[0] == AgentState.IDLE


def test_weekly_agent_sleep_threshold():
    base = dict(open_run=False, last_run_status=RunStatus.SUCCESS, input_count=0, stale_locks=0, awaiting_human=0, now=NOW)
    assert derive_agent_state(agent="researcher", last_run_at=NOW - timedelta(days=8), **base)[0] == AgentState.IDLE
    assert derive_agent_state(agent="researcher", last_run_at=NOW - timedelta(days=11), **base)[0] == AgentState.SLEEPING


# ------------------------------------------------------------------ events
def test_events_written_on_every_transition_and_run(brief_factory):
    from pipeline.__main__ import main

    with session_scope() as s:
        brief_factory(s)
    assert main(["run", "creator"]) == 0 and main(["run", "qa"]) == 0 and main(["run", "lister"]) == 0
    with session_scope() as s:
        events = s.execute(select(Event).order_by(Event.id)).scalars().all()
        kinds = [e.kind for e in events]
        assert kinds.count("run_started") == 3 and kinds.count("run_finished") == 3
        transitions = [(e.from_state, e.to_state) for e in events if e.kind == "transition"]
        assert transitions == [("brief", "created"), ("created", "approved"), ("approved", "drafted")]
        assert all(e.product_id for e in events if e.kind == "transition")
        assert {e.agent for e in events} == {"creator", "qa", "lister"}


def test_cap_and_error_events(monkeypatch, brief_factory):
    monkeypatch.setenv("MAX_DAILY_SPEND_USD", "1")
    from pipeline.config import reset_settings_cache

    reset_settings_cache()
    with session_scope() as s:
        s.add(Run(agent="creator", status="success", cost_usd=5.0))
        brief_factory(s)
    from pipeline.agents.creator import CreatorAgent

    CreatorAgent().run()
    with session_scope() as s:
        kinds = {e.kind for e in s.execute(select(Event)).scalars()}
        assert "cap_reached" in kinds


def test_event_retention_prunes_old_rows():
    from pipeline.db.events import prune_events, record_event

    with session_scope() as s:
        old = record_event(s, kind="transition", agent="qa", message="old")
        old.ts = datetime.now(timezone.utc) - timedelta(days=40)
        record_event(s, kind="transition", agent="qa", message="new")
    with session_scope() as s:
        assert prune_events(s) == 1
        assert [e.message for e in s.execute(select(Event)).scalars()] == ["new"]


# ------------------------------------------------------------------ API
@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from pipeline.web.app import app

    return TestClient(app)


def test_root_redirects_to_dashboard(client):
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/dashboard"


def test_village_endpoints_open_in_local_without_auth(client, brief_factory):
    from pipeline.__main__ import main

    with session_scope() as s:
        brief_factory(s)
    main(["run", "creator"])
    assert client.get("/village").status_code == 200 and "<canvas" in client.get("/village").text
    s = client.get("/api/village/state").json()
    assert s["queue"]["created"] == 1 and len(s["agents"]) == 5 and s["env"]["mode"] == "mock"
    assert s["run_now_enabled"] is False
    ev = client.get("/api/village/events").json()
    assert ev["events"] and ev["last_id"] == s["last_event_id"]
    assert client.get(f"/api/village/events?since={ev['last_id']}").json()["events"] == []
    a = client.get("/api/village/agent/creator").json()
    assert a["prompt_file"] == "prompts/creator.md" and a["runs"][0]["items"] == 1 and a["run_now_available"] is False
    assert client.get("/api/village/agent/nope").status_code == 404
    p = client.get("/api/village/product/1").json()
    assert p["state"] == "created" and len(p["images"]) == 4 and p["history"]
    img = client.get("/api/village/image/1/0")
    assert img.status_code == 200 and img.headers["content-type"] == "image/jpeg"
    assert client.get("/api/village/image/1/99").status_code == 404
    assert client.get("/api/village/outbox").json()["drafts"] == []
    assert client.get("/api/village/rejects").json()["rejects"] == []
    assert "ANTHROPIC" not in client.get("/api/village/state").text  # no secrets in responses


def test_auth_required_when_configured(client, monkeypatch):
    monkeypatch.setenv("VILLAGE_USER", "owner")
    monkeypatch.setenv("VILLAGE_PASSWORD", "s3cret")
    from pipeline.config import reset_settings_cache

    reset_settings_cache()
    for path in ("/village", "/api/village/state", "/api/village/events", "/status", "/oauth/etsy/start",
                 "/dashboard", "/api/dashboard/overview", "/api/dashboard/products", "/api/dashboard/research",
                 "/api/dashboard/product/1", "/api/dashboard/file/1/0"):
        r = client.get(path)
        assert r.status_code == 401, path
        assert "Basic" in r.headers.get("www-authenticate", "")
    assert client.get("/health").status_code == 200  # Railway healthcheck stays public
    assert client.get("/village", auth=("owner", "wrong")).status_code == 401
    assert client.get("/api/village/state", auth=("owner", "s3cret")).status_code == 200


def test_auth_refuses_unconfigured_hosted_env(client, monkeypatch):
    monkeypatch.setenv("APP_ENV", "staging")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/x")  # validation only; never connected
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_BUCKET", "b")
    monkeypatch.setenv("S3_ENDPOINT", "https://e")
    from pipeline.config import reset_settings_cache

    reset_settings_cache()
    assert client.get("/village").status_code == 503


def test_run_now_disabled_without_railway_token(client):
    r = client.post("/api/village/run/creator")
    assert r.status_code == 403
    assert client.post("/api/village/run/nope").status_code == 404


def test_no_village_route_can_publish(client):
    """Only GETs plus the run-now POST exist; nothing accepts a listing state."""
    from pipeline.web.app import app

    for route in app.routes:
        path = getattr(route, "path", "")
        methods = getattr(route, "methods", set()) or set()
        if path.startswith("/api/village"):
            assert methods <= {"GET", "POST", "HEAD"}, path
            if "POST" in methods:
                assert path == "/api/village/run/{agent}", path
    import inspect

    from pipeline.web import village_api

    src = inspect.getsource(village_api)
    assert "update_listing" not in src and "create_draft_listing" not in src and '"active"' not in src


# ------------------------------------------------------------------ demo
def test_demo_refuses_production_and_live(monkeypatch):
    from pipeline.commands.demo import DemoRefused, check_demo_allowed
    from pipeline.config import reset_settings_cache

    check_demo_allowed()
    monkeypatch.setenv("ETSY_MODE", "live")
    reset_settings_cache()
    with pytest.raises(DemoRefused):
        check_demo_allowed()
    monkeypatch.delenv("ETSY_MODE")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/x")
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_BUCKET", "b")
    monkeypatch.setenv("S3_ENDPOINT", "https://e")
    reset_settings_cache()
    with pytest.raises(DemoRefused, match="production"):
        check_demo_allowed()


def test_demo_runs_a_cycle():
    from pipeline.commands.demo import run_demo

    assert run_demo(cycles=1, sleep_seconds=0) == 0
    with session_scope() as s:
        states = {p.state for p in s.execute(select(Product)).scalars()}
        assert ProductState.DRAFTED in states
