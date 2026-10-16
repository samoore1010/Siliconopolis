"""Optional web service: /health, /status (JSON + HTML) and the Etsy OAuth callback."""

from __future__ import annotations

import html
import json

from fastapi import FastAPI, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse

from pipeline import __version__
from pipeline.commands.status import collect_status, format_status
from pipeline.config import get_settings
from pipeline.logging_setup import configure_logging, get_logger

settings = get_settings()
configure_logging(settings.log_level, settings.log_format)
log = get_logger("pipeline.web")

app = FastAPI(title="Etsy Agent Pipeline", version=__version__)

# In-memory PKCE verifier store for the OAuth helper flow (single owner; fine for v1).
_PKCE: dict[str, str] = {}


@app.get("/health")
def health():
    return {"ok": True, "version": __version__, "app_env": settings.app_env, "mode": settings.mode}


@app.get("/status")
def status(request: Request):
    data = collect_status()
    if "text/html" in request.headers.get("accept", "") and "application/json" not in request.headers.get("accept", ""):
        body = html.escape(format_status(data))
        return HTMLResponse(f"<html><head><title>Etsy Agent Pipeline</title></head><body style='font-family:monospace;white-space:pre;padding:2rem'>"
                            f"<h2>Etsy Agent Pipeline — {settings.app_env} / {settings.mode}</h2>{body}</body></html>")
    return JSONResponse(data)


@app.get("/oauth/etsy/start")
def oauth_start(state: str = Query("pipeline")):
    """Convenience: build the Etsy authorize URL (same logic as scripts/etsy_oauth.py)."""
    from scripts.etsy_oauth import build_authorize_url

    url, verifier = build_authorize_url(settings.etsy_api_key, settings.etsy_redirect_uri, state)
    _PKCE[state] = verifier
    return {"authorize_url": url, "state": state}


@app.get("/oauth/etsy/callback", response_class=PlainTextResponse)
def oauth_callback(code: str = Query(...), state: str = Query("pipeline")):
    from scripts.etsy_oauth import exchange_code

    verifier = _PKCE.pop(state, None)
    if not verifier:
        return PlainTextResponse("Unknown state / no PKCE verifier in memory. Start again at /oauth/etsy/start "
                                 "or use scripts/etsy_oauth.py locally.", status_code=400)
    try:
        tokens = exchange_code(settings.etsy_api_key, settings.etsy_redirect_uri, code, verifier, persist=True)
    except Exception as e:  # noqa: BLE001
        log.error("oauth exchange failed", error=str(e))
        return PlainTextResponse(f"Token exchange failed: {e}", status_code=502)
    return "Etsy OAuth complete. Tokens stored in the oauth_tokens table. You can close this tab.\n" + json.dumps(
        {"expires_in": tokens.get("expires_in"), "token_type": tokens.get("token_type")})
