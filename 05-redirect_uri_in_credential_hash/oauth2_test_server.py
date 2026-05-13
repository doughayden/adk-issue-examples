"""
Weather API OAuth2 Test Server — adapted from ADK contributing sample.

Source: https://github.com/google/adk-python/tree/main/contributing/samples/oauth2_client_credentials

Adaptations for reproducing ADK OAuth2 bugs (kept minimal — server structure,
naming, and handler signatures match the upstream sample as closely as
possible):

1. ``refresh_tokens`` in-memory store and a new ``handle_refresh_token``
   handler with refresh_token rotation. Rotation is standard security
   practice (Salesforce, many OIDC providers) and makes the
   "refreshed credentials not persisted" bug visible on the second refresh.
2. ``STRICT_SCOPE_REJECTION`` env-var toggle: when enabled, the refresh
   handler returns 400 on requests that include a ``scope`` parameter.
   Mimics Salesforce behavior for the "scope parameter not supported"
   bug.

Usage:
    python oauth2_test_server.py
    STRICT_SCOPE_REJECTION=1 python oauth2_test_server.py   # enable scope-reject mode

Endpoints:
    GET  /auth                 - Authorization endpoint (auth code flow)
    POST /token                - Token endpoint (client credentials, auth code, refresh)
    GET  /.well-known/openid_configuration - OpenID Connect discovery
    GET  /api/weather          - Weather API (requires Bearer token)
"""

import os
import secrets
import time

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import BaseModel

# Toggle via env var: STRICT_SCOPE_REJECTION=1 → refresh requests containing
# a `scope` parameter return 400 (Salesforce behavior).
STRICT_SCOPE_REJECTION = os.environ.get("STRICT_SCOPE_REJECTION") == "1"

app = FastAPI(title="Weather API OAuth2 Server", version="1.0.0")

# In-memory storage (for testing only)
clients = {
    "test_client": {
        "client_secret": "test_secret",  # noqa: S106
        "redirect_uris": [
            "http://localhost:8080/callback",
            "urn:ietf:wg:oauth:2.0:oob",
        ],
        "scopes": ["read", "write", "admin"],
    }
}

authorization_codes: dict = {}  # code -> {client_id, redirect_uri, scope, expires_at}
access_tokens: dict = {}  # token -> {client_id, scope, expires_at, token_type}
refresh_tokens: dict = {}  # refresh_token -> {client_id, scope}


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "Bearer"  # noqa: S105
    expires_in: int = 3600
    refresh_token: str | None = None
    scope: str | None = None


@app.get("/.well-known/openid_configuration")
async def openid_configuration():
    """OpenID Connect Discovery endpoint."""
    return {
        "issuer": "http://localhost:8080",
        "authorization_endpoint": "http://localhost:8080/auth",
        "token_endpoint": "http://localhost:8080/token",
        "userinfo_endpoint": "http://localhost:8080/userinfo",
        "revocation_endpoint": "http://localhost:8080/revoke",
        "scopes_supported": ["openid", "read", "write", "admin"],
        "response_types_supported": ["code"],
        "grant_types_supported": [
            "authorization_code",
            "client_credentials",
            "refresh_token",
        ],
        "token_endpoint_auth_methods_supported": [
            "client_secret_basic",
            "client_secret_post",
        ],
        "subject_types_supported": ["public"],
    }


@app.get("/auth")
async def authorize(
    response_type: str = Query(...),
    client_id: str = Query(...),
    redirect_uri: str = Query(...),
    scope: str = Query(default="read"),
    state: str = Query(default=""),
):
    """Authorization endpoint for OAuth2 authorization code flow."""

    if client_id not in clients:
        raise HTTPException(status_code=400, detail="Invalid client_id")

    client = clients[client_id]
    if redirect_uri not in client["redirect_uris"]:
        raise HTTPException(status_code=400, detail="Invalid redirect_uri")

    if response_type != "code":
        raise HTTPException(status_code=400, detail="Unsupported response_type")

    auth_code = secrets.token_urlsafe(32)
    authorization_codes[auth_code] = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scope": scope,
        "expires_at": time.time() + 600,  # 10 minutes
    }

    params = f"code={auth_code}"
    if state:
        params += f"&state={state}"

    return RedirectResponse(url=f"{redirect_uri}?{params}")


