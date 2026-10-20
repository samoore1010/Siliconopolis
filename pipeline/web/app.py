"""Web service: village dashboard (auth), /status (auth), /health (public), Etsy OAuth routes."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles

from pipeline import __version__
from pipeline.commands.status import collect_status, format_status
from pipeline.config import get_settings
from pipeline.logging_setup import configure_logging, get_logger
from pipeline.web.auth import require_printer, require_viewer
from pipeline.web.dashboard_api import router as dashboard_router
from pipeline.web.print_api import router as print_router
from pipeline.web.village_api import router as village_router

settings = get_settings()
configure_logging(settings.log_level, settings.log_format)
log = get_logger("pipeline.web")

STATIC_DIR = Path(__file__).resolve().parent / "static"

@asynccontextmanager
async def lifespan(_app: FastAPI):
    """The agents run from inside this process (see pipeline/worker.py) unless WORKER_ENABLED=false."""
    from pipeline.worker import start_background_worker, stop_background_worker

    start_background_worker()
    try:
        yield
    finally:
        stop_background_worker()


app = FastAPI(title="Siliconopolis — Etsy Agent Pipeline", version=__version__, lifespan=lifespan)
app.include_router(village_router)
app.include_router(dashboard_router)
app.include_router(print_router)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# In-memory PKCE verifier store for the OAuth helper flow (single owner; fine for v1).
_PKCE: dict[str, str] = {}


@app.get("/", include_in_schema=False)
def index():
    return RedirectResponse(url="/dashboard", status_code=307)


@app.get("/dashboard", response_class=HTMLResponse, dependencies=[Depends(require_viewer)])
def dashboard():
    return FileResponse(STATIC_DIR / "dashboard.html", media_type="text/html")


@app.get("/print", response_class=HTMLResponse, dependencies=[Depends(require_printer)])
def print_queue():
    return FileResponse(STATIC_DIR / "print.html", media_type="text/html")


@app.get("/village", response_class=HTMLResponse, dependencies=[Depends(require_viewer)])
def village():
    return FileResponse(STATIC_DIR / "village.html", media_type="text/html")


@app.get("/health")
def health():
    """Public liveness check for Railway. Exposes no secrets."""
    return {"ok": True, "version": __version__, "app_env": settings.app_env, "mode": settings.mode}


@app.get("/status", dependencies=[Depends(require_viewer)])
def status_route(request: Request):
    data = collect_status()
    accept = request.headers.get("accept", "")
    if "text/html" in accept and "application/json" not in accept:
        import html

        body = html.escape(format_status(data))
        return HTMLResponse("<html><head><title>Status — Siliconopolis</title></head><body style='font-family:monospace;"
                            "white-space:pre;padding:2rem;background:#0f1115;color:#e6e6e6'>"
                            f"<a href='/village' style='color:#8ab4f8'>← village</a>\n\n{body}</body></html>")
    return JSONResponse(data)


@app.get("/oauth/etsy/start", dependencies=[Depends(require_viewer)])
def oauth_start(state: str = Query("pipeline")):
    """Build the Etsy authorize URL (same logic as scripts/etsy_oauth.py)."""
    from scripts.etsy_oauth import build_authorize_url

    url, verifier = build_authorize_url(settings.etsy_api_key, settings.etsy_redirect_uri, state)
    _PKCE[state] = verifier
    return {"authorize_url": url, "state": state}


@app.get("/oauth/etsy/callback", response_class=PlainTextResponse)
def oauth_callback(code: str = Query(...), state: str = Query("pipeline")):
    """Etsy redirects the owner's browser here; it only works for a state started via /oauth/etsy/start."""
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
