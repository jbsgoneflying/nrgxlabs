"""Invite-code gate compatible with v1.

v2 verifies the same HMAC-signed cookie v1 issues, so a desk member who
logs into ``app.nrgxlabs.com`` is automatically signed into
``v2.app.nrgxlabs.com`` (cookie is set on the parent domain).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .config import V2Config


def _b64url_decode(s: str) -> bytes:
    pad = "=" * ((4 - (len(s) % 4)) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("utf-8"))


def verify_token(token: str, secret: str) -> bool:
    try:
        if not token or "." not in token or not secret:
            return False
        body, sig = token.split(".", 1)
        expected = hmac.new(secret.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
        got = _b64url_decode(sig)
        if not hmac.compare_digest(expected, got):
            return False
        payload = json.loads(_b64url_decode(body).decode("utf-8"))
        exp = float(payload.get("exp") or 0.0)
        if exp <= time.time():
            return False
        return True
    except Exception:
        return False


_PUBLIC_PREFIXES = ("/static/", "/assets/", "/login", "/logout", "/.well-known/acme-challenge/")
_PUBLIC_EXACT = {
    "/api/health",
    "/api/v2/health",
    "/api/v2/version",
    "/favicon.ico",
    "/robots.txt",
}


def path_is_public(path: str) -> bool:
    p = str(path or "")
    if p in _PUBLIC_EXACT:
        return True
    return any(p.startswith(prefix) for prefix in _PUBLIC_PREFIXES)


def auth_enabled(cfg: V2Config) -> bool:
    if cfg.public_access:
        return False
    return bool(cfg.invite_code)


async def invite_gate(request: Request, call_next, cfg: V2Config):
    if not auth_enabled(cfg):
        return await call_next(request)

    if not cfg.auth_secret:
        return HTMLResponse(
            "<h3>Server misconfigured</h3><p>AUTH_SECRET required when INVITE_CODE is set.</p>",
            status_code=500,
        )

    if path_is_public(request.url.path):
        return await call_next(request)

    token = request.cookies.get(cfg.auth_cookie_name) or ""
    if verify_token(token, cfg.auth_secret):
        return await call_next(request)

    nxt = request.url.path
    if request.url.query:
        nxt = f"{nxt}?{request.url.query}"
    return RedirectResponse(url=f"https://app.nrgxlabs.com/login?next={nxt}", status_code=302)
