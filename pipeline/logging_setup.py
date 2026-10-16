"""Structured JSON logging with per-run context (agent, run_id)."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
from datetime import UTC, datetime

_agent_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("agent", default=None)
_run_ctx: contextvars.ContextVar[int | None] = contextvars.ContextVar("run_id", default=None)

SECRET_KEYS = ("token", "secret", "api_key", "apikey", "password", "authorization")


def set_run_context(agent: str | None, run_id: int | None) -> None:
    _agent_ctx.set(agent)
    _run_ctx.set(run_id)


def current_agent() -> str | None:
    return _agent_ctx.get()


def current_run_id() -> int | None:
    return _run_ctx.get()


def redact(obj):
    """Recursively redact values whose key looks like a secret. Used before logging dicts."""
    if isinstance(obj, dict):
        return {
            k: ("***" if any(s in k.lower() for s in SECRET_KEYS) else redact(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [redact(v) for v in obj]
    return obj


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if _agent_ctx.get():
            payload["agent"] = _agent_ctx.get()
        if _run_ctx.get() is not None:
            payload["run_id"] = _run_ctx.get()
        extra = getattr(record, "extra_fields", None)
        if extra:
            payload.update(redact(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        prefix = ""
        if _agent_ctx.get():
            prefix = f"[{_agent_ctx.get()}#{_run_ctx.get()}] "
        base = f"{datetime.now(UTC).strftime('%H:%M:%S')} {record.levelname:<7} {prefix}{record.getMessage()}"
        extra = getattr(record, "extra_fields", None)
        if extra:
            base += " " + json.dumps(redact(extra), default=str)
        if record.exc_info:
            base += "\n" + self.formatException(record.exc_info)
        return base


class ContextAdapter(logging.LoggerAdapter):
    """logger.info("msg", count=3) -> structured extra field."""

    def process(self, msg, kwargs):
        extra_fields = {k: v for k, v in kwargs.items() if k not in ("exc_info", "stack_info", "stacklevel")}
        for k in extra_fields:
            kwargs.pop(k)
        kwargs["extra"] = {"extra_fields": extra_fields}
        return msg, kwargs


_configured = False


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    global _configured
    root = logging.getLogger()
    root.setLevel(level.upper())
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    root.addHandler(handler)
    for noisy in ("httpx", "httpcore", "botocore", "boto3", "urllib3", "PIL"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True


def get_logger(name: str) -> ContextAdapter:
    return ContextAdapter(logging.getLogger(name), {})