@app.post("/token")
async def token_endpoint(
    request: Request,
    grant_type: str = Form(...),
    client_id: str = Form(default=None),
    client_secret: str = Form(default=None),
    code: str = Form(default=None),
    redirect_uri: str = Form(default=None),
    refresh_token: str = Form(default=None),
    scope: str = Form(default=None),
):
    """Token endpoint for client credentials, authorization code, and refresh flows."""

    # Support both HTTP Basic auth and form-based client authentication
    auth_header = request.headers.get("Authorization")

    if auth_header and auth_header.startswith("Basic "):
        import base64

        try:
            encoded_credentials = auth_header[6:]
            decoded = base64.b64decode(encoded_credentials).decode("utf-8")
            basic_client_id, basic_client_secret = decoded.split(":", 1)
            client_id = client_id or basic_client_id
            client_secret = client_secret or basic_client_secret
        except Exception as exc:
            raise HTTPException(
                status_code=401, detail="Invalid authorization header"
            ) from exc

    if not client_id or not client_secret:
        raise HTTPException(status_code=400, detail="Client credentials required")

    if client_id not in clients:
        raise HTTPException(status_code=401, detail="Invalid client")

    client = clients[client_id]
    if client["client_secret"] != client_secret:
        raise HTTPException(status_code=401, detail="Invalid client credentials")

    if grant_type == "client_credentials":
        return await handle_client_credentials(client_id, scope or "read")
    if grant_type == "authorization_code":
        return await handle_authorization_code(client_id, code, redirect_uri)
    if grant_type == "refresh_token":
        return await handle_refresh_token(client_id, refresh_token, scope)
    raise HTTPException(status_code=400, detail="Unsupported grant_type")


async def handle_client_credentials(client_id: str, scope: str) -> TokenResponse:
    """Handle client credentials flow."""

    access_token = secrets.token_urlsafe(32)
    expires_at = time.time() + 3600

    access_tokens[access_token] = {
        "client_id": client_id,
        "scope": scope,
        "expires_at": expires_at,
        "token_type": "Bearer",
    }

    return TokenResponse(
        access_token=access_token,
        token_type="Bearer",  # noqa: S106
        expires_in=3600,
        scope=scope,
    )


async def handle_authorization_code(
    client_id: str, code: str, redirect_uri: str
) -> TokenResponse:
    """Handle authorization code flow."""

    if not code:
        raise HTTPException(status_code=400, detail="Missing authorization code")

    if code not in authorization_codes:
        raise HTTPException(status_code=400, detail="Invalid authorization code")

    auth_data = authorization_codes[code]

    if time.time() > auth_data["expires_at"]:
        del authorization_codes[code]
        raise HTTPException(status_code=400, detail="Authorization code expired")

    if auth_data["client_id"] != client_id:
        raise HTTPException(status_code=400, detail="Client mismatch")

    if redirect_uri and auth_data["redirect_uri"] != redirect_uri:
        raise HTTPException(status_code=400, detail="Redirect URI mismatch")

    access_token = secrets.token_urlsafe(32)
    refresh_token_value = secrets.token_urlsafe(32)
    expires_at = time.time() + 3600

    access_tokens[access_token] = {
        "client_id": client_id,
        "scope": auth_data["scope"],
        "expires_at": expires_at,
        "token_type": "Bearer",
    }
    refresh_tokens[refresh_token_value] = {
        "client_id": client_id,
        "scope": auth_data["scope"],
    }

    del authorization_codes[code]

    return TokenResponse(
        access_token=access_token,
        token_type="Bearer",  # noqa: S106
        expires_in=3600,
        refresh_token=refresh_token_value,
        scope=auth_data["scope"],
    )


