"""Engine / session helpers. DATABASE_URL comes from settings (Railway injects it)."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from pipeline.config import get_settings

_engine: Engine | None = None
_factory: sessionmaker[Session] | None = None


def get_engine() -> Engine:
    global _engine
    if _engine is None:
        settings = get_settings()
        kwargs: dict = {"pool_pre_ping": True, "future": True}
        if settings.is_sqlite:
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(settings.database_url, **kwargs)
        if settings.is_sqlite:

            @event.listens_for(_engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):  # pragma: no cover
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                # SQLite has one writer at a time: while the in-app worker is mid-item, a dashboard write
                # waits for that item to commit (reads never block in WAL mode). Local dev only; Railway uses Postgres.
                cur.execute("PRAGMA busy_timeout=30000")
                cur.close()

    return _engine


def get_session_factory() -> sessionmaker[Session]:
    global _factory
    if _factory is None:
        _factory = sessionmaker(bind=get_engine(), expire_on_commit=False, future=True)
    return _factory


@contextmanager
def session_scope() -> Iterator[Session]:
    session = get_session_factory()()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def dispose_engine() -> None:
    """Close pooled connections so cron jobs exit cleanly (Railway requirement)."""
    global _engine, _factory
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _factory = None


def create_all_for_tests() -> None:
    from pipeline.db.models import Base

    Base.metadata.create_all(get_engine())
