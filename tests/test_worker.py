"""The in-app worker: reacts to queues, keeps the schedule, honours Run now, never spins on fruitless runs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

HDR = {"X-Siliconopolis": "1"}


class Clock:
    def __init__(self, at: datetime):
        self.at = at

    def __call__(self) -> datetime:
        return self.at


def _worker(clock, calls):
    from pipeline.agents import get_agent
    from pipeline.worker import Worker

    def runner(name):
        calls.append(name)
        return get_agent(name).run()

    return Worker(now=clock, runner=runner)


def test_reactive_agents_run_as_soon_as_their_queue_has_work(session):
    # Tuesday 15:00 UTC: after the daily 14:00 build, before anything weekly. Pre-create the scheduled
    # runs so only the reactive agents are due.
    from pipeline.db.models import Product, ProductState, Run

    clock = Clock(datetime(2026, 10, 20, 15, 0, tzinfo=UTC))
    for agent, at in (("creator", clock.at - timedelta(minutes=50)), ("fulfiller", clock.at),
                      ("researcher", clock.at - timedelta(days=1)), ("analyst", clock.at - timedelta(days=1))):
        session.add(Run(agent=agent, status="success", started_at=at, ended_at=at))
    session.add(Product(state="brief", niche="n", product_type="spreadsheet", brief_json={"concept": "Workbook"}))
    session.commit()
    calls: list[str] = []
    w = _worker(clock, calls)
    assert w.tick() == []  # a brief waits for the daily build; nothing reactive to do
    from pipeline.agents.creator import CreatorAgent

    CreatorAgent().run()
    assert w.tick() == ["qa", "lister"]  # built -> QA at once -> approved -> Lister at once
    session.expire_all()
    assert session.query(Product).one().state == ProductState.DRAFTED
    assert w.tick() == []


def test_editor_runs_within_a_tick_of_being_sent_something(session, client_with_worker):
    client, clock, calls, w = client_with_worker
    from pipeline.agents.creator import CreatorAgent
    from pipeline.db.models import Product, ProductState

    session.add(Product(state="brief", niche="n", product_type="spreadsheet", brief_json={"concept": "Workbook"}))
    session.commit()
    CreatorAgent().run()
    w.tick()  # qa, lister
    session.expire_all()
    p = session.query(Product).one()
    assert p.state == ProductState.DRAFTED  # mock Etsy drafts it; take it back to approved for the test
    p.state, p.etsy_listing_id, p.listing_json = ProductState.APPROVED, None, None
    session.commit()
    assert client.post(f"/api/dashboard/product/{p.id}/edit", json={"note": "tidy"}, headers=HDR).status_code == 200
    calls.clear()
    assert w.tick()[0] == "editor"
    session.expire_all()
    assert session.get(Product, p.id).state in (ProductState.CREATED, ProductState.APPROVED, ProductState.DRAFTED)


def test_fruitless_reactive_run_backs_off_until_new_work_arrives(session, monkeypatch):
    from pipeline.db.models import Product, Run

    clock = Clock(datetime(2026, 10, 20, 15, 0, tzinfo=UTC))
    for agent in ("creator", "fulfiller", "researcher", "analyst"):
        session.add(Run(agent=agent, status="success", started_at=clock.at, ended_at=clock.at))
    session.add(Product(state="created", niche="n", product_type="planner", brief_json={"concept": "c"}))
    session.commit()
    from pipeline.agents.qa import QAAgent

    monkeypatch.setattr(QAAgent, "_review", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("vision down")))
    calls: list[str] = []
    w = _worker(clock, calls)
    assert w.tick() == ["qa"] and w.tick() == []  # tried once, found nothing to finish, waits
    clock.at += timedelta(minutes=10)
    assert w.tick() == []
    session.add(Product(state="created", niche="n", product_type="planner", brief_json={"concept": "d"}))  # new work arrives
    session.commit()
    assert w.tick() == ["qa"]
    clock.at += timedelta(minutes=31)
    assert w.tick() == ["qa"]  # retry window elapsed


def test_scheduled_agents_run_when_their_time_has_passed(session):
    from pipeline.db.models import Run

    clock = Clock(datetime(2026, 10, 19, 13, 5, tzinfo=UTC))  # Monday just after the 13:00 research slot
    calls: list[str] = []
    w = _worker(clock, calls)
    ran = w.tick()
    assert ran[0] == "researcher"  # due now; the daily build (14:00) is not due until later today
    assert "creator" not in ran[:1]
    agents = {r.agent for r in session.query(Run)}
    assert "researcher" in agents
    assert w.tick() == [] or "researcher" not in w.tick()  # runs once per slot
    clock.at = datetime(2026, 10, 19, 14, 1, tzinfo=UTC)
    assert "creator" in w.tick()


def test_run_now_queues_a_job_the_worker_picks_up(session, client_with_worker):
    client, clock, calls, w = client_with_worker
    from pipeline.db.models import JobRequest, JobStatus

    assert client.post("/api/dashboard/agents/analyst/run").status_code == 403  # CSRF header
    r = client.post("/api/dashboard/agents/analyst/run", headers=HDR)
    assert r.status_code == 200 and r.json()["via"] == "worker"
    again = client.post("/api/dashboard/agents/analyst/run", headers=HDR).json()
    assert again["job_id"] == r.json()["job_id"]  # collapsed onto the queued request
    assert client.post("/api/dashboard/agents/nope/run", headers=HDR).status_code == 404
    ov = client.get("/api/dashboard/overview").json()
    assert ov["worker"]["jobs"][0]["agent"] == "analyst" and ov["run_now_available"]
    assert w.tick()[0] == "analyst"
    session.expire_all()
    job = session.get(JobRequest, r.json()["job_id"])
    assert job.status == JobStatus.DONE and job.run_id
    assert client.get("/api/dashboard/overview").json()["worker"]["jobs"] == []


def test_overview_warns_when_worker_is_off(client_with_worker, monkeypatch):
    client, *_ = client_with_worker
    monkeypatch.setenv("WORKER_ENABLED", "false")
    from pipeline.config import reset_settings_cache

    reset_settings_cache()
    ov = client.get("/api/dashboard/overview").json()
    assert any("worker is off" in n["title"] for n in ov["needs_you"]) and ov["run_now_available"] is False


@pytest.fixture
def client_with_worker(monkeypatch):
    monkeypatch.setenv("WORKER_ENABLED", "true")
    from fastapi.testclient import TestClient

    from pipeline.config import reset_settings_cache

    reset_settings_cache()
    from pipeline.db.models import Run
    from pipeline.db.session import session_scope
    from pipeline.web import app as app_module

    # the app's lifespan would start a real background thread; keep ticks under the test's control
    monkeypatch.setattr("pipeline.worker.start_background_worker", lambda: None)
    clock = Clock(datetime(2026, 10, 20, 15, 0, tzinfo=UTC))
    with session_scope() as s:
        for agent in ("creator", "fulfiller", "researcher", "analyst"):
            s.add(Run(agent=agent, status="success", started_at=clock.at, ended_at=clock.at))
    calls: list[str] = []
    w = _worker(clock, calls)
    with TestClient(app_module.app) as client:
        yield client, clock, calls, w
