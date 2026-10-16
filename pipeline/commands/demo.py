"""`python -m pipeline demo`: mock-mode loop so the village can be watched animating. Never in production."""

from __future__ import annotations

import time

from sqlalchemy import select

from pipeline.config import get_settings
from pipeline.db import session_scope
from pipeline.db.models import Product
from pipeline.logging_setup import get_logger

log = get_logger("pipeline.demo")


class DemoRefused(RuntimeError):
    pass


def check_demo_allowed() -> None:
    s = get_settings()
    if s.app_env == "production":
        raise DemoRefused("demo refuses to run when APP_ENV=production")
    if s.mode != "mock" or s.adapter_mode("etsy") != "mock":
        raise DemoRefused("demo only runs in MODE=mock (Etsy adapter must be mock)")


def run_demo(*, cycles: int = 3, sleep_seconds: float = 4.0, reset: bool = False) -> int:
    """Seed briefs (researcher) then loop creator -> qa -> lister -> analyst with short pauses."""
    from pipeline.agents import get_agent
    from pipeline.db.session import dispose_engine

    check_demo_allowed()
    try:
        if reset:
            with session_scope() as s:
                from pipeline.db.models import Event, ResearchNote, Run

                for model in (Event, Run, ResearchNote, Product):
                    s.query(model).delete()
            log.info("demo: database reset")
        with session_scope() as s:
            briefs = s.execute(select(Product).where(Product.state == "brief")).scalars().all()
        if not briefs:
            get_agent("researcher").run()
            time.sleep(sleep_seconds)
        for cycle in range(1, cycles + 1):
            log.info("demo cycle", cycle=cycle, of=cycles)
            for name in ("creator", "qa", "lister"):
                outcome = get_agent(name).run()
                log.info("demo step", agent=name, status=outcome.status, items=outcome.items_processed)
                time.sleep(sleep_seconds)
            if cycle % 2 == 0 or cycle == cycles:
                get_agent("analyst").run()
                time.sleep(sleep_seconds)
            with session_scope() as s:
                remaining = s.execute(select(Product).where(Product.state == "brief")).scalars().all()
            if not remaining and cycle < cycles:
                get_agent("researcher").run()
                time.sleep(sleep_seconds)
        return 0
    finally:
        dispose_engine()
