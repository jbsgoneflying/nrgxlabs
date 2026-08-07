from __future__ import annotations

import logging
import os
import base64
import hashlib
import hmac
import json
import time
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request, Form
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# Ichimoku-only desk (2026-08-07): every other engine is offline in full.
# Only the routers the /ichimoku page needs are imported and mounted:
#   - engine4_ichimoku : the scanner itself (EODHD-backed)
#   - front_layer      : card-insight LLM commentary (OpenAI)
#   - desk_insight     : desk insight panel (OpenAI)
#   - raven_chat       : /api/chat advisor (OpenAI)
from backend.routers import (
    engine4_ichimoku,
    front_layer,
    raven_chat,
    desk_insight,
)

try:
    load_dotenv()
except Exception:
    pass


def _configure_logging() -> None:
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


_configure_logging()
LOG = logging.getLogger("app")

app = FastAPI(title="NRGX Labs", version="2.0.0")

# ---- Invite-code gate (lightweight) ----
AUTH_COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "raven_session").strip() or "raven_session"
AUTH_COOKIE_TTL_S = int(float(os.getenv("AUTH_COOKIE_TTL_S") or (7 * 24 * 60 * 60)))  # 7 days
INVITE_CODE = (os.getenv("INVITE_CODE") or "").strip()
AUTH_SECRET = (os.getenv("AUTH_SECRET") or "").strip()


def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("utf-8").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * ((4 - (len(s) % 4)) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("utf-8"))


