"""Agent base: one Run row per invocation, structured logs, cost accounting, cap enforcement."""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from pipeline.config import Settings, get_settings
from pipeline.db import session_scope
from pipeline.db.events import EventKind, prune_events, record_event
from pipeline.db.models import Run, RunStatus
from pipeline.guardrails.caps import CapReached, check_spend_cap
from pipeline.llm import ImageInput, LLMClient, LLMResult, get_llm
from pipeline.logging_setup import get_logger, set_run_context
from pipeline.prompts import load_prompt


@dataclass
class AgentOutcome:
    agent: str
    run_id: int
    status: str
    items_processed: int
    cost_usd: float
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.status == RunStatus.SUCCESS

    @property
    def exit_code(self) -> int:
        return 0 if self.ok else (3 if self.status == RunStatus.CAP_REACHED else 1)


class Agent:
    name: str = "agent"
    prompt_file: str = "researcher"

    def __init__(self, settings: Settings | None = None, llm: LLMClient | None = None):
        self.settings = settings or get_settings()
        self.llm = llm or get_llm()
        self.log = get_logger(f"pipeline.agents.{self.name}")
        self.run_id: int | None = None
        self._run_cost = 0.0
        self._usage: dict[str, Any] = {"llm_calls": 0, "input_tokens": 0, "output_tokens": 0, "images": 0}
        self.errors: list[str] = []
        self.items_processed = 0

    # ------------------------------------------------------------------ lifecycle
    def run(self) -> AgentOutcome:
        with session_scope() as s:
            run = Run(agent=self.name, status=RunStatus.RUNNING)
            s.add(run)
            s.flush()
            self.run_id = run.id
            set_run_context(self.name, self.run_id)
            record_event(s, kind=EventKind.RUN_STARTED, message=f"{self.name} run #{self.run_id} started")
            pruned = prune_events(s)
            if pruned:
                self.log.info("pruned old events", count=pruned)
        self.log.info("run started", mode=self.settings.mode, dry_run=self.settings.dry_run,
                      app_env=self.settings.app_env, llm=self.llm.provider)
        status = RunStatus.SUCCESS
        try:
            with session_scope() as s:
                self.execute(s)
        except CapReached as e:
            status = RunStatus.CAP_REACHED
            self.errors.append(str(e))
            self.log.warning("CAP REACHED - stopping run", cap=e.cap, limit=e.limit, current=e.current)
            self._event(EventKind.CAP_REACHED, str(e))
        except Exception as e:  # noqa: BLE001
            status = RunStatus.FAILED
            self.errors.append(f"{type(e).__name__}: {e}")
            self.log.error("run failed", error=str(e), trace=traceback.format_exc())
            self._event(EventKind.ERROR, f"{type(e).__name__}: {e}")
        finally:
            with session_scope() as s:
                run = s.get(Run, self.run_id)
                if run:
                    run.ended_at = datetime.now(UTC)
                    run.status = status
                    run.items_processed = self.items_processed
                    run.errors_json = self.errors
                    run.cost_usd = round(self._run_cost, 6)
                    run.usage_json = self._usage
                record_event(s, kind=EventKind.RUN_FINISHED, cost_usd=self._run_cost,
                             message=f"{self.name} run #{self.run_id} {status}: {self.items_processed} items, "
                                     f"${self._run_cost:.4f}, {len(self.errors)} errors")
            self.log.info("run finished", status=status, items=self.items_processed,
                          cost_usd=round(self._run_cost, 4), errors=len(self.errors))
            set_run_context(None, None)
        return AgentOutcome(self.name, self.run_id or -1, status, self.items_processed, self._run_cost, self.errors)

    def execute(self, session: Session) -> None:  # pragma: no cover - abstract
        raise NotImplementedError

    # ------------------------------------------------------------------ helpers
    def record_cost(self, session: Session, usd: float, *, kind: str = "llm") -> None:
        self._run_cost += usd
        if kind == "image":
            self._usage["images"] += 1
        # Persist incrementally so a crash mid-run still reflects spend.
        run = session.get(Run, self.run_id)
        if run:
            run.cost_usd = round(self._run_cost, 6)
            run.usage_json = dict(self._usage)
        session.flush()
        check_spend_cap(session, self.settings.max_daily_spend_usd, 0.0, None)

    def ask(self, session: Session, *, task: str, model: str, user: str, images: list[ImageInput] | None = None,
            max_tokens: int = 8192, temperature: float = 0.7) -> LLMResult:
        # check cap before spending
        check_spend_cap(session, self.settings.max_daily_spend_usd, 0.0, None)
        result = self.llm.complete(model=model, system=load_prompt(self.prompt_file), user=user, task=task,
                                   images=images, max_tokens=max_tokens, temperature=temperature)
        self._usage["llm_calls"] += 1
        self._usage["input_tokens"] += result.input_tokens
        self._usage["output_tokens"] += result.output_tokens
        self.record_cost(session, result.cost_usd)
        return result

    def note_error(self, msg: str, **fields: Any) -> None:
        self.errors.append(msg)
        self.log.error(msg, **fields)
        self._event(EventKind.ERROR, msg, product_id=fields.get("product_id"))

    def _event(self, kind: str, message: str, product_id: int | None = None) -> None:
        """Write an event in its own transaction so it survives a rolled-back run."""
        try:
            with session_scope() as s:
                record_event(s, kind=kind, message=message, product_id=product_id)
        except Exception:  # noqa: BLE001 - never let telemetry break a run
            self.log.warning("could not write event", kind=kind)
