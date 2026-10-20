"""Application settings loaded from environment variables / .env via pydantic-settings."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Mode = Literal["mock", "live"]
AppEnv = Literal["local", "staging", "production"]

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # global
    mode: Mode = Field("mock", alias="MODE")
    app_env: AppEnv = Field("local", alias="APP_ENV")
    dry_run: bool = Field(False, alias="DRY_RUN")
    log_level: str = Field("INFO", alias="LOG_LEVEL")
    log_format: Literal["json", "text"] = Field("json", alias="LOG_FORMAT")

    # anthropic
    anthropic_api_key: str = Field("", alias="ANTHROPIC_API_KEY")
    model_research: str = Field("claude-opus-5-5", alias="MODEL_RESEARCH")
    model_worker: str = Field("claude-sonnet-5-5", alias="MODEL_WORKER")
    model_cheap: str = Field("claude-haiku-5-5", alias="MODEL_CHEAP")
    # the Editor polishes existing products with a stronger model than the Creator
    model_editor: str = Field("claude-opus-5-5", alias="MODEL_EDITOR")
    # an edit that costs more than this (LLM + images, this product, this run) is abandoned, not retried
    editor_max_cost_per_product_usd: float = Field(3.0, alias="EDITOR_MAX_COST_PER_PRODUCT_USD")
    llm_provider: Literal["", "anthropic", "mock"] = Field("", alias="LLM_PROVIDER")
    # USD per million tokens, "model_prefix=input/output;..." (owner may tune).
    # TODO(owner): confirm current list prices at https://www.anthropic.com/pricing
    llm_pricing: str = Field(
        "claude-opus=15/75;claude-sonnet=3/15;claude-haiku=0.8/4", alias="LLM_PRICING"
    )

    # database
    database_url: str = Field("sqlite:///./pipeline.db", alias="DATABASE_URL")

    # storage
    storage_backend: Literal["local", "s3"] = Field("local", alias="STORAGE_BACKEND")
    storage_dir: str = Field("./storage", alias="STORAGE_DIR")
    s3_endpoint: str = Field("", alias="S3_ENDPOINT")
    s3_bucket: str = Field("", alias="S3_BUCKET")
    s3_region: str = Field("auto", alias="S3_REGION")
    s3_access_key_id: str = Field("", alias="S3_ACCESS_KEY_ID")
    s3_secret_access_key: str = Field("", alias="S3_SECRET_ACCESS_KEY")

    # etsy
    etsy_mode: Literal["", "mock", "live"] = Field("", alias="ETSY_MODE")
    etsy_api_key: str = Field("", alias="ETSY_API_KEY")
    etsy_shared_secret: str = Field("", alias="ETSY_SHARED_SECRET")
    etsy_shop_id: str = Field("", alias="ETSY_SHOP_ID")
    etsy_oauth_access_token: str = Field("", alias="ETSY_OAUTH_ACCESS_TOKEN")
    etsy_oauth_refresh_token: str = Field("", alias="ETSY_OAUTH_REFRESH_TOKEN")
    etsy_redirect_uri: str = Field(
        "http://localhost:8000/oauth/etsy/callback", alias="ETSY_REDIRECT_URI"
    )
    etsy_rate_limit_per_sec: float = Field(4.0, alias="ETSY_RATE_LIMIT_PER_SEC")
    # physical (in-house) listings: set these from Etsy Shop Manager before the Lister can draft them
    etsy_shipping_profile_id: str = Field("", alias="ETSY_SHIPPING_PROFILE_ID")
    etsy_production_partner_id: str = Field("", alias="ETSY_PRODUCTION_PARTNER_ID")
    etsy_readiness_state_id: str = Field("", alias="ETSY_READINESS_STATE_ID")
    etsy_return_policy_id: str = Field("", alias="ETSY_RETURN_POLICY_ID")

    # printify
    printify_mode: Literal["", "mock", "live"] = Field("", alias="PRINTIFY_MODE")
    printify_api_token: str = Field("", alias="PRINTIFY_API_TOKEN")
    printify_shop_id: str = Field("", alias="PRINTIFY_SHOP_ID")

    # images
    image_provider: str = Field("mock", alias="IMAGE_PROVIDER")
    image_api_key: str = Field("", alias="IMAGE_API_KEY")
    image_model: str = Field("gpt-image-1.5", alias="IMAGE_MODEL")
    # IMAGE_EDIT_MODEL: the model for image EDITS (restaging a print as a photo, the Editor's artwork
    # edits), where preserving the exact design matters; empty = same as IMAGE_MODEL
    image_edit_model: str = Field("", alias="IMAGE_EDIT_MODEL")
    image_quality: Literal["low", "medium", "high", "xhigh", "max"] = Field("high", alias="IMAGE_QUALITY")
    # lifestyle mockup backgrounds (cached and reused, so this is a one-off cost per scene)
    mockup_scenes: bool = Field(True, alias="MOCKUP_SCENES")
    mockup_quality: Literal["low", "medium", "high", "xhigh", "max"] = Field("medium", alias="MOCKUP_QUALITY")

    # QA bars (1-10): every score must reach QA_MIN_SCORE; design_polish and commercial_viability must
    # reach QA_MIN_COMMERCIAL_SCORE. 8 = "would list as-is"; 7 = "minor polish away"; start at 7.
    qa_min_score: int = Field(6, alias="QA_MIN_SCORE")
    qa_min_commercial_score: int = Field(7, alias="QA_MIN_COMMERCIAL_SCORE")

    # caps
    daily_draft_cap: int = Field(10, alias="DAILY_DRAFT_CAP")
    weekly_brief_cap: int = Field(20, alias="WEEKLY_BRIEF_CAP")
    max_daily_spend_usd: float = Field(10.0, alias="MAX_DAILY_SPEND_USD")
    lock_timeout_minutes: int = Field(120, alias="LOCK_TIMEOUT_MINUTES")

    # config files
    niches_file: str = Field("niches.yaml", alias="NICHES_FILE")
    catalog_file: str = Field("catalog.yaml", alias="CATALOG_FILE")
    blocklist_file: str = Field("blocklist.txt", alias="BLOCKLIST_FILE")
    prompts_dir: str = Field("prompts", alias="PROMPTS_DIR")

    # in-app worker: the web service runs the agents itself (reacting to queues, keeping the schedule,
    # and honouring "Run now"), so no cron services or Railway API token are needed
    worker_enabled: bool = Field(True, alias="WORKER_ENABLED")
    worker_poll_seconds: int = Field(15, alias="WORKER_POLL_SECONDS")
    worker_retry_minutes: int = Field(30, alias="WORKER_RETRY_MINUTES")

    # web / village
    port: int = Field(8000, alias="PORT")
    village_user: str = Field("", alias="VILLAGE_USER")
    village_password: str = Field("", alias="VILLAGE_PASSWORD")
    # separate login for the print shop's order queue (/print); it cannot see the rest of the dashboard
    print_user: str = Field("", alias="PRINT_USER")
    print_password: str = Field("", alias="PRINT_PASSWORD")
    # Optional "Run now": Railway API token + JSON map {"researcher": "<serviceId>", "daily": "...", "analyst": "..."}
    railway_api_token: str = Field("", alias="RAILWAY_API_TOKEN")
    railway_environment_id: str = Field("", alias="RAILWAY_ENVIRONMENT_ID")
    railway_service_ids: str = Field("", alias="RAILWAY_SERVICE_IDS")

    # ------------------------------------------------------------------ validators
    @field_validator("database_url")
    @classmethod
    def _normalize_db_url(cls, v: str) -> str:
        # Railway injects postgresql://; SQLAlchemy 2 + psycopg3 wants postgresql+psycopg://
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://"):]
        return v

    @model_validator(mode="after")
    def _guard_hosted_envs(self) -> Settings:
        if self.app_env != "local":
            if self.database_url.startswith("sqlite"):
                raise ValueError(
                    "SQLite is not allowed when APP_ENV != local: the container filesystem is "
                    "wiped on redeploy. Set DATABASE_URL to the Railway Postgres URL."
                )
            if self.storage_backend == "local":
                raise ValueError(
                    "STORAGE_BACKEND=local is not allowed when APP_ENV != local. Use s3."
                )
        if self.storage_backend == "s3" and not (self.s3_bucket and self.s3_endpoint):
            raise ValueError("STORAGE_BACKEND=s3 requires S3_BUCKET and S3_ENDPOINT")
        return self

    # ------------------------------------------------------------------ helpers
    @property
    def village_auth_configured(self) -> bool:
        return bool(self.village_user and self.village_password)

    @property
    def print_auth_configured(self) -> bool:
        return bool(self.print_user and self.print_password)

    @property
    def railway_run_now_enabled(self) -> bool:
        return bool(self.railway_api_token and self.railway_service_ids)

    @property
    def run_now_enabled(self) -> bool:
        """"Run now" works through the in-app worker, or (legacy) by re-running a Railway cron service."""
        return self.worker_enabled or self.railway_run_now_enabled

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @property
    def effective_llm_provider(self) -> str:
        if self.llm_provider:
            return self.llm_provider
        return "anthropic" if self.anthropic_api_key else "mock"

    def adapter_mode(self, adapter: Literal["etsy", "printify"]) -> Mode:
        override = getattr(self, f"{adapter}_mode")
        return override or self.mode  # type: ignore[return-value]

    def resolve(self, relative: str) -> Path:
        p = Path(relative)
        return p if p.is_absolute() else PROJECT_ROOT / p

    def pricing_table(self) -> dict[str, tuple[float, float]]:
        table: dict[str, tuple[float, float]] = {}
        for part in self.llm_pricing.split(";"):
            part = part.strip()
            if not part or "=" not in part:
                continue
            prefix, prices = part.split("=", 1)
            inp, out = prices.split("/")
            table[prefix.strip()] = (float(inp), float(out))
        return table


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


def reset_settings_cache() -> None:
    get_settings.cache_clear()
