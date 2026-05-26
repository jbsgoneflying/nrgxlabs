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
from fastapi import FastAPI, HTTPException, Request, Form
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from backend.config import get_flags

from backend.routers import (
    engine1_breach,
    engine2_spx_ic,
    engine2b_flex_ic,
    engine3_red_dog,
    engine4_ichimoku,
    engine5_lead_lag,
    engine7_pairs,
    engine8_post_event,
    engine9_credit,
    engine12_vix_fade,
    engine13_gap_regime,
    engine14_ic_scenario,
    engine15_earnings_ic,
    calendar,
    market_intel,
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

app = FastAPI(title="Raven-Tech.co", version="2.0.0")

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
    # The app is intentionally public by default so the full desk-agent
    # walkthrough (every route, every engine) is reachable without a cookie.
    # Set ``PUBLIC_ACCESS=0`` (or any falsy value) in the environment to
    # re-engage the invite-code gate. ``INVITE_CODE`` still must be set and
    # non-empty for the gate to do anything.
    public = (os.getenv("PUBLIC_ACCESS") or "1").strip().lower()
    if public in ("1", "true", "yes", "on"):
        return False
    return bool(INVITE_CODE)


def _path_is_public(path: str) -> bool:
    p = str(path or "")
    if p.startswith("/static/"):
        return True
    if p in ("/api/health", "/privacy-policy", "/support/fasting-guide"):
        return True
    if p.startswith("/login") or p.startswith("/logout"):
        return True
    if p.startswith("/.well-known/acme-challenge/"):
        return True
    if p == "/api/engine7-pairs/nightly-review":
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
    nxt = str(next or "/")
    return HTMLResponse(
        f"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>NRGX Labs — Access</title>
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
def login_submit(code: str = Form(...), next: str = Form("/")):
    if not _auth_enabled():
        return RedirectResponse(url=str(next or "/"), status_code=302)
    if str(code or "").strip() != INVITE_CODE:
        return RedirectResponse(url="/login?error=1", status_code=302)

    now = time.time()
    token = _sign_token({"v": 1, "exp": now + float(AUTH_COOKIE_TTL_S)})
    resp = RedirectResponse(url=str(next or "/"), status_code=302)
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
    """Serve Market Intelligence as the home page."""
    mi_path = STATIC_DIR / "market-intelligence.html"
    if not mi_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/market-intelligence.html")
    return FileResponse(str(mi_path))


@app.get("/breach")
def breach_page():
    return FileResponse(str(STATIC_DIR / "index.html"))


@app.get("/calendar")
def calendar_page():
    return FileResponse(str(STATIC_DIR / "earnings-calendar.html"))


@app.get("/api/health")
def health():
    return {"ok": True, "v": "2026-02-28-router-split"}


@app.get("/api/flags")
def flags():
    f = get_flags()
    return {
        "ENABLE_BENZINGA": bool(f.ENABLE_BENZINGA),
        "BENZINGA_ENABLE_EVENT_RISK": bool(f.BENZINGA_ENABLE_EVENT_RISK),
        "ENABLE_ENGINE2_SPX_IC": bool(f.ENABLE_ENGINE2_SPX_IC),
        "ENABLE_E2B_FLEX_EXPIRY": bool(getattr(f, "ENABLE_E2B_FLEX_EXPIRY", False)),
        "ENGINE2_DEFAULT_YEARS": int(f.ENGINE2_LOOKBACK_YEARS_DEFAULT),
        "ENGINE2_DEFAULT_EM_MULTS": str(f.ENGINE2_EM_MULTS),
        "ENGINE2_DEFAULT_WING_PTS": str(f.ENGINE2_WING_WIDTH_PTS),
        "ENGINE2_MACRO_MULTIPLIER_CAP": float(f.ENGINE2_MACRO_MULTIPLIER_CAP),
        "ENGINE2_REQUIRE_ORATS_DAILY_VWAP": bool(getattr(f, "ENGINE2_REQUIRE_ORATS_DAILY_VWAP", False)),
    }


@app.get("/privacy-policy")
def privacy_policy_page():
    return FileResponse(str(STATIC_DIR / "privacy-policy.html"))


@app.get("/support/fasting-guide")
def fasting_guide_support_page():
    return FileResponse(str(STATIC_DIR / "support-fasting-guide.html"))


@app.get("/spx")
def spx_page():
    return FileResponse(str(STATIC_DIR / "spx.html"))


@app.get("/red-dog")
def red_dog_page():
    return FileResponse(str(STATIC_DIR / "red-dog.html"))


@app.get("/ichimoku")
def ichimoku_page():
    return FileResponse(str(STATIC_DIR / "ichimoku.html"))


@app.get("/news-risk")
def news_risk_page():
    return FileResponse(str(STATIC_DIR / "news-risk.html"))


@app.get("/lead-lag")
def lead_lag_page():
    return FileResponse(str(STATIC_DIR / "engine5.html"))


@app.get("/pairs")
def pairs_page():
    return FileResponse(str(STATIC_DIR / "pairs.html"))


@app.get("/post-event")
def post_event_page():
    return FileResponse(str(STATIC_DIR / "post-event.html"))


@app.get("/credit-stress")
def credit_stress_page():
    fl = get_flags()
    if not getattr(fl, "ENABLE_ENGINE9_CREDIT_STRESS", True):
        raise HTTPException(status_code=404, detail="Engine 9 disabled")
    return FileResponse(str(STATIC_DIR / "engine9.html"))


@app.get("/vix-fade")
def vix_fade_page():
    fl = get_flags()
    if not getattr(fl, "ENABLE_ENGINE12_VIX_FADE", True):
        raise HTTPException(status_code=404, detail="Engine 12 disabled")
    return FileResponse(str(STATIC_DIR / "vix-fade.html"))


@app.get("/gap-regime")
def gap_regime_page():
    fl = get_flags()
    if not getattr(fl, "ENABLE_ENGINE13_GAP_REGIME", True):
        raise HTTPException(status_code=404, detail="Engine 13 disabled")
    return FileResponse(str(STATIC_DIR / "gap-regime.html"))


@app.get("/ic-scenario")
def ic_scenario_page():
    # E2/E14 single-surface desk migration:
    # keep Engine 14 APIs available, but retire the standalone /ic-scenario page UX.
    return RedirectResponse(url="/spx", status_code=302)


# NOTE: The /earnings-ic page was retired 2026-05-20 alongside the
# Wing Decision Console refactor. The /api/earnings-ic/* routes
# (registered via include_router(engine15_earnings_ic)) stay alive
# because the simulator powers E1 Live Review's projection panel
# server-side and we want existing deep-link integrations and any
# external callers to keep working. The static HTML/JS no longer
# exists, so the page route would 500 if we tried to serve it.


@app.get("/compare")
def serve_compare():
    return FileResponse(str(STATIC_DIR / "compare.html"))


@app.get("/market-intelligence")
def market_intelligence_page():
    return FileResponse(str(STATIC_DIR / "market-intelligence.html"))


# ── Include API routers ──

app.include_router(engine1_breach.router)
app.include_router(engine2_spx_ic.router)
app.include_router(engine2b_flex_ic.router)
app.include_router(engine3_red_dog.router)
app.include_router(engine4_ichimoku.router)
app.include_router(engine5_lead_lag.router)
app.include_router(engine7_pairs.router)
app.include_router(engine8_post_event.router)
app.include_router(engine9_credit.router)
app.include_router(engine12_vix_fade.router)
app.include_router(engine13_gap_regime.router)
app.include_router(engine14_ic_scenario.router)
app.include_router(engine15_earnings_ic.router)
app.include_router(calendar.router)
app.include_router(market_intel.router)
app.include_router(front_layer.router)
app.include_router(raven_chat.router)
app.include_router(desk_insight.router)


# ── Startup: rebuild trade indexes if they expired while the app was down ──

@app.on_event("startup")
def _rebuild_trade_indexes():
    try:
        from backend.engine2_trades import rebuild_index_if_missing as e2_rebuild
        from backend.e1_earnings_trades import rebuild_index_if_missing as e1_rebuild
        e2_rebuild()
        e1_rebuild()
    except Exception:
        pass
