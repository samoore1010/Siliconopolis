"""The in-app worker: runs the agents from inside the always-on web service.

Why: with one cron service per agent, QA only looked at new products once a day, the Editor once an
hour, and "Run now" needed a Railway API token. This loop (one background thread in the web
process) makes the pipeline react instead:

- reactive agents run as soon as their queue has work: something built -> QA; approved -> Lister;
  sent to the Editor -> Editor. After a run that got nothing done (nothing claimable, Etsy not
  connected, a cap, an error) the agent waits WORKER_RETRY_MINUTES before trying again, unless new
  work arrives in the meantime;
- scheduled agents (Researcher weekly, Creator daily, Analyst weekly, Fulfiller hourly) run at the
  times in pipeline/schedule.py: if the last scheduled time has passed and no run happened since,
  it runs now (so a redeploy never skips a day);
- "Run now" from the dashboard inserts a job_requests row, picked up on the next tick.

One agent runs at a time (rendering needs Chromium/LibreOffice; serial keeps memory bounded). Caps,
row locks, draft-only and Stop are the agents' own and apply unchanged, so leftover Railway cron
services are harmless: whichever claims a row first does the work.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, or_, select

from pipeline.agents import AGENT_NAMES, get_agent
from pipeline.config import Settings, get_settings
from pipeline.db import session_scope
from pipeline.db.models import JobRequest, JobStatus, Product, Run, RunStatus, utcnow
from pipeline.logging_setup import get_logger
from pipeline.schedule import INPUT_STATE, REACTIVE_AGENTS, SCHEDULED_AGENTS, previous_run

log = get_logger("pipeline.worker")
LIVE_RUN_HOURS = 6  # an unfinished run older than this crashed and is ignored


@dataclass
class WorkerStatus:
    enabled: bool = False
    alive: bool = False
    started_at: str | None = None
    last_tick: str | None = None
    current: str | None = None  # agent running right now
    last_runs: dict[str, dict[str, Any]] = field(default_factory=dict)
    error: str | None = None


_status = WorkerStatus()


def worker_status() -> WorkerStatus:
    return _status


def request_run(session, agent: str, *, source: str = "owner", note: str = "") -> JobRequest:
    """Queue a run (dashboard "Run now"). Collapses onto an existing queued request for the same agent."""
    if agent not in AGENT_NAMES:
        raise KeyError(agent)
    existing = session.execute(select(JobRequest).where(JobRequest.agent == agent, JobRequest.status == JobStatus.QUEUED)
                               .order_by(JobRequest.id.asc())).scalars().first()
    if existing:
        return existing
    job = JobRequest(agent=agent, source=source, note=note[:500], status=JobStatus.QUEUED)
    session.add(job)
    session.flush()
    return job


class Worker:
    def __init__(self, settings: Settings | None = None, now: Callable[[], datetime] | None = None,
                 runner: Callable[[str], Any] | None = None):
        self.settings = settings or get_settings()
        self.now = now or (lambda: datetime.now(UTC))
        self.runner = runner or (lambda name: get_agent(name).run())
        # agent -> (when it last ran reactively, how many items were waiting then)
        self._reactive: dict[str, tuple[datetime, int]] = {}
        self._scheduled: dict[str, datetime] = {}  # agent -> the slot it last ran for
        self._stop = threading.Event()

    # ------------------------------------------------------------------ one tick
    def tick(self) -> list[str]:
        """Run everything that is due, one agent at a time. Returns the agents that ran."""
        ran: list[str] = []
        for _ in range(len(AGENT_NAMES) + 2):  # never spin forever within one tick
            if self._stop.is_set():
                break
            job, agent, source, slot = self._next()
            if not agent:
                break
            if slot:
                self._scheduled[agent] = slot
            self._run(agent, source, job)
            ran.append(agent)
        _status.last_tick = self.now().isoformat()
        return ran

    def _next(self) -> tuple[int | None, str | None, str, datetime | None]:
        now = self.now()
        with session_scope() as s:
            busy = self._running_agents(s, now)
            job = s.execute(select(JobRequest).where(JobRequest.status == JobStatus.QUEUED)
                            .order_by(JobRequest.id.asc())).scalars().first()
            if job and job.agent not in busy:
                return job.id, job.agent, "owner", None
            for agent in REACTIVE_AGENTS:
                if agent in busy:
                    continue
                waiting = self._waiting(s, agent)
                if not waiting:
                    continue
                last = self._reactive.get(agent)
                if last and now - last[0] < timedelta(minutes=self.settings.worker_retry_minutes) and waiting <= last[1]:
                    continue  # it found nothing to do a moment ago and nothing new arrived
                return None, agent, "queue", None
            for agent in SCHEDULED_AGENTS:
                if agent in busy:
                    continue
                due = previous_run(agent, now)
                mine = self._scheduled.get(agent)
                if mine is not None:
                    if mine >= due:
                        continue  # this process already ran it for this slot
                elif s.execute(select(func.count(Run.id)).where(Run.agent == agent, Run.started_at >= due)).scalar_one():
                    continue  # after a restart: a run recorded since the slot (by anyone) counts
                return None, agent, "schedule", due
        return None, None, "", None

    @staticmethod
    def _running_agents(s, now: datetime) -> set[str]:
        since = now - timedelta(hours=LIVE_RUN_HOURS)
        rows = s.execute(select(Run.agent).where(Run.ended_at.is_(None), Run.status == RunStatus.RUNNING,
                                                 Run.started_at >= since)).all()
        return {r[0] for r in rows}

    def _waiting(self, s, agent: str) -> int:
        state = INPUT_STATE.get(agent)
        if not state:
            return 0
        stale = self.now() - timedelta(minutes=self.settings.lock_timeout_minutes)
        return int(s.execute(select(func.count(Product.id)).where(
            Product.state == state, or_(Product.locked_by.is_(None), Product.locked_at < stale))).scalar_one())

    def _run(self, agent: str, source: str, job_id: int | None) -> None:
        waiting_before = 0
        with session_scope() as s:
            waiting_before = self._waiting(s, agent)
            if job_id:
                job = s.get(JobRequest, job_id)
                if job:
                    job.status, job.started_at = JobStatus.RUNNING, utcnow()
        _status.current = agent
        log.info("worker starting agent", agent=agent, source=source)
        outcome, error = None, None
        try:
            outcome = self.runner(agent)
        except Exception as e:  # noqa: BLE001 - one bad run must not kill the worker
            error = f"{type(e).__name__}: {e}"
            log.error("worker: agent crashed", agent=agent, error=error)
        finally:
            _status.current = None
        if source == "queue" or (outcome is not None and getattr(outcome, "items_processed", 0) == 0):
            # remember what the queue looked like so a fruitless run isn't repeated every tick
            self._reactive[agent] = (self.now(), waiting_before if outcome is None or outcome.items_processed == 0 else 0)
        if outcome is not None and outcome.items_processed > 0:
            self._reactive.pop(agent, None)
        _status.last_runs[agent] = {"at": self.now().isoformat(), "source": source,
                                    "status": getattr(outcome, "status", None) or "crashed",
                                    "items": getattr(outcome, "items_processed", 0), "error": error}
        with session_scope() as s:
            if job_id and (job := s.get(JobRequest, job_id)):
                job.status = JobStatus.DONE if outcome is not None and outcome.exit_code != 1 else JobStatus.FAILED
                job.finished_at, job.run_id = utcnow(), getattr(outcome, "run_id", None)
                job.note = (error or (outcome.errors[0] if outcome and outcome.errors else ""))[:500]

    # ------------------------------------------------------------------ loop
    def run_forever(self) -> None:
        _status.enabled = _status.alive = True
        _status.started_at = self.now().isoformat()
        log.info("worker started", poll_seconds=self.settings.worker_poll_seconds)
        try:
            while not self._stop.is_set():
                try:
                    self.tick()
                    _status.error = None
                except Exception as e:  # noqa: BLE001
                    _status.error = f"{type(e).__name__}: {e}"
                    log.error("worker tick failed", error=_status.error)
                self._stop.wait(self.settings.worker_poll_seconds)
        finally:
            _status.alive = False
            log.info("worker stopped")

    def stop(self) -> None:
        self._stop.set()


_thread: threading.Thread | None = None
_worker: Worker | None = None


def start_background_worker() -> Worker | None:
    """Start the loop in a daemon thread (called by the web app on startup). No-op when disabled."""
    global _thread, _worker
    settings = get_settings()
    _status.enabled = settings.worker_enabled
    if not settings.worker_enabled or (_thread and _thread.is_alive()):
        return _worker
    _worker = Worker(settings)
    _thread = threading.Thread(target=_worker.run_forever, name="pipeline-worker", daemon=True)
    _thread.start()
    return _worker


def stop_background_worker(timeout: float = 5.0) -> None:
    if _worker:
        _worker.stop()
    if _thread and _thread.is_alive():
        _thread.join(timeout)


def run_standalone() -> int:
    """`python -m pipeline worker`: the same loop as a separate always-on process."""
    from pipeline.db.session import dispose_engine

    w = Worker()
    try:
        w.run_forever()
    except KeyboardInterrupt:
        pass
    finally:
        dispose_engine()
    return 0


