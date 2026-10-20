"""HTTP basic auth for the village, status and OAuth-start routes. /health stays public."""

from __future__ import annotations

import secrets

from fastapi import Depends, Header, HTTPException, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from pipeline.config import get_settings

_basic = HTTPBasic(auto_error=False)


def require_write(x_siliconopolis: str | None = Header(None)) -> None:
    """Every state-changing route needs this custom header. A cross-site form cannot send it, so a
    logged-in browser cannot be tricked into posting (basic-auth credentials are sent automatically)."""
    if x_siliconopolis != "1":
        raise HTTPException(403, "missing X-Siliconopolis header")


def require_viewer(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> str:
    settings = get_settings()
    if not settings.village_auth_configured:
        if settings.app_env == "local":
            return "local"  # open on a developer machine
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Village auth is not configured. Set VILLAGE_USER and VILLAGE_PASSWORD on the web service.",
        )
    if credentials is None or not (
        secrets.compare_digest(credentials.username.encode(), settings.village_user.encode())
        and secrets.compare_digest(credentials.password.encode(), settings.village_password.encode())
    ):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized",
                            headers={"WWW-Authenticate": 'Basic realm="Siliconopolis village"'})
    return credentials.username


def _matches(credentials: HTTPBasicCredentials | None, user: str, password: str) -> bool:
    return bool(credentials and user and password
                and secrets.compare_digest(credentials.username.encode(), user.encode())
                and secrets.compare_digest(credentials.password.encode(), password.encode()))


def require_printer(credentials: HTTPBasicCredentials | None = Depends(_basic)) -> str:
    """The print shop's login (PRINT_USER/PRINT_PASSWORD) or the owner's. Returns "owner" or "printer"."""
    settings = get_settings()
    if not (settings.village_auth_configured or settings.print_auth_configured):
        if settings.app_env == "local":
            return "owner"
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="Set PRINT_USER/PRINT_PASSWORD (and VILLAGE_USER/VILLAGE_PASSWORD) on the web service.")
    if _matches(credentials, settings.village_user, settings.village_password):
        return "owner"
    if _matches(credentials, settings.print_user, settings.print_password):
        return "printer"
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Unauthorized",
                        headers={"WWW-Authenticate": 'Basic realm="Siliconopolis print queue"'})