def _sign_token(payload: dict) -> str:
    """Token format: base64url(json).base64url(hmac_sha256)"""
    if not AUTH_SECRET:
        raise RuntimeError("Missing AUTH_SECRET (required when INVITE_CODE is set).")
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = _b64url_encode(raw)
    sig = hmac.new(AUTH_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
    return f"{body}.{_b64url_encode(sig)}"


def _verify_token(token: str) -> bool:
    try:
        if not token or "." not in token:
            return False
        body, sig = token.split(".", 1)
        if not AUTH_SECRET:
            return False
        expected = hmac.new(AUTH_SECRET.encode("utf-8"), body.encode("utf-8"), hashlib.sha256).digest()
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


def _auth_enabled() -> bool:
    # The desk is invite-only: the app is gated by default so general traffic
    # off the splash page (nrgxlabs.com -> "Enter the desk") hits the invite
    # code wall before reaching any engine. Set ``PUBLIC_ACCESS=1`` (or any
    # truthy value) in the environment to drop the gate for a full walkthrough.
    # ``INVITE_CODE`` still must be set and non-empty for the gate to engage.
    public = (os.getenv("PUBLIC_ACCESS") or "0").strip().lower()
    if public in ("1", "true", "yes", "on"):
        return False
    return bool(INVITE_CODE)


def _path_is_public(path: str) -> bool:
    p = str(path or "")
    if p.startswith("/static/"):
        # Static assets (css/js/images/fonts) stay public so the /login page
        # can render. The engine HTML shells live in this same folder but are
        # served through gated routes (/spx, /market-intelligence, ...); block
        # direct .html access so the gate can't be bypassed via /static/*.html.
        if p.lower().endswith(".html"):
            return False
        return True
    if p in ("/api/health", "/privacy-policy", "/support/fasting-guide"):
        return True
    if p.startswith("/login") or p.startswith("/logout"):
        return True
    if p.startswith("/.well-known/acme-challenge/"):
        return True
    return False


@app.middleware("http")
async def invite_gate(request: Request, call_next):
    if not _auth_enabled():
        return await call_next(request)

    if not AUTH_SECRET:
        return HTMLResponse(
            "<h3>Server misconfigured</h3><p>AUTH_SECRET is required when INVITE_CODE is set.</p>",
            status_code=500,
        )

    if _path_is_public(request.url.path):
        return await call_next(request)

    token = request.cookies.get(AUTH_COOKIE_NAME) or ""
    if _verify_token(token):
        return await call_next(request)

    nxt = request.url.path
    if request.url.query:
        nxt = f"{nxt}?{request.url.query}"
    return RedirectResponse(url=f"/login?next={nxt}", status_code=302)


# ── Login / Logout ──

@app.get("/login", response_class=HTMLResponse)
def login_page(next: str | None = None):
    nxt = str(next or "/ichimoku")
    return HTMLResponse(
        f"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>NRGX Labs — Access</title>
    <meta name="theme-color" content="#f5f5f7" />
    <meta name="description" content="NRGX Labs — Private research lab for self-directed capital. Invite-only beta access." />
    <meta name="application-name" content="NRGX Labs" />
    <meta name="apple-mobile-web-app-title" content="NRGX Labs" />
    <meta name="robots" content="noindex, nofollow" />
    <link rel="icon" href="/static/favicon.ico" sizes="any" />
    <link rel="icon" type="image/png" sizes="32x32" href="/static/favicon-32.png" />
    <link rel="icon" type="image/png" sizes="16x16" href="/static/favicon-16.png" />
    <link rel="apple-touch-icon" sizes="180x180" href="/static/apple-touch-icon.png" />
    <link rel="stylesheet" href="/static/styles.css" />
    <style>
      body {{ display:flex; align-items:center; justify-content:center; min-height:100vh; }}
      .loginCard {{ width:min(520px, 92vw); padding:18px; border:1px solid var(--border); border-radius:18px; background:var(--surface); box-shadow:var(--shadow); }}
      .loginTop {{ display:flex; align-items:center; gap:12px; }}
      .loginTop img {{ width:120px; height:54px; object-fit:contain; }}
      .loginTitle {{ font-size:18px; font-weight:800; letter-spacing:0.1px; }}
      .loginSub {{ margin-top:2px; color:var(--muted); font-size:13px; }}
      .loginForm {{ margin-top:14px; display:grid; gap:10px; }}
      .loginForm input {{ padding:12px 12px; border-radius:12px; border:1px solid var(--border); font-size:14px; }}
      .loginForm button {{ justify-self:start; }}
      .loginFoot {{ margin-top:10px; color:var(--muted); font-size:12px; }}
    </style>
  </head>
  <body>
    <div class="loginCard">
      <div class="loginTop">
        <img src="/static/NRGX-Logo.png" alt="NRGX Labs" />
        <div>
          <div class="loginTitle">Private Beta</div>
          <div class="loginSub">Enter your invite code to continue.</div>
        </div>
      </div>
      <form class="loginForm" method="post" action="/login">
        <input type="hidden" name="next" value="{nxt}" />
        <input type="password" name="code" placeholder="Invite code" autocomplete="current-password" required />
        <button class="btn" type="submit">Continue</button>
      </form>
      <div class="loginFoot">This app uses paid market-data APIs. Access is limited.</div>
    </div>
  </body>
</html>
        """.strip(),
        status_code=200,
    )


@app.post("/login")
def login_submit(code: str = Form(...), next: str = Form("/ichimoku")):
    if not _auth_enabled():
        return RedirectResponse(url=str(next or "/ichimoku"), status_code=302)
    if str(code or "").strip() != INVITE_CODE:
        return RedirectResponse(url="/login?error=1", status_code=302)

    now = time.time()
    token = _sign_token({"v": 1, "exp": now + float(AUTH_COOKIE_TTL_S)})
    resp = RedirectResponse(url=str(next or "/ichimoku"), status_code=302)
    secure = str(os.getenv("COOKIE_SECURE") or "").strip().lower() in ("1", "true", "yes", "y", "on")
    resp.set_cookie(
        AUTH_COOKIE_NAME,
        token,
        max_age=int(AUTH_COOKIE_TTL_S),
        httponly=True,
        secure=bool(secure),
        samesite="lax",
        path="/",
    )
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse(url="/login", status_code=302)
    resp.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return resp


# ── Static files ──

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


# ── Page-serving routes ──

@app.get("/")
def index():
    """The desk is Ichimoku-only: the app root lands on the scanner."""
    return RedirectResponse(url="/ichimoku", status_code=302)


# Retired engine pages (Ichimoku-only desk). Old bookmarks and deep links
# land on the scanner instead of a 404. The API routers behind these pages
# are no longer mounted, so the engines are offline in full.
_RETIRED_PAGES = (
    "/breach",
    "/calendar",
    "/spx",
    "/red-dog",
    "/desk-brain",
    "/ai-capex",
    "/earnings-drift",
    "/equity-repricing",
    "/news-risk",
    "/lead-lag",
    "/pairs",
    "/post-event",
    "/credit-stress",
    "/vix-fade",
    "/gap-regime",
    "/ic-scenario",
    "/compare",
    "/market-intelligence",
)


def _redirect_to_desk():
    return RedirectResponse(url="/ichimoku", status_code=302)


for _page in _RETIRED_PAGES:
    app.add_api_route(_page, _redirect_to_desk, methods=["GET"], include_in_schema=False)


@app.get("/api/health")
def health():
    return {"ok": True, "v": "2026-02-28-router-split"}


# robots.txt — the app domain (app.nrgxlabs.com) is
# invite-only and behind a login gate. We still serve an explicit
# Disallow so the login page itself doesn't leak into search indexes,
# and no crawler wastes time probing /api/* surfaces.
_ROBOTS_TXT = "User-agent: *\nDisallow: /\n"


@app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
def robots_txt() -> str:
    return _ROBOTS_TXT


@app.get("/privacy-policy")
def privacy_policy_page():
    return FileResponse(str(STATIC_DIR / "privacy-policy.html"))


@app.get("/support/fasting-guide")
def fasting_guide_support_page():
    return FileResponse(str(STATIC_DIR / "support-fasting-guide.html"))


@app.get("/ichimoku")
def ichimoku_page():
    return FileResponse(str(STATIC_DIR / "ichimoku.html"))


# ── Include API routers (Ichimoku-only desk) ──

app.include_router(engine4_ichimoku.router)
app.include_router(front_layer.router)
app.include_router(raven_chat.router)
app.include_router(desk_insight.router)
