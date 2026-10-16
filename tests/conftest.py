"""Test fixtures: isolated SQLite DB + local storage per test, mock mode, mock LLM."""

from __future__ import annotations

import pytest


def _reset_all_caches():
    from pipeline.adapters.etsy.base import reset_etsy_cache
    from pipeline.adapters.images.base import reset_image_provider_cache
    from pipeline.adapters.printify.base import reset_printify_cache
    from pipeline.config import reset_settings_cache
    from pipeline.db.session import dispose_engine
    from pipeline.guardrails.blocklist import reset_blocklist_cache
    from pipeline.llm import reset_llm_cache
    from pipeline.prompts import load_prompt
    from pipeline.storage.base import reset_storage_cache

    reset_settings_cache()
    dispose_engine()
    reset_storage_cache()
    reset_llm_cache()
    reset_etsy_cache()
    reset_printify_cache()
    reset_image_provider_cache()
    reset_blocklist_cache()
    load_prompt.cache_clear()


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MODE", "mock")
    monkeypatch.setenv("APP_ENV", "local")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("DRY_RUN", "false")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("LOG_FORMAT", "text")
    monkeypatch.setenv("DAILY_DRAFT_CAP", "10")
    monkeypatch.setenv("WEEKLY_BRIEF_CAP", "20")
    monkeypatch.setenv("MAX_DAILY_SPEND_USD", "10")
    for k in ("ANTHROPIC_API_KEY", "ETSY_MODE", "PRINTIFY_MODE"):
        monkeypatch.delenv(k, raising=False)
    # never read a developer's .env during tests
    monkeypatch.chdir(tmp_path)
    _reset_all_caches()
    from pipeline.config import PROJECT_ROOT, get_settings
    from pipeline.db.session import create_all_for_tests

    s = get_settings()
    # config files live in the repo
    monkeypatch.setenv("NICHES_FILE", str(PROJECT_ROOT / "niches.yaml"))
    monkeypatch.setenv("BLOCKLIST_FILE", str(PROJECT_ROOT / "blocklist.txt"))
    monkeypatch.setenv("PROMPTS_DIR", str(PROJECT_ROOT / "prompts"))
    _reset_all_caches()
    s = get_settings()
    assert s.mode == "mock"
    create_all_for_tests()
    yield
    _reset_all_caches()


@pytest.fixture
def session():
    from pipeline.db import session_scope

    with session_scope() as s:
        yield s


def make_brief(session, *, niche="minimalist botanical wall art", product_type="wall_art", concept="Original test concept: fern frond", **extra):
    from pipeline.db.models import Product, ProductState

    p = Product(state=ProductState.BRIEF, niche=niche, product_type=product_type,
                brief_json={"concept": concept, "style": "minimal", "audience": "renters",
                            "keywords": ["fern", "printable"], "target_price_usd": 5.5, **extra})
    session.add(p)
    session.flush()
    return p


@pytest.fixture
def brief_factory():
    return make_brief
