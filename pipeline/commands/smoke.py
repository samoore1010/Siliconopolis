"""`python -m pipeline smoke`: connectivity checks without creating anything."""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

from pipeline.config import get_settings
from pipeline.db import get_engine
from pipeline.storage import get_storage


def run_smoke() -> tuple[bool, list[dict[str, Any]]]:
    settings = get_settings()
    results: list[dict[str, Any]] = []

    def check(name: str, fn, *, skipped_reason: str | None = None):
        if skipped_reason:
            results.append({"check": name, "ok": True, "skipped": skipped_reason})
            return
        try:
            detail = fn()
            results.append({"check": name, "ok": True, "detail": detail})
        except Exception as e:  # noqa: BLE001
            results.append({"check": name, "ok": False, "error": f"{type(e).__name__}: {e}"})

    def db():
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
            try:
                version = conn.execute(text("SELECT version_num FROM alembic_version")).scalar()
            except Exception:  # noqa: BLE001
                version = "no alembic_version table (run `alembic upgrade head`)"
        return {"url_scheme": settings.database_url.split("://")[0], "alembic": version}

    def storage():
        st = get_storage()
        st.ping()
        return {"backend": st.name}

    def anthropic():
        from pipeline.llm import get_llm

        llm = get_llm()
        llm.ping()
        return {"provider": llm.provider, "models": [settings.model_research, settings.model_worker, settings.model_cheap]}

    def etsy():
        from pipeline.adapters.etsy import get_etsy_client

        c = get_etsy_client()
        c.ping()
        return {"adapter": c.name}

    def printify():
        from pipeline.adapters.printify import get_printify_client

        c = get_printify_client()
        c.ping()
        return {"adapter": c.name}

    def images():
        from pipeline.adapters.images import get_image_provider

        p = get_image_provider()
        p.ping()
        return {"provider": p.name}

    def config_files():
        for f in (settings.niches_file, settings.blocklist_file):
            if not settings.resolve(f).exists():
                raise FileNotFoundError(f)
        return {"niches": settings.niches_file, "blocklist": settings.blocklist_file, "mode": settings.mode,
                "app_env": settings.app_env, "dry_run": settings.dry_run}

    check("config", config_files)
    check("database", db)
    check("storage", storage)
    check("anthropic", anthropic)
    check("etsy", etsy)
    check("printify", printify, skipped_reason=None if settings.adapter_mode("printify") == "live" else "mock mode")
    check("image_provider", images)
    return all(r["ok"] for r in results), results