async def handle_refresh_token(
    client_id: str, refresh_token_value: str | None, scope: str | None
) -> TokenResponse:
    """Handle refresh_token grant with rotation (and optional scope rejection)."""

    # Mimic Salesforce: reject refresh requests that include a `scope` parameter.
    if STRICT_SCOPE_REJECTION and scope:
        raise HTTPException(
            status_code=400,
            detail="invalid_request: scope parameter not supported",
        )

    if not refresh_token_value or refresh_token_value not in refresh_tokens:
        raise HTTPException(status_code=400, detail="Invalid refresh_token")

    token_data = refresh_tokens[refresh_token_value]
    if token_data["client_id"] != client_id:
        raise HTTPException(status_code=400, detail="Client mismatch")

    # Rotate: invalidate old refresh_token and issue a new one.
    del refresh_tokens[refresh_token_value]

    new_access_token = secrets.token_urlsafe(32)
    new_refresh_token = secrets.token_urlsafe(32)
    expires_at = time.time() + 3600

    access_tokens[new_access_token] = {
        "client_id": client_id,
        "scope": token_data["scope"],
        "expires_at": expires_at,
        "token_type": "Bearer",
    }
    refresh_tokens[new_refresh_token] = {
        "client_id": client_id,
        "scope": token_data["scope"],
    }

    return TokenResponse(
        access_token=new_access_token,
        token_type="Bearer",  # noqa: S106
        expires_in=3600,
        refresh_token=new_refresh_token,
        scope=scope or token_data["scope"],
    )


@app.get("/api/weather")
async def get_weather(
    request: Request, city: str = "San Francisco", units: str = "metric"
):
    """Weather API endpoint that returns weather data for a city."""

    auth_header = request.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401, detail="Missing or invalid authorization header"
        )

    token = auth_header[7:]

    if token not in access_tokens:
        raise HTTPException(status_code=401, detail="Invalid access token")

    token_data = access_tokens[token]

    if time.time() > token_data["expires_at"]:
        del access_tokens[token]
        raise HTTPException(status_code=401, detail="Access token expired")

    import random
    from datetime import datetime

    conditions = ["Sunny", "Partly Cloudy", "Cloudy", "Light Rain", "Clear"]

    return {
        "city": city,
        "temperature": random.randint(15, 30),  # noqa: S311
        "condition": random.choice(conditions),  # noqa: S311
        "humidity": random.randint(40, 80),  # noqa: S311
        "wind_speed": random.randint(5, 25),  # noqa: S311
        "timestamp": datetime.now().isoformat(),
        "units": units,
        "api_client": token_data["client_id"],
    }


@app.get("/")
async def root():
    """Root endpoint with server information."""
    return HTMLResponse(
        """
    <html>
        <head><title>Weather API OAuth2 Server</title></head>
        <body>
            <h1>Weather API OAuth2 Server</h1>
            <h2>Available Endpoints:</h2>
            <ul>
                <li>GET /auth</li>
                <li>POST /token
                    (client_credentials, authorization_code, refresh_token)</li>
                <li>GET /.well-known/openid_configuration</li>
                <li>GET /api/weather (Bearer token required)</li>
            </ul>
            <h2>Test Client Credentials:</h2>
            <ul>
                <li>Client ID: test_client</li>
                <li>Client Secret: test_secret</li>
                <li>Scopes: read, write, admin</li>
            </ul>
        </body>
    </html>
    """
    )


if __name__ == "__main__":
    import uvicorn

    print("🌤️  Starting Weather API OAuth2 Server...")
    print(f"    STRICT_SCOPE_REJECTION={STRICT_SCOPE_REJECTION}")
    print("🏠 Server Info: http://localhost:8080")
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")  # noqa: S104
