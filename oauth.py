import os
from urllib.parse import urlencode

import requests

# Keyed by provider name so a second provider (Microsoft, GitHub, ...) is
# just another entry here plus a branch in fetch_userinfo() - no route or
# flow changes needed.
PROVIDERS = {
    "google": {
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://www.googleapis.com/oauth2/v3/userinfo",
        "scope": "openid email profile",
        "client_id_env": "GOOGLE_CLIENT_ID",
        "client_secret_env": "GOOGLE_CLIENT_SECRET",
    },
}


def is_configured(provider: str) -> bool:
    p = PROVIDERS[provider]
    return bool(os.environ.get(p["client_id_env"]) and os.environ.get(p["client_secret_env"]))


def build_authorize_url(provider: str, redirect_uri: str, state: str) -> str:
    p = PROVIDERS[provider]
    params = {
        "client_id": os.environ[p["client_id_env"]],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": p["scope"],
        "state": state,
        "prompt": "select_account",
    }
    return f"{p['authorize_url']}?{urlencode(params)}"


def exchange_code(provider: str, code: str, redirect_uri: str) -> str:
    """Trades a one-time authorization code for an access token. This call
    happens server-to-server over HTTPS with our client secret attached, so
    the response is trusted directly - no separate id_token signature
    verification needed on top of it."""
    p = PROVIDERS[provider]
    resp = requests.post(p["token_url"], data={
        "code": code,
        "client_id": os.environ[p["client_id_env"]],
        "client_secret": os.environ[p["client_secret_env"]],
        "redirect_uri": redirect_uri,
        "grant_type": "authorization_code",
    }, timeout=10)
    resp.raise_for_status()
    return resp.json()["access_token"]


def fetch_userinfo(provider: str, access_token: str) -> dict:
    """Returns a normalized dict: id, email, email_verified, name."""
    p = PROVIDERS[provider]
    resp = requests.get(
        p["userinfo_url"], headers={"Authorization": f"Bearer {access_token}"}, timeout=10,
    )
    resp.raise_for_status()
    data = resp.json()
    if provider == "google":
        return {
            "id": data["sub"],
            "email": data.get("email"),
            "email_verified": bool(data.get("email_verified", False)),
            "name": data.get("name"),
        }
    raise ValueError(f"no userinfo normalizer for provider {provider!r}")
