#!/usr/bin/env python
"""One-time Etsy OAuth 2.0 (PKCE) helper.

Usage (locally, after setting ETSY_API_KEY and ETSY_REDIRECT_URI in .env):

    python scripts/etsy_oauth.py            # prints an authorize URL, waits for the code, stores tokens
    python scripts/etsy_oauth.py --no-db    # only print tokens (paste into Railway variables to seed)

Flow: https://developers.etsy.com/documentation/essentials/authentication
  1. Open the printed URL, sign in, grant scopes.
  2. Etsy redirects to ETSY_REDIRECT_URI?code=...&state=...
  3. Paste the `code` here; we exchange it for access+refresh tokens and persist them in oauth_tokens.

TODO(owner): confirm scopes and the redirect URI registered in the Etsy developer app match.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import secrets
import sys
import urllib.parse
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

AUTH_URL = "https://www.etsy.com/oauth/connect"
TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
# transactions_w: send tracking numbers for in-house (printed) orders back to Etsy
SCOPES = ["listings_r", "listings_w", "listings_d", "shops_r", "transactions_r", "transactions_w"]


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def build_authorize_url(api_key: str, redirect_uri: str, state: str | None = None) -> tuple[str, str]:
    verifier, challenge = _pkce_pair()
    params = {
        "response_type": "code",
        "client_id": api_key,
        "redirect_uri": redirect_uri,
        "scope": " ".join(SCOPES),
        "state": state or secrets.token_urlsafe(16),
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return f"{AUTH_URL}?{urllib.parse.urlencode(params)}", verifier


def exchange_code(api_key: str, redirect_uri: str, code: str, verifier: str, *, persist: bool) -> dict:
    resp = httpx.post(TOKEN_URL, data={"grant_type": "authorization_code", "client_id": api_key,
                                       "redirect_uri": redirect_uri, "code": code, "code_verifier": verifier}, timeout=30)
    if resp.status_code >= 400:
        raise RuntimeError(f"token exchange failed: {resp.status_code} {resp.text[:300]}")
    tokens = resp.json()
    if persist:
        from pipeline.adapters.etsy.tokens import save_tokens

        save_tokens(tokens["access_token"], tokens["refresh_token"], tokens.get("expires_in"), " ".join(SCOPES))
    return tokens


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-db", action="store_true", help="do not persist tokens, just print them")
    args = ap.parse_args()

    from pipeline.config import get_settings

    s = get_settings()
    if not s.etsy_api_key:
        print("ETSY_API_KEY is not set", file=sys.stderr)
        return 2
    url, verifier = build_authorize_url(s.etsy_api_key, s.etsy_redirect_uri)
    print("\n1) Open this URL in a browser and approve access:\n\n" + url + "\n")
    print(f"2) After approving, Etsy redirects to {s.etsy_redirect_uri}?code=...")
    code = input("3) Paste the `code` query parameter here: ").strip()
    if not code:
        return 2
    tokens = exchange_code(s.etsy_api_key, s.etsy_redirect_uri, code, verifier, persist=not args.no_db)
    print("\nSuccess. Tokens:")
    print(f"  ETSY_OAUTH_ACCESS_TOKEN={tokens['access_token']}")
    print(f"  ETSY_OAUTH_REFRESH_TOKEN={tokens['refresh_token']}")
    print(f"  expires_in={tokens.get('expires_in')}s")
    if args.no_db:
        print("\nPaste these into Railway variables; they seed the oauth_tokens table on first live run.")
    else:
        print("\nStored in the oauth_tokens table. Do NOT commit these values anywhere.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
