import pytest


def test_postgres_url_is_normalized(monkeypatch):
    from pipeline.config import Settings

    s = Settings(DATABASE_URL="postgres://u:p@host:5432/db", APP_ENV="local")
    assert s.database_url.startswith("postgresql+psycopg://")


def test_hosted_env_rejects_sqlite():
    from pipeline.config import Settings

    with pytest.raises(ValueError, match="SQLite is not allowed"):
        Settings(APP_ENV="staging", DATABASE_URL="sqlite:///x.db", STORAGE_BACKEND="s3", S3_BUCKET="b", S3_ENDPOINT="e")


def test_hosted_env_rejects_local_storage():
    from pipeline.config import Settings

    with pytest.raises(ValueError, match="STORAGE_BACKEND=local"):
        Settings(APP_ENV="production", DATABASE_URL="postgresql://u:p@h/db", STORAGE_BACKEND="local")


def test_llm_provider_falls_back_to_mock_without_key():
    from pipeline.config import Settings

    assert Settings(ANTHROPIC_API_KEY="", LLM_PROVIDER="").effective_llm_provider == "mock"
    assert Settings(ANTHROPIC_API_KEY="sk-x", LLM_PROVIDER="").effective_llm_provider == "anthropic"


def test_adapter_mode_override():
    from pipeline.config import Settings

    s = Settings(MODE="live", ETSY_MODE="mock")
    assert s.adapter_mode("etsy") == "mock"
    assert s.adapter_mode("printify") == "live"
