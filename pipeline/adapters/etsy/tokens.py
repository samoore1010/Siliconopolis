"""OAuth token persistence for Etsy. Etsy rotates refresh tokens, so the DB is the source of truth."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from pipeline.db import session_scope
from pipeline.db.models import OAuthToken
from pipeline.logging_setup import get_logger

log = get_logger(__name__)
PROVIDER = "etsy"


def load_tokens() -> OAuthToken | None:
    with session_scope() as s:
        return s.execute(select(OAuthToken).where(OAuthToken.provider == PROVIDER)).scalar_one_or_none()


def save_tokens(access_token: str, refresh_token: str, expires_in: int | None, scope: str | None = None) -> OAuthToken:
    with session_scope() as s:
        row = s.execute(select(OAuthToken).where(OAuthToken.provider == PROVIDER)).scalar_one_or_none()
        if row is None:
            row = OAuthToken(provider=PROVIDER, access_token=access_token, refresh_token=refresh_token)
            s.add(row)
        row.access_token = access_token
        row.refresh_token = refresh_token
        row.scope = scope or row.scope
        row.expires_at = (
            datetime.now(UTC) + timedelta(seconds=int(expires_in)) if expires_in else None
        )
        s.flush()
        log.info("oauth tokens persisted", provider=PROVIDER, expires_at=str(row.expires_at))
        return row


def seed_tokens_from_env(access_token: str, refresh_token: str) -> OAuthToken | None:
    """First-run only: copy env-provided tokens into the table if it is empty."""
    if load_tokens() is not None:
        return load_tokens()
    if not (access_token and refresh_token):
        return None
    log.info("seeding oauth_tokens from environment (first run)")
    return save_tokens(access_token, refresh_token, expires_in=0)
