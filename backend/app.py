from __future__ import annotations

import logging
import os
import base64
import hashlib
import hmac
import json
import time
import datetime as dt
from pathlib import Path
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
from typing import Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Form
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from cachetools import TTLCache
from pydantic import BaseModel, Field
import uuid
import pathlib

from backend.earnings_logic import BreachInputError, compute_breach_stats, compute_current_snapshot
from backend.go_no_go import compute_go_no_go
from backend.config import get_flags
from backend.benzinga_client import BenzingaClient
from backend.orats_client import OratsClient, OratsError
from backend.spx_ic_engine import compute_engine2_spx_ic, compute_spx_live_levels, compute_live_levels, fetch_dailies_ohlc_range
from backend.redis_store import get_store_optional
from backend.calendar_api import build_calendar_payload
from backend.condor_rank import compute_condor_rank
from backend.calendar_snapshot import EARNINGS_SNAPSHOT_KEY, load_earnings_snapshot
from backend.fmp_snapshot import FMP_EARNINGS_SNAPSHOT_KEY, load_fmp_earnings_snapshot
from backend.macro_event_stats import compute_macro_event_stats
from backend.fmp_client import FmpClient, FmpError
from backend.api_ninjas_client import ApiNinjasClient, ApiNinjasError
from backend.engine3_screener import compute_engine3_scan, compute_single_ticker_scan
from backend.engine4_screener import (
    run_universe_scan as compute_engine4_scan,
    scan_single_ticker as compute_engine4_single_ticker,
    get_all_signals as get_engine4_signals,
    refresh_signal_statuses as refresh_engine4_statuses,
)
from backend.breach_ranker import rank_tickers, summarize_tiers
from backend.flow_pressure import compute_flow_pressure, compute_flow_pressure_snapshot, FlowPressure
from backend.gating import gate_scan_results, summarize_gates
from backend.earnings_gamma_context import compute_earnings_gamma_context
from backend.sequencer import (
    SequencerEvent, WeeklySequence, current_week_id,
    detect_state_changes, build_weekly_sequence, week_trading_days,
    PATTERN_TEMPLATES,
)
from backend.llm_client import generate_desk_brief, suggest_features
from backend.daily_market_state import (
    DailyMarketState, build_daily_market_state, persist_dms,
    load_dms, load_dms_history, compute_dms_diff, DMS_INDEX_KEY,
)
from backend.cross_asset_stress import (
    CrossAssetStressSnapshot, AssetStressReading,
    compute_asset_stress, build_cross_asset_snapshot, CROSS_ASSET_UNIVERSE,
)
from backend.news_theme_intelligence import (
    NewsThemeSnapshot, score_themes, extract_headlines_from_eodhd,
    extract_headlines_from_benzinga, persist_theme_snapshot, load_theme_history,
)
from backend.front_layer_llm import (
    generate_morning_brief, generate_weekly_roadmap, detect_asymmetries,
    generate_asset_insight, generate_card_insight,
)


try:
    # In some environments (CI/sandboxes), `.env` may be unreadable; keep startup resilient.
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

app = FastAPI(title="ORATS Earnings Implied Move Breach", version="1.0.0")

# ---- Invite-code gate (lightweight) ----
# Intended for private beta access so we don't expose paid ORATS/Benzinga keys to the public internet.
AUTH_COOKIE_NAME = os.getenv("AUTH_COOKIE_NAME", "raven_session").strip() or "raven_session"
AUTH_COOKIE_TTL_S = int(float(os.getenv("AUTH_COOKIE_TTL_S") or (7 * 24 * 60 * 60)))  # 7 days
INVITE_CODE = (os.getenv("INVITE_CODE") or "").strip()
AUTH_SECRET = (os.getenv("AUTH_SECRET") or "").strip()

# iOS app API token (allows TestFlight/production iOS app to bypass invite gate)
IOS_API_TOKEN = (os.getenv("IOS_API_TOKEN") or "").strip()


def _check_api_token(request: Request) -> bool:
    """Check if request has valid X-API-Token header for iOS app access."""
    if not IOS_API_TOKEN:
        return False
    token = request.headers.get("X-API-Token", "").strip()
    return token and hmac.compare_digest(token, IOS_API_TOKEN)


def _b64url_encode(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode("utf-8").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    pad = "=" * ((4 - (len(s) % 4)) % 4)
    return base64.urlsafe_b64decode((s + pad).encode("utf-8"))


def _sign_token(payload: dict) -> str:
    """
    Token format: base64url(json).base64url(hmac_sha256)
    """
    if not AUTH_SECRET:
        # Hard fail in gated mode; in ungated mode we don't mint tokens anyway.
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
    # If no invite code is set, run open (dev-friendly).
    return bool(INVITE_CODE)


def _normalize_host(host: str | None) -> str:
    """
    Normalize host header into a bare hostname (strip port, lowercase).
    Examples:
      - "app.raven-tech.co" -> "app.raven-tech.co"
      - "raven-tech.co:443" -> "raven-tech.co"
    """
    h = str(host or "").strip().lower()
    if not h:
        return ""
    if ":" in h:
        h = h.split(":", 1)[0].strip()
    # Some reverse proxies / clients may include a trailing dot; normalize it away.
    return h.rstrip(".")


def _is_tailnet_host(host: str | None) -> bool:
    """
    Treat Tailscale MagicDNS hostnames as private access.
    Example: raven-tech.tail530226.ts.net

    This allows the iOS app (VPN-only) to call APIs without the web login gate,
    while keeping the public web domain behavior unchanged.
    """
    h = _normalize_host(host)
    return h.endswith(".ts.net")


def _is_root_domain_host(host: str | None) -> bool:
    h = _normalize_host(host)
    return h in ("raven-tech.co", "www.raven-tech.co")


def _path_is_public(path: str) -> bool:
    p = str(path or "")
    if p.startswith("/static/"):
        return True
    if p in ("/api/health", "/privacy-policy", "/support/fasting-guide"):
        return True
    if p.startswith("/login") or p.startswith("/logout"):
        return True
    # Let’s Encrypt http-01 (if you choose to serve challenges through the app).
    if p.startswith("/.well-known/acme-challenge/"):
        return True
    # Internal cron endpoints — called from localhost, no browser session.
    if p == "/api/engine7-pairs/nightly-review":
        return True
    return False


@app.middleware("http")
async def invite_gate(request: Request, call_next):
    if not _auth_enabled():
        return await call_next(request)

    # If secret is missing, refuse to start in gated mode (prevents insecure deploys).
    if not AUTH_SECRET:
        return HTMLResponse(
            "<h3>Server misconfigured</h3><p>AUTH_SECRET is required when INVITE_CODE is set.</p>",
            status_code=500,
        )

    # Root-domain landing page should remain public even in gated mode.
    if request.url.path == "/" and _is_root_domain_host(request.headers.get("host")):
        return await call_next(request)

    # VPN-only / private access path (Tailscale). Do not require web login cookie.
    if _is_tailnet_host(request.headers.get("host")):
        return await call_next(request)

    # iOS app API token (X-API-Token header) — allows TestFlight builds to bypass invite gate.
    if _check_api_token(request):
        return await call_next(request)

    if _path_is_public(request.url.path):
        return await call_next(request)

    token = request.cookies.get(AUTH_COOKIE_NAME) or ""
    if _verify_token(token):
        return await call_next(request)

    # Redirect to login, preserving the original destination.
    nxt = request.url.path
    if request.url.query:
        nxt = f"{nxt}?{request.url.query}"
    return RedirectResponse(url=f"/login?next={nxt}", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_page(next: str | None = None):
    nxt = str(next or "/")
    # Keep the page self-contained; rely on /static assets for logo/styles.
    return HTMLResponse(
        f"""
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>Raven-Tech.co — Access</title>
    <link rel="stylesheet" href="/static/styles.css" />
    <style>
      body {{ display:flex; align-items:center; justify-content:center; min-height:100vh; }}
      .loginCard {{ width:min(520px, 92vw); padding:18px; border:1px solid var(--border); border-radius:18px; background:var(--surface); box-shadow:var(--shadow); }}
      .loginTop {{ display:flex; align-items:center; gap:12px; }}
      .loginTop img {{ width:54px; height:54px; object-fit:contain; }}
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
        <img src="/static/RavenONLY.png" alt="Raven-Tech.co" />
        <div>
          <div class="loginTitle">Raven-Tech.co — Private Beta</div>
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
    # Secure cookie when behind HTTPS; allow local testing if needed.
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

# Keep a singleton ORATS client + a response cache for /api/breach.
_client_lock = threading.Lock()
_client: OratsClient | None = None

_bz_client_lock = threading.Lock()
_bz_client: BenzingaClient | None = None

_fmp_client_lock = threading.Lock()
_fmp_client: FmpClient | None = None

_api_ninjas_client_lock = threading.Lock()
_api_ninjas_client: ApiNinjasClient | None = None

_breach_cache = TTLCache(maxsize=512, ttl=6 * 60 * 60)  # 6 hours
_breach_cache_lock = threading.Lock()

_spx_ic_cache = TTLCache(maxsize=128, ttl=30 * 60)  # 30 minutes (interactive)
_spx_ic_cache_lock = threading.Lock()

_spx_levels_cache = TTLCache(maxsize=128, ttl=60)  # 60s (interactive hover chart)
_spx_levels_cache_lock = threading.Lock()

_levels_cache = TTLCache(maxsize=256, ttl=60)  # 60s (interactive hover chart; per-ticker)
_levels_cache_lock = threading.Lock()

_calendar_cache = TTLCache(maxsize=128, ttl=10 * 60)  # calendar cache (effective ttl controlled per-request by CALENDAR_CACHE_TTL_S)
_calendar_cache_lock = threading.Lock()

_engine1_elig_cache = TTLCache(maxsize=50_000, ttl=24 * 60 * 60)  # 24h
_engine1_elig_cache_lock = threading.Lock()

_condor_rank_cache = TTLCache(maxsize=1024, ttl=6 * 60 * 60)  # 6h
_condor_rank_cache_lock = threading.Lock()

_macro_stats_cache = TTLCache(maxsize=256, ttl=6 * 60 * 60)  # 6h (on-demand)
_macro_stats_cache_lock = threading.Lock()

def _truncate(s: str, n: int) -> str:
    t = str(s or "").replace("\n", " ").strip()
    return (t[:n] + "…") if len(t) > n else t


def _get_client() -> OratsClient:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            _client = OratsClient.from_env()
    return _client


def _get_client_optional() -> OratsClient | None:
    """
    Optional ORATS client so tests / misconfigured envs don't 500.
    """
    try:
        return _get_client()
    except Exception:
        return None


def _get_benzinga_client_optional() -> BenzingaClient | None:
    """
    Optional Benzinga client (only constructed if BENZINGA_API_KEY is set).
    Kept as a singleton so per-process caching is effective.
    """
    # Feature-flag gate (env-driven)
    if not get_flags().ENABLE_BENZINGA:
        return None
    global _bz_client
    if _bz_client is not None:
        return _bz_client
    with _bz_client_lock:
        if _bz_client is None:
            _bz_client = BenzingaClient.from_env_optional()
    return _bz_client


def _get_fmp_client_optional() -> FmpClient | None:
    """
    Optional FMP client (constructed if FMP_API_KEY is set).
    Kept as a singleton to avoid re-reading env and for any internal connection reuse.
    """
    global _fmp_client
    try:
        if _fmp_client is not None:
            return _fmp_client
        with _fmp_client_lock:
            if _fmp_client is None:
                # Only construct if key is present.
                if not (os.getenv("FMP_API_KEY") or "").strip():
                    return None
                _fmp_client = FmpClient.from_env()
        return _fmp_client
    except Exception:
        return None


def _get_api_ninjas_client_optional() -> ApiNinjasClient | None:
    """
    Optional API Ninjas client (constructed if API_NINJAS_API_KEY is set).
    Kept as a singleton to avoid re-reading env and for connection reuse.
    """
    global _api_ninjas_client
    try:
        if _api_ninjas_client is not None:
            return _api_ninjas_client
        with _api_ninjas_client_lock:
            if _api_ninjas_client is None:
                # Only construct if key is present.
                if not (os.getenv("API_NINJAS_API_KEY") or "").strip():
                    return None
                _api_ninjas_client = ApiNinjasClient.from_env()
        return _api_ninjas_client
    except Exception:
        return None


def _breach_cache_key(ticker: str, n: int, years: int, k: float, flags_fp: tuple | None = None) -> tuple:
    # token is never part of key; include feature flags to prevent mixing methodologies
    fp = flags_fp if flags_fp is not None else get_flags().cache_fingerprint()
    return (ticker.strip().upper(), int(n), int(years), float(k), fp)

def _spx_ic_cache_key(params: dict, flags_fp: tuple) -> tuple:
    # stable primitives only
    items = tuple(sorted((k, str(v)) for k, v in (params or {}).items()))
    return ("spx_ic", items, flags_fp)


def _spx_levels_cache_key(params: dict, flags_fp: tuple) -> tuple:
    items = tuple(sorted((k, str(v)) for k, v in (params or {}).items()))
    return ("spx_levels", items, flags_fp)


def _levels_cache_key(ticker: str, params: dict, flags_fp: tuple) -> tuple:
    items = tuple(sorted((k, str(v)) for k, v in (params or {}).items()))
    return ("levels", str(ticker or "").strip().upper(), items, flags_fp)


# Static frontend
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index(request: Request):
    """
    Host-based routing:
      - raven-tech.co / www.raven-tech.co -> marketing landing page
      - all other hosts (e.g. app.raven-tech.co) -> app home/dashboard page
    """
    if _is_root_domain_host(request.headers.get("host")):
        landing_path = STATIC_DIR / "landing.html"
        if not landing_path.exists():
            raise HTTPException(status_code=500, detail="Missing static/landing.html")
        return FileResponse(str(landing_path))

    # App subdomain -> Home dashboard (platform overview & engine directory).
    # Command Center lives at its own route: /command-center
    home_path = STATIC_DIR / "home.html"
    if not home_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/home.html")
    return FileResponse(str(home_path))


@app.get("/breach")
def breach_page():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/index.html")
    return FileResponse(str(index_path))


@app.get("/calendar")
def calendar_page():
    cal_path = STATIC_DIR / "earnings-calendar.html"
    if not cal_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/earnings-calendar.html")
    return FileResponse(str(cal_path))


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/flags")
def flags():
    f = get_flags()
    # Keep this intentionally small (frontend feature gating + debugging).
    return {
        "ENABLE_BENZINGA": bool(f.ENABLE_BENZINGA),
        "BENZINGA_ENABLE_EVENT_RISK": bool(f.BENZINGA_ENABLE_EVENT_RISK),
        "ENABLE_ENGINE2_SPX_IC": bool(f.ENABLE_ENGINE2_SPX_IC),
        "ENGINE2_DEFAULT_YEARS": int(f.ENGINE2_LOOKBACK_YEARS_DEFAULT),
        "ENGINE2_DEFAULT_EM_MULTS": str(f.ENGINE2_EM_MULTS),
        "ENGINE2_DEFAULT_WING_PTS": str(f.ENGINE2_WING_WIDTH_PTS),
        "ENGINE2_MACRO_MULTIPLIER_CAP": float(f.ENGINE2_MACRO_MULTIPLIER_CAP),
        "ENGINE2_REQUIRE_ORATS_DAILY_VWAP": bool(getattr(f, "ENGINE2_REQUIRE_ORATS_DAILY_VWAP", False)),
    }


@app.get("/privacy-policy")
def privacy_policy_page():
    policy_path = STATIC_DIR / "privacy-policy.html"
    if not policy_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/privacy-policy.html")
    return FileResponse(str(policy_path))


@app.get("/support/fasting-guide")
def fasting_guide_support_page():
    support_path = STATIC_DIR / "support-fasting-guide.html"
    if not support_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/support-fasting-guide.html")
    return FileResponse(str(support_path))


@app.get("/spx")
def spx_page():
    spx_path = STATIC_DIR / "spx.html"
    if not spx_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/spx.html")
    return FileResponse(str(spx_path))


@app.get("/red-dog")
def red_dog_page():
    """Engine 3: Red Dog Reversal Scanner page."""
    red_dog_path = STATIC_DIR / "red-dog.html"
    if not red_dog_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/red-dog.html")
    return FileResponse(str(red_dog_path))


@app.get("/ichimoku")
def ichimoku_page():
    """Engine 4: Ichimoku Cloud Continuation Scanner page."""
    ichimoku_path = STATIC_DIR / "ichimoku.html"
    if not ichimoku_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/ichimoku.html")
    return FileResponse(str(ichimoku_path))


@app.get("/news-risk")
def news_risk_page():
    """News Risk Engine: Weekly event risk calendar page."""
    news_risk_path = STATIC_DIR / "news-risk.html"
    if not news_risk_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/news-risk.html")
    return FileResponse(str(news_risk_path))


@app.get("/lead-lag")
def lead_lag_page():
    """Engine 5: Global Lead-Lag Engine page."""
    lead_lag_path = STATIC_DIR / "engine5.html"
    if not lead_lag_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/engine5.html")
    return FileResponse(str(lead_lag_path))


@app.get("/pairs")
def pairs_page():
    """Engine 7: Thematic Relative Value (Pairs) Scanner page."""
    pairs_path = STATIC_DIR / "pairs.html"
    if not pairs_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/pairs.html")
    return FileResponse(str(pairs_path))


@app.get("/post-event")
def post_event_page():
    """Engine 8: Post-Event Trade Extension Evaluator page."""
    pe_path = STATIC_DIR / "post-event.html"
    if not pe_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/post-event.html")
    return FileResponse(str(pe_path))


@app.get("/api/spx-ic")
def spx_ic(
    underlying: str = Query("SPX", description="Underlying: SPX|SPY|QQQ"),
    entry_day: str = Query("mon", description="Entry day: mon|tue|wed"),
    years: int = Query(3, ge=1, le=5),
    widths: str = Query("0.8,1.0,1.2", description="Comma-separated EM width multiples (e.g. 0.8,1.0,1.2)"),
    risk_target_breach_pct: float = Query(25.0, gt=0.0, le=100.0),
    seasonality_mode: str = Query("none", description="Seasonality conditioning: none|quarter|month|summer|opex"),
    weeks_offset: int = Query(0, ge=0, le=5000, description="Pagination: weeks offset"),
    weeks_limit: int = Query(120, ge=0, le=500, description="Pagination: weeks limit (0 to omit weeks)"),
    grid_limit: int = Query(0, ge=0, le=50000, description="Optional cap on riskGrid cells (0 = all)"),
):
    f = get_flags()
    if not f.ENABLE_ENGINE2_SPX_IC:
        raise HTTPException(status_code=404, detail="Engine 2 disabled (ENABLE_ENGINE2_SPX_IC=0).")

    try:
        under = str(underlying or "SPX").strip().upper()
        if under not in ("SPX", "SPY", "QQQ"):
            raise HTTPException(status_code=400, detail="underlying must be SPX|SPY|QQQ")
        params = {
            "underlying": under,
            "entry_day": entry_day,
            "years": years,
            "widths": widths,
            "risk_target_breach_pct": risk_target_breach_pct,
            "seasonality_mode": seasonality_mode,
            "weeks_offset": weeks_offset,
            "weeks_limit": weeks_limit,
            "grid_limit": grid_limit,
        }
        key = _spx_ic_cache_key(params, f.cache_key_engine2())
        with _spx_ic_cache_lock:
            cached = _spx_ic_cache.get(key)
        if cached is not None:
            return cached

        ws: list[float] = []
        for part in str(widths).split(","):
            p = part.strip()
            if not p:
                continue
            ws.append(float(p))
        if not ws:
            ws = [0.8, 1.0, 1.2]
        ws = [w for w in ws if w > 0]
        ws = sorted(list(dict.fromkeys(ws)))  # unique, stable order

        payload = compute_engine2_spx_ic(
            client=_get_client(),
            benzinga_client=_get_benzinga_client_optional(),
            flags=f,
            underlying_preference=under,
            entry_day=entry_day,
            years=years,
            widths=ws,
            risk_target_breach_pct=risk_target_breach_pct,
            seasonality_mode=seasonality_mode,
        )

        # API hardening: apply pagination/caps without changing compute determinism.
        payload["schemaVersion"] = 2

        weeks_obj = payload.get("weeks") if isinstance(payload.get("weeks"), dict) else None
        if weeks_obj is not None:
            all_rows = weeks_obj.get("rows") if isinstance(weeks_obj.get("rows"), list) else []
            if weeks_limit <= 0:
                weeks_obj["rows"] = []
                weeks_obj["page"] = {"offset": int(weeks_offset), "limit": 0, "returned": 0, "total": int(weeks_obj.get("count") or len(all_rows))}
            else:
                sl = all_rows[int(weeks_offset) : int(weeks_offset) + int(weeks_limit)]
                weeks_obj["rows"] = sl
                weeks_obj["page"] = {"offset": int(weeks_offset), "limit": int(weeks_limit), "returned": len(sl), "total": int(weeks_obj.get("count") or len(all_rows))}

        grid_obj = payload.get("riskGrid") if isinstance(payload.get("riskGrid"), dict) else None
        if grid_obj is not None:
            cells = grid_obj.get("cells") if isinstance(grid_obj.get("cells"), list) else []
            if grid_limit and int(grid_limit) > 0:
                grid_obj["cells"] = cells[: int(grid_limit)]
                grid_obj["page"] = {"limit": int(grid_limit), "returned": len(grid_obj["cells"]), "total": len(cells)}
            else:
                grid_obj["page"] = {"limit": 0, "returned": len(cells), "total": len(cells)}

        with _spx_ic_cache_lock:
            _spx_ic_cache[key] = payload
        return payload
    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception("ORATS failure (spx-ic)")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure (spx-ic)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@app.get("/api/spx-levels")
def spx_levels(
    underlying: str = Query("SPX", description="Underlying: SPX|SPY|QQQ"),
    view: str = Query("weekly", description="weekly|nearest"),
    window_days: int = Query(180, ge=30, le=800, description="Calendar days to scan back for SPX EOD closes (chart window)"),
    points: int = Query(90, ge=30, le=260, description="Max trading-day points to return for charting"),
    include_heatmap: int = Query(1, ge=0, le=1, description="Include net $GEX heatmap matrix (0|1)"),
    heatmap_expiries: int = Query(30, ge=6, le=60, description="How many expiries to include in the raw heatmap grid"),
    heatmap_band_pct: float = Query(0.05, ge=0.01, le=0.20, description="Spot band for heatmap strikes (e.g. 0.05 = ±5%)"),
    heatmap_mode: str = Query("slope", description="Heatmap mode: net|slope"),
    heatmap_view: str = Query("composite", description="Heatmap view: composite|raw"),
    slope_window: int = Query(5, ge=1, le=25, description="Slope smoothing window (strikes)"),
    flip_adjacent_n: int = Query(5, ge=2, le=20, description="Persistence requirement for acceleration boundary detection"),
):
    """
    Lightweight chart payload for Engine 2's dealer-gamma / OI wall visualization.
    - Uses ORATS EOD daily closes (range fetch) for SPX price series.
    - Uses ORATS LIVE strikes (short TTL) for OI walls/clusters and gamma peaks.
    """
    f = get_flags()
    if not f.ENABLE_ENGINE2_SPX_IC:
        raise HTTPException(status_code=404, detail="Engine 2 disabled (ENABLE_ENGINE2_SPX_IC=0).")

    v = str(view or "weekly").strip().lower()
    if v not in ("weekly", "nearest"):
        raise HTTPException(status_code=400, detail="view must be weekly|nearest")

    try:
        under = str(underlying or "SPX").strip().upper()
        if under not in ("SPX", "SPY", "QQQ"):
            raise HTTPException(status_code=400, detail="underlying must be SPX|SPY|QQQ")
        params = {
            "underlying": under,
            "view": v,
            "window_days": int(window_days),
            "points": int(points),
            "include_heatmap": int(include_heatmap),
            "heatmap_expiries": int(heatmap_expiries),
            "heatmap_band_pct": float(heatmap_band_pct),
            "heatmap_mode": str(heatmap_mode or "net"),
            "heatmap_view": str(heatmap_view or "composite"),
            "slope_window": int(slope_window),
            "flip_adjacent_n": int(flip_adjacent_n),
        }
        key = _spx_levels_cache_key(params, f.cache_key_engine2())
        with _spx_levels_cache_lock:
            cached = _spx_levels_cache.get(key)
        if cached is not None:
            return cached

        client = _get_client()

        # --- Price series (EOD) ---
        end = dt.date.today()
        start = end - dt.timedelta(days=int(window_days))
        bars = fetch_dailies_ohlc_range(client, ticker=under, start=start, end=end)
        if not bars:
            raise HTTPException(status_code=502, detail=f"{under} unavailable in ORATS dailies (no rows returned for requested window).")
        closes = [{"date": b.trade_date, "close": float(b.close)} for b in (bars or []) if getattr(b, "close", None)]
        if int(points) > 0 and len(closes) > int(points):
            closes = closes[-int(points) :]

        # --- Live levels ---
        if under == "SPX":
            levels = compute_spx_live_levels(
                client,
                view=v,
                band_pct=0.05,
                top_n=5,
                cluster_steps=2,
                include_heatmap=bool(int(include_heatmap)),
                heatmap_expiries=int(heatmap_expiries),
                heatmap_band_pct=float(heatmap_band_pct),
                heatmap_mode=str(heatmap_mode or "net"),
                heatmap_view=str(heatmap_view or "composite"),
                slope_window=int(slope_window),
                flip_adjacent_n=int(flip_adjacent_n),
            )
        else:
            levels = compute_live_levels(
                client,
                underlying=under,
                symbols=(under,),
                view=v,
                band_pct=0.05,
                top_n=5,
                cluster_steps=2,
                include_heatmap=bool(int(include_heatmap)),
                heatmap_expiries=int(heatmap_expiries),
                heatmap_band_pct=float(heatmap_band_pct),
                heatmap_mode=str(heatmap_mode or "net"),
                heatmap_view=str(heatmap_view or "composite"),
                slope_window=int(slope_window),
                flip_adjacent_n=int(flip_adjacent_n),
            )

        payload = {
            "schemaVersion": 3,
            "priceSeries": closes,
            "levels": levels,
        }

        with _spx_levels_cache_lock:
            _spx_levels_cache[key] = payload
        return payload
    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception("ORATS failure (spx-levels)")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure (spx-levels)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@app.get("/api/levels")
def levels(
    ticker: str = Query(..., description="Underlying ticker (e.g. AAPL, TSLA, SPX)"),
    view: str = Query("weekly", description="weekly|nearest"),
    window_days: int = Query(180, ge=30, le=800, description="Calendar days to scan back for EOD closes (chart window)"),
    points: int = Query(90, ge=30, le=260, description="Max trading-day points to return for charting"),
    include_heatmap: int = Query(1, ge=0, le=1, description="Include net $GEX heatmap matrix (0|1)"),
    heatmap_expiries: int = Query(30, ge=6, le=60, description="How many expiries to include in the raw heatmap grid"),
    heatmap_band_pct: float = Query(0.05, ge=0.01, le=0.20, description="Spot band for heatmap strikes (e.g. 0.05 = ±5%)"),
    heatmap_mode: str = Query("slope", description="Heatmap mode: net|slope"),
    heatmap_view: str = Query("composite", description="Heatmap view: composite|raw"),
    slope_window: int = Query(5, ge=1, le=25, description="Slope smoothing window (strikes)"),
    flip_adjacent_n: int = Query(5, ge=2, le=20, description="Persistence requirement for acceleration boundary detection"),
):
    """
    Lightweight chart payload for Dealer Gamma Map + Weekly Gamma Risk Heat-Map (per underlying).
    Used by Engine 1 (single-name) and can be used by Engine 2 (SPX) as well.
    """
    f = get_flags()

    t = str(ticker or "").strip().upper()
    if not t:
        raise HTTPException(status_code=400, detail="ticker is required")

    v = str(view or "weekly").strip().lower()
    if v not in ("weekly", "nearest"):
        raise HTTPException(status_code=400, detail="view must be weekly|nearest")

    try:
        params = {
            "ticker": t,
            "view": v,
            "window_days": int(window_days),
            "points": int(points),
            "include_heatmap": int(include_heatmap),
            "heatmap_expiries": int(heatmap_expiries),
            "heatmap_band_pct": float(heatmap_band_pct),
            "heatmap_mode": str(heatmap_mode or "net"),
            "heatmap_view": str(heatmap_view or "composite"),
            "slope_window": int(slope_window),
            "flip_adjacent_n": int(flip_adjacent_n),
        }
        key = _levels_cache_key(t, params, f.cache_key_engine2())
        with _levels_cache_lock:
            cached = _levels_cache.get(key)
        if cached is not None:
            return cached

        client = _get_client()

        # --- Price series (EOD) ---
        end = dt.date.today()
        start = end - dt.timedelta(days=int(window_days))
        bars = fetch_dailies_ohlc_range(client, ticker=t, start=start, end=end)
        closes = [{"date": b.trade_date, "close": float(b.close)} for b in (bars or []) if getattr(b, "close", None)]
        if int(points) > 0 and len(closes) > int(points):
            closes = closes[-int(points) :]

        # --- Live levels ---
        levels_obj = compute_live_levels(
            client,
            underlying=t,
            symbols=(("SPXW", "SPX", "SPY") if t == "SPX" else (t,)),
            view=v,
            band_pct=0.05,
            top_n=5,
            cluster_steps=2,
            include_heatmap=bool(int(include_heatmap)),
            heatmap_expiries=int(heatmap_expiries),
            heatmap_band_pct=float(heatmap_band_pct),
            heatmap_mode=str(heatmap_mode or "net"),
            heatmap_view=str(heatmap_view or "composite"),
            slope_window=int(slope_window),
            flip_adjacent_n=int(flip_adjacent_n),
        )

        payload = {
            "schemaVersion": 3,
            "ticker": t,
            "priceSeries": closes,
            "levels": levels_obj,
        }

        with _levels_cache_lock:
            _levels_cache[key] = payload
        return payload
    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception("ORATS failure (levels)")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure (levels)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@app.get("/api/breach")
def breach(
    ticker: str = Query(..., description="US equity ticker"),
    n: int = Query(20, ge=1, le=50),
    years: int = Query(5, ge=1, le=10),
    k: float = Query(1.0, gt=0.0),
    mode: str | None = Query(None, description="trade builder: auto|equal_delta|equal_premium"),
    symmetry: str | None = Query(None, description="trade builder: auto|symmetric|manual"),
    target_delta: float | None = Query(None, gt=0.0, lt=1.0),
    target_premium: float | None = Query(None, gt=0.0),
    wing_width: float | None = Query(None, gt=0.0),
    dte_target: int | None = Query(None, ge=1, le=60),
    exp: str | None = Query(None, description="trade builder expiration (YYYY-MM-DD)"),
    mc: bool | None = Query(None, description="enable Monte Carlo earnings gap risk outputs (additive)"),
    mc_opt: bool | None = Query(None, description="enable Monte Carlo wing optimization (risk-only)"),
    mc_stability: bool | None = Query(None, description="enable bootstrap stability + asymmetry caps (additive)"),
    mc_cond_quarter: bool | None = Query(None, description="MC conditioning: quarter"),
    mc_cond_regime: bool | None = Query(None, description="MC conditioning: regime"),
    mc_event_date: str | None = Query(None, description="manual next earnings date override (YYYY-MM-DD)"),
    mc_event_timing: str | None = Query(None, description="manual next earnings timing override (AMC|BMO)"),
):
    try:
        trade_builder_inputs = {
            "mode": mode,
            "symmetry": symmetry,
            "target_delta": target_delta,
            "target_premium": target_premium,
            "wing_width": wing_width,
            "dte_target": dte_target,
            "exp": exp,
        }
        has_trade_builder = any(v is not None for v in trade_builder_inputs.values())

        # Per-request feature overrides (additive). Defaults remain env-driven unless query params are passed.
        base_flags = get_flags()
        overrides = {}
        if mc is not None:
            overrides["ENABLE_MONTE_CARLO_EARNINGS"] = bool(mc)
        if mc_opt is not None:
            overrides["MC_ENABLE_WING_OPTIMIZATION"] = bool(mc_opt)
        if mc_stability is not None:
            overrides["MC_ENABLE_TAS_STABILITY"] = bool(mc_stability)
        if mc_cond_quarter is not None:
            overrides["MC_ENABLE_CONDITION_ON_QUARTER"] = bool(mc_cond_quarter)
        if mc_cond_regime is not None:
            overrides["MC_ENABLE_CONDITION_ON_REGIME"] = bool(mc_cond_regime)

        effective_flags = replace(base_flags, **overrides) if overrides else base_flags
        enable_mc = bool(effective_flags.ENABLE_MONTE_CARLO_EARNINGS)

        # MC depends on near-term anchoring (nextEvent/current snapshot); avoid mixing stale cached payloads.
        if enable_mc:
            has_trade_builder = True

        key = _breach_cache_key(ticker, n, years, k, effective_flags.cache_fingerprint())
        if not has_trade_builder:
            with _breach_cache_lock:
                cached = _breach_cache.get(key)
            if cached is not None:
                # Refresh "current" snapshot even when the heavy payload is cached.
                # This prevents stale assumed-price/EM issues in the Trade Builder UI.
                try:
                    fresh = dict(cached)
                    client0 = _get_client()
                    fresh["current"] = compute_current_snapshot(client=client0, ticker=ticker.strip().upper())
                    # Refresh GO/NO-GO as it depends on current snapshot + live/macro context.
                    try:
                        bz_for_go = _get_benzinga_client_optional() if bool(get_flags().ENABLE_BENZINGA) else None
                        fresh["goNoGo"] = compute_go_no_go(client0, ticker=ticker.strip().upper(), payload=fresh, benzinga_client=bz_for_go)
                    except Exception:
                        # Non-fatal: keep cached response if GO/NO-GO refresh fails.
                        pass
                    return fresh
                except Exception:
                    return cached

        client = _get_client()
        payload = compute_breach_stats(
            client=client,
            ticker=ticker,
            n=n,
            years=years,
            k=k,
            trade_builder_inputs=(trade_builder_inputs if has_trade_builder else None),
            flags_override=effective_flags,
            next_event_override={"date": mc_event_date, "timing": mc_event_timing},
            benzinga_client=_get_benzinga_client_optional(),
        )

        # Inject Earnings Gamma Context (Raven-Tech 2.0)
        try:
            from backend.dealer_gamma_context import compute_dealer_gamma_context
            from backend.engine2_gamma_addons import compute_tail_ignition
            t_upper = ticker.strip().upper()
            rows = client.live_strikes(ticker=t_upper, fields="strike,gamma,callOpenInterest,putOpenInterest,spotPrice").rows or []
            if rows:
                dg = compute_dealer_gamma_context(rows)
                ti_data = compute_tail_ignition(client, t_upper)
                spot = None
                for r in rows:
                    if isinstance(r, dict) and r.get("spotPrice"):
                        spot = float(r["spotPrice"])
                        break
                current = payload.get("current") or {}
                im_pct = current.get("impliedMovePct")
                egc = compute_earnings_gamma_context(
                    ticker=t_upper,
                    as_of_date=dt.date.today().isoformat(),
                    dealer_gamma=dg,
                    tail_ignition=ti_data,
                    spot=spot,
                    implied_move_pct=im_pct,
                )
                payload["earningsGammaContext"] = egc.to_dict()
        except Exception as egc_err:
            LOG.debug(f"Earnings gamma context skipped for {ticker}: {egc_err}")

        if not has_trade_builder:
            with _breach_cache_lock:
                _breach_cache[key] = payload
        return payload
    except BreachInputError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception("ORATS failure")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure")
        raise HTTPException(status_code=500, detail="Internal error") from e


@app.get("/api/breach-compare")
def breach_compare(
    tickers: str = Query(..., description="Comma-separated list of tickers (max 10)"),
    k: float = Query(1.0, gt=0.0, description="Breach multiple (1.0, 1.5, 2.0)"),
    n: int = Query(10, ge=1, le=50, description="Number of earnings events to analyze"),
    years: int = Query(3, ge=1, le=10, description="Lookback years"),
):
    """
    Compare and rank multiple tickers for earnings plays.
    
    Returns ranked list with composite scores based on:
    - Breach rate (25%)
    - IV elevation (20%)
    - EM richness (15%)
    - Liquidity (15%)
    - Tail coverage (10%)
    - Market regime (10%)
    - Event risk (5%)
    """
    try:
        # Parse and validate tickers
        ticker_list = [t.strip().upper() for t in tickers.split(",") if t.strip()]
        ticker_list = list(dict.fromkeys(ticker_list))  # Dedupe, preserve order
        
        if not ticker_list:
            raise HTTPException(status_code=400, detail="No valid tickers provided")
        
        if len(ticker_list) > 10:
            raise HTTPException(status_code=400, detail="Maximum 10 tickers allowed")
        
        LOG.info(f"Breach compare: {len(ticker_list)} tickers at k={k}")
        
        # Fetch breach data for each ticker in PARALLEL for speed
        client = _get_client()
        benzinga_client = _get_benzinga_client_optional()
        base_flags = get_flags()
        
        payloads = []
        errors = []
        
        def fetch_single(ticker: str):
            """Fetch breach stats + goNoGo (for liquidity) for a single ticker."""
            payload = compute_breach_stats(
                client=client,
                ticker=ticker,
                n=n,
                years=years,
                k=k,
                trade_builder_inputs=None,
                flags_override=base_flags,
                benzinga_client=benzinga_client,
            )
            # Add goNoGo checks (includes critical liquidity data)
            try:
                payload["goNoGo"] = compute_go_no_go(
                    client, 
                    ticker=ticker, 
                    payload=payload, 
                    benzinga_client=benzinga_client
                )
            except Exception as e:
                LOG.warning(f"goNoGo failed for {ticker}: {e}")
                # Continue without goNoGo - liquidity will show as N/A
            return ticker, payload
        
        # Use ThreadPoolExecutor to fetch all tickers in parallel
        with ThreadPoolExecutor(max_workers=min(len(ticker_list), 5)) as executor:
            futures = {executor.submit(fetch_single, t): t for t in ticker_list}
            for future in as_completed(futures):
                ticker = futures[future]
                try:
                    _, payload = future.result(timeout=60)  # 60s per ticker (goNoGo adds time)
                    payloads.append((ticker, payload))
                except Exception as e:
                    LOG.warning(f"Failed to fetch {ticker}: {e}")
                    errors.append({"ticker": ticker, "error": str(e)})
        
        # Rank the tickers
        rankings = rank_tickers(payloads)
        tier_summary = summarize_tiers(rankings)
        
        return {
            "asOfDate": dt.date.today().isoformat(),
            "k": k,
            "n": n,
            "years": years,
            "tickersRequested": len(ticker_list),
            "tickersAnalyzed": len(payloads),
            "summary": tier_summary,
            "rankings": rankings,
            "errors": errors if errors else None,
        }
        
    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Unhandled failure (breach-compare)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@app.get("/compare")
def serve_compare():
    """Serve the compare page."""
    compare_path = STATIC_DIR / "compare.html"
    if not compare_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/compare.html")
    return FileResponse(str(compare_path))


# ---------------------------------------------------------------------------
# EODHD Earnings Calendar  (mega-cap $100 B+)
# ---------------------------------------------------------------------------

@app.get("/api/earnings-calendar")
async def earnings_calendar_api(
    view: str = Query("month", description="month|week"),
    anchor: str = Query("", description="YYYY-MM-DD anchor date"),
):
    """Earnings calendar for $100B+ market-cap companies (EODHD-only)."""
    import calendar as _cal
    from backend.eodhd_earnings_calendar import get_earnings_calendar

    today = dt.date.today()
    try:
        anchor_date = dt.date.fromisoformat(anchor[:10]) if anchor else today
    except Exception:
        anchor_date = today

    if view == "week":
        # Start from the Monday of the anchor week
        monday = anchor_date - dt.timedelta(days=anchor_date.weekday())
        start = monday
        end = monday + dt.timedelta(days=6)  # through Sunday
        label = f"Week of {start.strftime('%b %d, %Y')}"
    else:
        # Full calendar month (include leading/trailing days for the grid)
        first_of_month = anchor_date.replace(day=1)
        _, days_in_month = _cal.monthrange(first_of_month.year, first_of_month.month)
        last_of_month = first_of_month.replace(day=days_in_month)
        # Extend to fill the 7-column grid (Mon=0)
        start = first_of_month - dt.timedelta(days=first_of_month.weekday())
        end = last_of_month + dt.timedelta(days=6 - last_of_month.weekday())
        label = first_of_month.strftime("%B %Y")

    import asyncio
    try:
        loop = asyncio.get_event_loop()
        days = await loop.run_in_executor(None, lambda: get_earnings_calendar(start, end))
    except Exception as exc:
        LOG.exception("Earnings calendar failed")
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    return {
        "view": view,
        "anchor": anchor_date.isoformat(),
        "start": start.isoformat(),
        "end": end.isoformat(),
        "label": label,
        "days": days,
    }


@app.get("/api/calendar")
def calendar(
    view: str = Query("month", description="month|week|day"),
    anchor: str = Query(None, description="YYYY-MM-DD (anchor date)"),
    tz: str = Query("America/New_York"),
    engine1Only: int = Query(0, ge=0, le=1),
    includeEvents: int = Query(1, ge=0, le=1),
    maxTickers: int = Query(12000, ge=200, le=50000),
    minMarketCap: float = Query(0, ge=0, description="Min market cap filter in billions (e.g., 100 = $100B+)"),
):
    """
    Earnings calendar endpoint for the front page.

    Design goals:
    - One response for the visible range (month/week/day)
    - Macro events fetched once per range (Benzinga economics)
    - Earnings data from API Ninjas Premium
    """
    try:
        a = str(anchor or dt.date.today().isoformat())[:10]
        v = str(view or "month").strip().lower()
        if v not in ("month", "week", "day"):
            raise HTTPException(status_code=400, detail="Unsupported view. Allowed: month|week|day")
        e1 = bool(int(engine1Only))
        inc = bool(int(includeEvents))
        min_mcap_b = float(minMarketCap) if minMarketCap else 0.0

        flags_fp = get_flags().cache_fingerprint()
        cache_ttl_s = int(float(os.getenv("CALENDAR_CACHE_TTL_S") or 0))
        key = ("calendar", v, a, str(tz or ""), int(e1), int(inc), int(maxTickers), flags_fp)
        if cache_ttl_s > 0:
            with _calendar_cache_lock:
                cached = _calendar_cache.get(key)
            if cached is not None:
                return cached

        payload = build_calendar_payload(
            view=v,
            anchor=a,
            tz=tz,
            engine1_only=e1,
            include_events=inc,
            benzinga_client=_get_benzinga_client_optional(),
            max_tickers=int(maxTickers),
            min_market_cap_b=min_mcap_b,
            api_ninjas_client=_get_api_ninjas_client_optional(),
        )
        if cache_ttl_s > 0:
            with _calendar_cache_lock:
                _calendar_cache[key] = payload
        return payload
    except HTTPException:
        raise
    except ApiNinjasError as e:
        LOG.exception("API Ninjas failure (calendar)")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure (calendar)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@app.get("/api/transcripts/{ticker}")
def get_transcript_list(ticker: str):
    """
    Get list of available earnings call transcripts for a ticker.
    Returns the 4 most recent transcripts.
    """
    try:
        client = _get_api_ninjas_client_optional()
        if client is None:
            raise HTTPException(status_code=503, detail="API Ninjas unavailable")
        
        ticker = str(ticker).upper().strip()
        if not ticker:
            raise HTTPException(status_code=400, detail="Ticker required")
        
        transcripts = client.get_latest_transcripts(ticker, limit=4)
        return {
            "ticker": ticker,
            "transcripts": transcripts,
            "count": len(transcripts),
        }
    except HTTPException:
        raise
    except Exception as e:
        LOG.exception(f"Failed to fetch transcript list for {ticker}")
        raise HTTPException(status_code=500, detail="Internal error") from e


@app.get("/api/transcripts/{ticker}/{year}/{quarter}")
def get_transcript(ticker: str, year: int, quarter: int):
    """
    Get full earnings call transcript for a specific quarter.
    Returns the transcript text.
    """
    try:
        client = _get_api_ninjas_client_optional()
        if client is None:
            raise HTTPException(status_code=503, detail="API Ninjas unavailable")
        
        ticker = str(ticker).upper().strip()
        if not ticker:
            raise HTTPException(status_code=400, detail="Ticker required")
        if year < 2000 or year > 2100:
            raise HTTPException(status_code=400, detail="Invalid year")
        if quarter < 1 or quarter > 4:
            raise HTTPException(status_code=400, detail="Quarter must be 1-4")
        
        transcript = client.get_transcript(ticker, year, quarter)
        if transcript is None:
            raise HTTPException(status_code=404, detail=f"No transcript found for {ticker} {year}Q{quarter}")
        
        return transcript
    except HTTPException:
        raise
    except Exception as e:
        LOG.exception(f"Failed to fetch transcript for {ticker} {year}Q{quarter}")
        raise HTTPException(status_code=500, detail="Internal error") from e


@app.get("/api/transcripts/{ticker}/{year}/{quarter}/download")
def download_transcript(ticker: str, year: int, quarter: int):
    """
    Download earnings call transcript as a .txt file.
    """
    from fastapi.responses import Response
    
    try:
        client = _get_api_ninjas_client_optional()
        if client is None:
            raise HTTPException(status_code=503, detail="API Ninjas unavailable")
        
        ticker = str(ticker).upper().strip()
        if not ticker:
            raise HTTPException(status_code=400, detail="Ticker required")
        if year < 2000 or year > 2100:
            raise HTTPException(status_code=400, detail="Invalid year")
        if quarter < 1 or quarter > 4:
            raise HTTPException(status_code=400, detail="Quarter must be 1-4")
        
        transcript = client.get_transcript(ticker, year, quarter)
        if transcript is None:
            raise HTTPException(status_code=404, detail=f"No transcript found for {ticker} {year}Q{quarter}")
        
        # Build the text file content
        date_str = transcript.get("date", "Unknown date")
        timing = transcript.get("earnings_timing", "unknown")
        text = transcript.get("transcript", "No transcript available")
        
        content = f"""EARNINGS CALL TRANSCRIPT
========================
Ticker: {ticker}
Date: {date_str}
Quarter: Q{quarter} {year}
Timing: {timing}

========================
TRANSCRIPT
========================

{text}
"""
        
        filename = f"{ticker}_Q{quarter}_{year}_transcript.txt"
        
        return Response(
            content=content,
            media_type="text/plain",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"'
            }
        )
    except HTTPException:
        raise
    except Exception as e:
        LOG.exception(f"Failed to download transcript for {ticker} {year}Q{quarter}")
        raise HTTPException(status_code=500, detail="Internal error") from e


@app.get("/api/calendar-snapshot-status")
def calendar_snapshot_status():
    """
    Lightweight diagnostics for calendar earnings snapshots in Redis.

    Purpose: quickly confirm whether the calendar is using the FMP snapshot
    or falling back to the legacy ORATS snapshot (which can anchor estimates to Wednesday).
    """
    store = get_store_optional()
    if store is None:
        return {
            "ok": False,
            "redisAvailable": False,
            "error": "Redis unavailable (missing REDIS_URL).",
        }
    if not store.ping():
        return {
            "ok": False,
            "redisAvailable": False,
            "error": "Redis ping failed.",
        }

    def _summarize(snap):
        if not isinstance(snap, dict):
            return {"present": False, "meta": None, "byDateSize": 0}
        meta = snap.get("meta") if isinstance(snap.get("meta"), dict) else None
        by_date = snap.get("byDate") if isinstance(snap.get("byDate"), dict) else {}
        return {
            "present": True,
            "meta": meta,
            "byDateSize": int(len(by_date)),
        }

    fmp = _summarize(load_fmp_earnings_snapshot(store))
    orats = _summarize(load_earnings_snapshot(store))
    return {
        "ok": True,
        "redisAvailable": True,
        "keys": {
            "fmp": {"key": FMP_EARNINGS_SNAPSHOT_KEY, **fmp},
            "orats": {"key": EARNINGS_SNAPSHOT_KEY, **orats},
        },
    }


@app.get("/api/calendar-debug-earnings")
def calendar_debug_earnings(
    ticker: str = Query("TSLA", description="Ticker to probe (optional)"),
    date_from: str = Query(..., description="YYYY-MM-DD"),
    date_to: str = Query(..., description="YYYY-MM-DD"),
    max_rows: int = Query(2000, ge=1, le=20000),
):
    """
    Debug helper to diagnose missing tickers in the calendar.

    Returns a sanitized subset of Benzinga /calendar/earnings rows for the given date range,
    optionally filtered to a specific ticker.
    """
    try:
        bz = _get_benzinga_client_optional()
        if bz is None:
            raise HTTPException(status_code=503, detail="Benzinga unavailable or disabled.")

        d0 = str(date_from)[:10]
        d1 = str(date_to)[:10]
        t = str(ticker or "").strip().upper()

        pagesize = 1000
        max_pages = 50
        rows_all: list[dict] = []
        for page in range(max_pages):
            # IMPORTANT: use Benzinga server-side ticker filtering when provided.
            # Some feeds/plans can return sparse results for broad date-range queries
            # but return full coverage for per-ticker queries.
            resp = bz.calendar_earnings(
                tickers=(t if t else None),
                date_from=d0,
                date_to=d1,
                pagesize=pagesize,
                page=page,
            )
            batch = resp.rows or []
            rows_all.extend([r for r in batch if isinstance(r, dict)])
            if len(batch) < pagesize:
                break

        # Filter + sanitize
        out_rows: list[dict] = []
        for r in rows_all:
            sym = str(r.get("ticker") or r.get("symbol") or "").strip().upper()
            # If server-side tickers was used, sym should already match; keep this as a safety net.
            if t and sym != t:
                continue
            out_rows.append(
                {
                    "ticker": sym,
                    "date": str(r.get("date") or r.get("earnings_date") or "")[:10],
                    "time": str(r.get("time") or ""),
                    "date_confirmed": r.get("date_confirmed"),
                    "updated": r.get("updated") or r.get("updated_at") or r.get("updatedAt"),
                }
            )
            if len(out_rows) >= int(max_rows):
                break

        # Simple per-day counts
        by_day: dict[str, int] = {}
        for r in out_rows:
            dd = str(r.get("date") or "")[:10]
            if dd:
                by_day[dd] = int(by_day.get(dd, 0)) + 1

        return {
            "range": {"from": d0, "to": d1},
            "tickerFilter": t or None,
            "counts": {
                "rowsFetchedAll": len(rows_all),
                "rowsReturned": len(out_rows),
                "pagesize": pagesize,
                "maxPages": max_pages,
            },
            "byDay": {k: by_day[k] for k in sorted(by_day.keys())},
            "rows": out_rows,
        }
    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Unhandled failure (calendar-debug-earnings)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@app.get("/api/condor-rank")
def condor_rank(
    ticker: str = Query(..., description="US equity ticker"),
    n: int = Query(20, ge=5, le=50),
    years: int = Query(5, ge=1, le=10),
):
    """
    Iron Condor Rank endpoint (lightweight, cached).
    """
    try:
        t = str(ticker or "").strip().upper()
        key = (t, int(n), int(years), get_flags().cache_fingerprint())
        with _condor_rank_cache_lock:
            cached = _condor_rank_cache.get(key)
        if cached is not None:
            return cached

        payload = compute_condor_rank(_get_client(), ticker=t, n=int(n), years=int(years))
        with _condor_rank_cache_lock:
            _condor_rank_cache[key] = payload
        return payload
    except BreachInputError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception("ORATS failure (condor-rank)")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure (condor-rank)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@app.get("/api/macro-event-stats")
def macro_event_stats(
    key: str = Query(..., description="Macro event key (e.g., CPI, FOMC_RATE_DECISION, NFP)"),
    lookback_years: int = Query(5, ge=1, le=10),
    max_events: int = Query(60, ge=10, le=200),
):
    """
    On-demand macro event reaction stats (risk-only).
    Uses Benzinga economics history + SPY close-to-close returns.
    Cached to avoid repeated computation.
    """
    try:
        k = str(key or "").strip().upper()
        if not k:
            raise HTTPException(status_code=400, detail="Missing key.")
        cache_key = (k, int(lookback_years), int(max_events))
        with _macro_stats_cache_lock:
            cached = _macro_stats_cache.get(cache_key)
        if cached is not None:
            return cached

        bz = _get_benzinga_client_optional()
        if bz is None:
            raise HTTPException(status_code=503, detail="Benzinga unavailable or disabled.")
        client = _get_client_optional()
        if client is None:
            raise HTTPException(status_code=503, detail="ORATS unavailable (missing ORATS_TOKEN).")

        payload = compute_macro_event_stats(
            key=k,
            bz=bz,
            orats=client,
            lookback_years=int(lookback_years),
            max_events=int(max_events),
        )
        with _macro_stats_cache_lock:
            _macro_stats_cache[cache_key] = payload
        return payload
    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Unhandled failure (macro-event-stats)")
        raise HTTPException(status_code=500, detail="Internal error") from e


# ---------------------------------------------------------------------------
# News Risk Engine
# ---------------------------------------------------------------------------

_news_risk_cache: TTLCache = TTLCache(maxsize=10, ttl=30 * 60)  # 30 min TTL
_news_risk_cache_lock = threading.Lock()


@app.get("/api/news-risk")
def news_risk(
    week_offset: int = Query(0, ge=-12, le=12, description="Week offset: 0=current, 1=next, -1=last"),
):
    """
    News Risk Engine: Weekly view of macro events, analyst ratings, and news headlines
    with historical SPX impact data for event risk planning.
    """
    from backend.news_risk import build_news_risk_payload
    
    try:
        cache_key = ("news_risk", int(week_offset))
        with _news_risk_cache_lock:
            cached = _news_risk_cache.get(cache_key)
        if cached is not None:
            return cached

        bz = _get_benzinga_client_optional()
        if bz is None:
            raise HTTPException(status_code=503, detail="Benzinga unavailable or disabled.")
        
        orats = _get_client_optional()
        if orats is None:
            raise HTTPException(status_code=503, detail="ORATS unavailable (missing ORATS_TOKEN).")

        payload = build_news_risk_payload(
            bz=bz,
            orats=orats,
            week_offset=int(week_offset),
        )
        
        with _news_risk_cache_lock:
            _news_risk_cache[cache_key] = payload
        return payload
    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Unhandled failure (news-risk)")
        raise HTTPException(status_code=500, detail="Internal error") from e


# ---------------------------------------------------------------------------
# Backtest Engine
# ---------------------------------------------------------------------------

_backtest_cache: TTLCache = TTLCache(maxsize=20, ttl=60 * 60)  # 1 hour TTL
_backtest_cache_lock = threading.Lock()


@app.get("/api/backtest")
def backtest(
    engine: str = Query("engine3", description="engine3 (Red Dog) or engine4 (Ichimoku)"),
    trades: int = Query(50, ge=10, le=200, description="Number of trades: 25, 50, 100, 200"),
):
    """
    Backtest Engine 3 (Red Dog) or Engine 4 (Ichimoku) using historical A+ signals.
    
    Entry: Next day after signal if trigger price is hit
    Exit: At stop loss or target price
    Tracks performance segmented by market context alignment (gamma + trend).
    """
    from backend.backtest_engine import run_backtest
    
    try:
        # Validate engine
        eng = engine.lower().strip()
        if eng not in ("engine3", "engine4"):
            raise HTTPException(status_code=400, detail="Engine must be 'engine3' or 'engine4'")
        
        # Check cache
        cache_key = (eng, int(trades))
        with _backtest_cache_lock:
            cached = _backtest_cache.get(cache_key)
        if cached is not None:
            LOG.info(f"Backtest cache hit for {eng} x {trades}")
            return cached

        client = _get_client_optional()
        if client is None:
            raise HTTPException(status_code=503, detail="ORATS unavailable (missing ORATS_TOKEN).")

        result = run_backtest(
            client=client,
            engine=eng,
            trade_count=int(trades),
            max_workers=10,
        )
        
        payload = result.to_dict()
        
        with _backtest_cache_lock:
            _backtest_cache[cache_key] = payload
        return payload
    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Unhandled failure (backtest)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@app.get("/backtest")
def backtest_page():
    """Backtest Engine page for Engine 3/4 historical analysis."""
    backtest_path = STATIC_DIR / "backtest.html"
    if not backtest_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/backtest.html")
    return FileResponse(str(backtest_path))


# ---------------------------------------------------------------------------
# Raven-Tech 2.0 – Gate context helper
# ---------------------------------------------------------------------------

def _get_gate_context(flags) -> dict:
    """Gather regime, vol, and flow pressure context for gating decisions."""
    ctx = {
        "regime_label": "",
        "vol_direction": "",
        "fp_score": None,
        "fp_label": None,
        "gamma_ctx": None,
        "high_events_within_days": 0,
    }
    try:
        store = get_store_optional()
        if store and flags.ENABLE_ENGINE5_LEAD_LAG:
            snap = _engine5_get_best_snapshot(store, flags)
            if snap:
                data = snap.get("data", {})
                regime = data.get("regime", {})
                ctx["regime_label"] = regime.get("label") or regime.get("current_label") or ""
                vol = data.get("volLeadLag", {})
                ctx["vol_direction"] = vol.get("global_vol_direction") or vol.get("globalVolDirection") or ""
    except Exception:
        pass
    try:
        # Try to get flow pressure from cache
        with _fp_cache_lock:
            fp_data = _fp_cache.get("latest")
        if fp_data:
            fp = fp_data.get("flowPressure", {})
            ctx["fp_score"] = fp.get("composite_score")
            ctx["fp_label"] = fp.get("composite_label")
    except Exception:
        pass
    return ctx


# ---------------------------------------------------------------------------
# Engine 3: Red Dog Reversal Scanner
# ---------------------------------------------------------------------------

_engine3_cache: TTLCache = TTLCache(maxsize=20, ttl=30 * 60)
_engine3_cache_lock = threading.Lock()


@app.get("/api/engine3-red-dog")
def engine3_red_dog_scan(
    request: Request,
    date: Optional[str] = Query(None, description="Scan date (YYYY-MM-DD), defaults to today"),
    min_score: int = Query(50, ge=0, le=100, description="Minimum score to include"),
    direction: Optional[str] = Query(None, description="Filter by direction: bullish, bearish, or both"),
):
    """
    Engine 3: Red Dog Reversal Scanner

    Scans SP500 + Nasdaq100 (516 tickers) for Red Dog Reversal setups with A+ quality scoring.

    Returns setups categorized by grade:
    - aPlus: Score >= 75 (high-quality setups)
    - standard: Score 50-74 (decent setups)
    - watchlist: Combined and sorted by score
    """
    # Auth handled by middleware

    flags = get_flags()
    if not flags.ENABLE_ENGINE3_RED_DOG:
        raise HTTPException(
            status_code=503,
            detail="Engine 3 (Red Dog Reversal) is disabled. Set ENABLE_ENGINE3_RED_DOG=1 to enable.",
        )

    try:
        client = _get_client_optional()
        if client is None:
            raise HTTPException(status_code=503, detail="ORATS unavailable (missing ORATS_TOKEN).")

        # Normalize direction filter
        dir_filter = None
        if direction:
            d = str(direction).strip().lower()
            if d in ("bullish", "bull", "long"):
                dir_filter = "bullish"
            elif d in ("bearish", "bear", "short"):
                dir_filter = "bearish"

        # Check cache
        cache_key = (date, min_score, dir_filter)
        with _engine3_cache_lock:
            cached = _engine3_cache.get(cache_key)
        if cached is not None:
            return cached

        # Run scan
        result = compute_engine3_scan(
            client,
            as_of_date=date,
            min_score=min_score,
            direction=dir_filter,
            max_workers=flags.ENGINE3_MAX_WORKERS,
            use_cache=True,
        )

        # Inject gate decisions (Raven-Tech 2.0)
        if flags.ENABLE_GATING and isinstance(result, dict):
            try:
                gate_ctx = _get_gate_context(flags)
                for key in ("aPlus", "standard", "watchlist"):
                    setups = result.get(key)
                    if isinstance(setups, list):
                        gate_scan_results(
                            scan_results=setups,
                            engine="engine3_red_dog",
                            **gate_ctx,
                        )
                gs = summarize_gates(
                    (result.get("aPlus") or []) + (result.get("standard") or [])
                )
                result["gateSummary"] = gs
                result["gateContext"] = gate_ctx
            except Exception as gate_err:
                LOG.warning(f"Gate injection failed for engine3: {gate_err}")

        with _engine3_cache_lock:
            _engine3_cache[cache_key] = result

        return result

    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception("ORATS failure (engine3-red-dog)")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure (engine3-red-dog)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@app.get("/api/engine3-red-dog/{ticker}")
def engine3_red_dog_ticker(
    request: Request,
    ticker: str,
    date: Optional[str] = Query(None, description="Scan date (YYYY-MM-DD), defaults to today"),
):
    """
    Engine 3: Single ticker Red Dog analysis

    Analyzes a specific ticker for Red Dog Reversal setup with full indicator details.
    """
    # Auth handled by middleware

    flags = get_flags()
    if not flags.ENABLE_ENGINE3_RED_DOG:
        raise HTTPException(
            status_code=503,
            detail="Engine 3 (Red Dog Reversal) is disabled. Set ENABLE_ENGINE3_RED_DOG=1 to enable.",
        )

    try:
        client = _get_client_optional()
        if client is None:
            raise HTTPException(status_code=503, detail="ORATS unavailable (missing ORATS_TOKEN).")

        t = str(ticker or "").strip().upper()
        if not t:
            raise HTTPException(status_code=400, detail="Missing ticker.")

        result = compute_single_ticker_scan(
            client,
            ticker=t,
            as_of_date=date,
        )

        return result

    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception(f"ORATS failure (engine3-red-dog/{ticker})")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception(f"Unhandled failure (engine3-red-dog/{ticker})")
        raise HTTPException(status_code=500, detail="Internal error") from e


# ---------------------------------------------------------------------------
# Engine 4: Ichimoku Cloud Continuation Scanner
# ---------------------------------------------------------------------------

_engine4_cache: TTLCache = TTLCache(maxsize=20, ttl=30 * 60)
_engine4_cache_lock = threading.Lock()


@app.get("/api/engine4-ichimoku")
def engine4_ichimoku_scan(
    request: Request,
    date: Optional[str] = Query(None, description="Scan date (YYYY-MM-DD), defaults to today"),
    min_score: int = Query(50, ge=0, le=100, description="Minimum score to include"),
    direction: Optional[str] = Query(None, description="Filter by direction: bullish, bearish, or both"),
):
    """
    Engine 4: Ichimoku Cloud Continuation Scanner

    Scans SP500 + Nasdaq100 for Ichimoku continuation setups (Kijun pullback + Tenkan reclaim)
    with A+ quality scoring.

    Returns setups categorized by grade:
    - aPlus: Score >= 75 (high-quality setups)
    - others: Score 50-74 (decent setups)

    Features:
    - Standard Ichimoku settings (9/26/52)
    - Trend qualification (price vs cloud, Kijun slope)
    - Pullback detection (past Tenkan, near Kijun)
    - Entry triggers (Tenkan reclaim with candle quality)
    - Dealer gamma context (SPX for S&P, NDX for Nasdaq)
    - Earnings filter (downgrade if within 5 sessions)
    """
    flags = get_flags()
    if not flags.ENABLE_ENGINE4_ICHIMOKU:
        raise HTTPException(
            status_code=503,
            detail="Engine 4 (Ichimoku Continuation) is disabled. Set ENABLE_ENGINE4_ICHIMOKU=1 to enable.",
        )

    try:
        client = _get_client_optional()
        if client is None:
            raise HTTPException(status_code=503, detail="ORATS unavailable (missing ORATS_TOKEN).")

        # Normalize direction filter
        dir_filter = None
        if direction:
            d = str(direction).strip().lower()
            if d in ("bullish", "bull", "long"):
                dir_filter = "bullish"
            elif d in ("bearish", "bear", "short"):
                dir_filter = "bearish"

        # Check cache
        cache_key = (date, min_score, dir_filter)
        with _engine4_cache_lock:
            cached = _engine4_cache.get(cache_key)
        if cached is not None:
            return cached

        # Get Benzinga client if available for earnings check
        benzinga_client = _get_benzinga_client_optional()

        # Run scan
        result = compute_engine4_scan(
            client,
            as_of_date=date,
            min_score=min_score,
            direction=dir_filter,
            benzinga_client=benzinga_client,
            max_workers=flags.ENGINE4_MAX_WORKERS,
        )

        # Inject gate decisions (Raven-Tech 2.0)
        if flags.ENABLE_GATING and isinstance(result, dict):
            try:
                gate_ctx = _get_gate_context(flags)
                for key in ("actionable", "structure", "watchlist"):
                    setups = result.get(key)
                    if isinstance(setups, list):
                        gate_scan_results(
                            scan_results=setups,
                            engine="engine4_ichimoku",
                            **gate_ctx,
                        )
                gs = summarize_gates(
                    (result.get("actionable") or []) + (result.get("structure") or [])
                )
                result["gateSummary"] = gs
                result["gateContext"] = gate_ctx
            except Exception as gate_err:
                LOG.warning(f"Gate injection failed for engine4: {gate_err}")

        with _engine4_cache_lock:
            _engine4_cache[cache_key] = result

        return result

    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception("ORATS failure (engine4-ichimoku)")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure (engine4-ichimoku)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@app.get("/api/engine4-ichimoku/status")
def engine4_ichimoku_status(
    request: Request,
    refresh: bool = Query(False, description="Refresh signal statuses against current prices"),
    date: Optional[str] = Query(None, description="As-of date for refresh (YYYY-MM-DD)"),
):
    """
    Engine 4: Signal Status Tracker

    Returns current status of all tracked Ichimoku signals.
    
    If refresh=True, updates signal statuses based on current price action:
    - Checks if entry triggers have been hit
    - Checks if stops have been hit
    - Marks invalidated signals
    """
    flags = get_flags()
    if not flags.ENABLE_ENGINE4_ICHIMOKU:
        raise HTTPException(
            status_code=503,
            detail="Engine 4 (Ichimoku Continuation) is disabled.",
        )

    try:
        if refresh:
            client = _get_client_optional()
            if client is None:
                raise HTTPException(status_code=503, detail="ORATS unavailable for refresh.")
            
            refresh_result = refresh_engine4_statuses(client, as_of_date=date)
            return {
                "refreshed": True,
                **refresh_result,
                "signals": get_engine4_signals(),
            }
        
        return {
            "refreshed": False,
            "signals": get_engine4_signals(),
        }

    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Unhandled failure (engine4-ichimoku/status)")
        raise HTTPException(status_code=500, detail="Internal error") from e


@app.get("/api/engine4-ichimoku/{ticker}")
def engine4_ichimoku_ticker(
    request: Request,
    ticker: str,
    date: Optional[str] = Query(None, description="Scan date (YYYY-MM-DD), defaults to today"),
):
    """
    Engine 4: Single ticker Ichimoku analysis

    Analyzes a specific ticker for Ichimoku continuation setup with full details:
    - Complete Ichimoku state (Tenkan, Kijun, cloud, Chikou)
    - Trend regime qualification
    - Pullback state machine
    - Entry trigger detection
    - A+ scoring breakdown
    - Dealer gamma context
    """
    flags = get_flags()
    if not flags.ENABLE_ENGINE4_ICHIMOKU:
        raise HTTPException(
            status_code=503,
            detail="Engine 4 (Ichimoku Continuation) is disabled.",
        )

    try:
        client = _get_client_optional()
        if client is None:
            raise HTTPException(status_code=503, detail="ORATS unavailable (missing ORATS_TOKEN).")

        t = str(ticker or "").strip().upper()
        if not t:
            raise HTTPException(status_code=400, detail="Missing ticker.")

        benzinga_client = _get_benzinga_client_optional()

        result = compute_engine4_single_ticker(
            client,
            ticker=t,
            as_of_date=date,
            benzinga_client=benzinga_client,
        )

        return result

    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception(f"ORATS failure (engine4-ichimoku/{ticker})")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception(f"Unhandled failure (engine4-ichimoku/{ticker})")
        raise HTTPException(status_code=500, detail="Internal error") from e


# ---------------------------------------------------------------------------
# Engine 5 – Global Lead-Lag Engine
# ---------------------------------------------------------------------------


def _engine5_snapshot_response(snap: dict) -> dict:
    """Merge snapshot metadata into the data payload for the frontend."""
    meta = snap.get("meta", {})
    data = snap.get("data", {})
    # Merge meta at the top level so the frontend gets everything in one response
    data["meta"] = meta
    return data


def _engine5_get_best_snapshot(store, flags):
    """Return the best snapshot from cache, or None."""
    from backend.engine5_snapshot import select_best_snapshot
    return select_best_snapshot(
        store,
        max_age_days=flags.ENGINE5_SNAPSHOT_BEST_MAX_AGE_DAYS,
        snapshot_ttl=flags.ENGINE5_SNAPSHOT_TTL_S,
    )


@app.get("/api/engine5/weekly-ideas")
async def engine5_weekly_ideas(view: str = "best", date: str = ""):
    """Smart Engine 5 endpoint with immutable snapshot selection.

    Query parameter ``view``:
    - **best**  (default): Return the highest-quality recent snapshot (Grade A/B).
      If no A/B exists, return newest with a warning.  If NO snapshots exist at
      all, auto-bootstrap and run the pipeline, then return the result.
    - **latest**: Return the newest snapshot regardless of quality.
    - **asof**: Return snapshot matching ``date`` (YYYY-MM-DD) as the US as-of date.
    - **run**: Explicitly trigger a new pipeline run and return the new snapshot.
    """
    import asyncio

    flags = get_flags()
    if not flags.ENABLE_ENGINE5_LEAD_LAG:
        raise HTTPException(status_code=404, detail="Engine 5 is not enabled")

    store = get_store_optional()
    if store is None:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    # ---- view=run  --------------------------------------------------------
    if view == "run":
        from backend.engine5_pipeline import run_pipeline

        # Capture the current best snapshot BEFORE running, so we can compare
        best_before = _engine5_get_best_snapshot(store, flags)
        best_before_meta = best_before.get("meta", {}) if best_before else None

        try:
            loop = asyncio.get_event_loop()
            exit_code, snapshot_id = await loop.run_in_executor(
                None, lambda: run_pipeline(force=True, source="manual"),
            )
        except Exception as e:
            LOG.exception("Engine 5 pipeline run failed")
            raise HTTPException(status_code=500, detail=f"Pipeline error: {e}") from e

        if exit_code != 0 or snapshot_id is None:
            raise HTTPException(status_code=500, detail="Pipeline completed with errors. Check server logs.")

        snap = store.get_json(f"engine5:snapshot:{snapshot_id}")
        if snap is None:
            raise HTTPException(status_code=500, detail="Pipeline succeeded but snapshot not found.")

        resp = _engine5_snapshot_response(snap)

        # Embed the prior best snapshot metadata so the frontend can compare
        # and offer a "load best" option if the new run is sparser.
        if best_before_meta:
            new_meta = resp.get("meta", {})
            best_sid = best_before_meta.get("snapshotId", "")
            new_sid = new_meta.get("snapshotId", "")
            # Only attach if the best is different from what we just created
            if best_sid and best_sid != new_sid:
                new_meta["bestSnapshotMeta"] = best_before_meta
                resp["meta"] = new_meta

        return resp

    # ---- view=latest  -----------------------------------------------------
    if view == "latest":
        from backend.engine5_snapshot import select_latest_snapshot

        snap = select_latest_snapshot(store)
        if snap is not None:
            return _engine5_snapshot_response(snap)
        raise HTTPException(status_code=404, detail="No snapshots available yet.")

    # ---- view=asof  -------------------------------------------------------
    if view == "asof":
        if not date:
            raise HTTPException(status_code=400, detail="date parameter required for view=asof")
        from backend.engine5_snapshot import select_asof_snapshot

        snap = select_asof_snapshot(store, target_date=date)
        if snap is not None:
            return _engine5_snapshot_response(snap)
        raise HTTPException(status_code=404, detail=f"No snapshot found for as-of date {date}")

    # ---- view=best (default)  ---------------------------------------------
    snap = _engine5_get_best_snapshot(store, flags)
    if snap is not None:
        return _engine5_snapshot_response(snap)

    # No snapshots at all → auto-run pipeline (first-use bootstrapping)
    LOG.info("No Engine 5 snapshots found — auto-bootstrapping pipeline...")
    from backend.engine5_pipeline import run_pipeline

    try:
        loop = asyncio.get_event_loop()
        exit_code, snapshot_id = await loop.run_in_executor(
            None, lambda: run_pipeline(force=True, source="auto"),
        )
    except Exception as e:
        LOG.exception("Engine 5 auto-bootstrap failed")
        raise HTTPException(status_code=500, detail=f"Auto-bootstrap error: {e}") from e

    if exit_code != 0 or snapshot_id is None:
        raise HTTPException(
            status_code=500,
            detail="Auto-bootstrap pipeline completed with errors. Check server logs.",
        )

    snap = store.get_json(f"engine5:snapshot:{snapshot_id}")
    if snap is None:
        raise HTTPException(status_code=500, detail="Pipeline succeeded but snapshot not found.")

    return _engine5_snapshot_response(snap)


@app.get("/api/engine5/regime")
async def engine5_regime():
    """Return the current global regime state from the best snapshot."""
    flags = get_flags()
    if not flags.ENABLE_ENGINE5_LEAD_LAG:
        raise HTTPException(status_code=404, detail="Engine 5 is not enabled")

    store = get_store_optional()
    if store is None:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    snap = _engine5_get_best_snapshot(store, flags)
    if snap is None:
        raise HTTPException(status_code=404, detail="No regime data available")

    data = snap.get("data", {})
    regime_data = data.get("regime")
    if not regime_data:
        raise HTTPException(status_code=404, detail="No regime data in snapshot")

    return regime_data


@app.get("/api/engine5/signals")
async def engine5_signals():
    """Return lead-lag signals from the best snapshot (debugging/transparency)."""
    flags = get_flags()
    if not flags.ENABLE_ENGINE5_LEAD_LAG:
        raise HTTPException(status_code=404, detail="Engine 5 is not enabled")

    store = get_store_optional()
    if store is None:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    snap = _engine5_get_best_snapshot(store, flags)
    if snap is None:
        raise HTTPException(status_code=404, detail="No signal data available")

    # Signals are embedded in the WeeklyIdeas output under globalSignalSummary
    data = snap.get("data", {})
    summary = data.get("globalSignalSummary", {})
    return {"signals": summary, "meta": snap.get("meta", {})}


@app.get("/api/engine5/global-summary")
async def engine5_global_summary():
    """Return global bar summary from the best snapshot."""
    flags = get_flags()
    if not flags.ENABLE_ENGINE5_LEAD_LAG:
        raise HTTPException(status_code=404, detail="Engine 5 is not enabled")

    store = get_store_optional()
    if store is None:
        raise HTTPException(status_code=503, detail="Redis unavailable")

    snap = _engine5_get_best_snapshot(store, flags)
    if snap is None:
        raise HTTPException(status_code=404, detail="No global summary available")

    data = snap.get("data", {})
    meta = snap.get("meta", {})
    return {
        "globalSignalSummary": data.get("globalSignalSummary", {}),
        "regime": data.get("regime", {}),
        "meta": meta,
    }


# ---------------------------------------------------------------------------
# Engine 7: Thematic Relative Value (Pairs) Engine
# ---------------------------------------------------------------------------

_engine7_cache: TTLCache = TTLCache(maxsize=20, ttl=30 * 60)
_engine7_cache_lock = threading.Lock()


@app.get("/api/engine7-pairs")
def engine7_pairs_scan(
    request: Request,
    date: Optional[str] = Query(None, description="Scan date (YYYY-MM-DD), defaults to today"),
    min_score: int = Query(50, ge=0, le=100, description="Minimum confidence score to include"),
    tier: Optional[int] = Query(None, description="Filter by tier: 1, 2, or 3"),
    mode: Optional[str] = Query(None, description="Filter by mode: mean_reversion or momentum"),
):
    """Engine 7: Thematic Relative Value (Pairs) Scanner.

    Evaluates 20 fixed asset pairs using ratio-based statistical analysis
    combined with deterministic theme validation.

    Returns signals categorised into four buckets:
    - aPlus: ELIGIBLE, score >= 75, tradable
    - standard: ELIGIBLE, score >= threshold, tradable
    - watchlist: ELIGIBLE, below threshold, NOT tradable
    - ineligible: NOT_ELIGIBLE (no theme support), NOT tradable
    """
    flags = get_flags()
    if not flags.ENABLE_ENGINE7_PAIRS:
        raise HTTPException(
            status_code=503,
            detail="Engine 7 (Thematic Relative Value / Pairs) is disabled. Set ENABLE_ENGINE7_PAIRS=1 to enable.",
        )

    try:
        from backend.engine7_screener import compute_engine7_scan

        store = get_store_optional()

        result = compute_engine7_scan(
            as_of_date=date,
            enable_orats=flags.ENGINE7_ENABLE_ORATS_VOL,
            enable_llm_annotation=flags.ENGINE7_ENABLE_LLM_ANNOTATION,
            theme_required=flags.ENGINE7_THEME_REQUIRED,
            z_score_window=flags.ENGINE7_Z_SCORE_WINDOW,
            z_entry_threshold=flags.ENGINE7_Z_ENTRY_THRESHOLD,
            z_momentum_threshold=flags.ENGINE7_Z_MOMENTUM_THRESHOLD,
            min_score=min_score,
            aplus_threshold=flags.ENGINE7_APLUS_THRESHOLD,
            max_concurrent=flags.ENGINE7_MAX_CONCURRENT_PAIRS,
            max_workers=flags.ENGINE7_MAX_WORKERS,
            overlap_corr_threshold=flags.ENGINE7_OVERLAP_CORR_THRESHOLD,
            overlap_corr_window=flags.ENGINE7_OVERLAP_CORR_WINDOW,
            redis_store=store,
        )

        # Apply optional filters
        if tier is not None:
            for key in ("aPlus", "standard", "watchlist", "ineligible"):
                result[key] = [s for s in result.get(key, []) if s.get("tier") == tier]
        if mode is not None:
            m = str(mode).strip().lower()
            for key in ("aPlus", "standard", "watchlist", "ineligible"):
                result[key] = [s for s in result.get(key, []) if s.get("mode") == m]

        # Inject gating (INV-4)
        if flags.ENABLE_GATING:
            try:
                gate_ctx = _get_gate_context(flags)
                from backend.gating import gate_engine7_pair
                for key in ("aPlus", "standard"):
                    for sig in result.get(key, []):
                        if isinstance(sig, dict):
                            gd = gate_engine7_pair(
                                signal=sig,
                                regime_label=gate_ctx.get("regime_label", ""),
                                vol_direction=gate_ctx.get("vol_direction", ""),
                                fp_score=gate_ctx.get("fp_score"),
                                regime_allow=flags.GATE_PAIRS_REGIME_ALLOW,
                                vol_state_allow=flags.GATE_PAIRS_VOL_STATE_ALLOW,
                            )
                            sig["gateDecision"] = gd.to_dict()
            except Exception as gate_err:
                LOG.warning("Gate injection failed for engine7: %s", gate_err)

        return result

    except Exception as exc:
        LOG.exception("Engine7 scan failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Engine 7 scan failed: {exc}")


@app.post("/api/engine7-pairs/clear-cache")
def engine7_clear_cache():
    """Force-clear Engine 7 in-memory caches (scan + theme). Bars kept."""
    from backend.engine7_screener import clear_engine7_caches
    clear_engine7_caches()
    return {"status": "ok", "message": "Engine 7 scan and theme caches cleared"}


@app.post("/api/engine7-pairs/nightly-review")
def engine7_nightly_review(
    date: Optional[str] = Query(None, description="Review date (YYYY-MM-DD), defaults to today"),
):
    """Engine 7: Run the LLM nightly theme review pipeline.

    Analyzes recent headlines with gpt-5.2 to identify emerging macro
    narratives not covered by the static theme list.  New themes are
    auto-promoted via a two-track system (immediate at 10%+ saturation,
    or after 2-of-3 consecutive nightly confirmations).

    Call via cron: 0 5 * * * curl -X POST http://localhost:8000/api/engine7-pairs/nightly-review
    """
    try:
        from backend.engine7_llm_review import review_and_propose
        from backend.engine7_screener import clear_engine7_caches

        result = review_and_propose(date_str=date)
        clear_engine7_caches()
        return result

    except Exception as exc:
        LOG.exception("Engine7 nightly review failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Engine 7 nightly review failed: {exc}")


@app.get("/api/engine7-pairs/dynamic-themes")
def engine7_dynamic_themes():
    """Engine 7: View current dynamic themes (active + pending) and review status."""
    try:
        from backend.engine7_llm_review import _read_store, _LLM_MODEL, _MAX_ACTIVE_DYNAMIC, _EXPIRY_DAYS
        store = _read_store()
        themes = store.get("themes", {})
        active = {k: v for k, v in themes.items() if v.get("status") == "active"}
        pending = {k: v for k, v in themes.items() if v.get("status") == "pending"}
        return {
            "lastReview": store.get("last_review"),
            "model": _LLM_MODEL,
            "maxActive": _MAX_ACTIVE_DYNAMIC,
            "expiryDays": _EXPIRY_DAYS,
            "activeCount": len(active),
            "pendingCount": len(pending),
            "active": active,
            "pending": pending,
            "themes": themes,
            "auditLog": store.get("audit_log", [])[-20:],
        }
    except Exception as exc:
        LOG.exception("Engine7 dynamic themes read failed: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


_E7_DESK_VIEW_SYSTEM = """You are a senior quant on a systematic relative-value desk.
A junior trader has clicked on a pair trade signal and needs your guidance.

You will receive a JSON payload describing a specific pair signal: the two assets,
trade mode (mean reversion or momentum), z-score, momentum metrics, confidence score,
active themes, tier, and other context.

Write a concise desk briefing in this exact JSON structure:

{
  "thesis": "2-3 sentences: WHY this pair, what is the structural relationship, why the spread is dislocated right now.",
  "market_context": "1-2 sentences: what macro/narrative backdrop supports this trade. Reference the active themes.",
  "how_to_enter": "2-3 sentences: specific entry mechanics — which leg to buy, which to sell, sizing guidance (risk units), and where the spread needs to be.",
  "how_to_exit": "2-3 sentences: target exit conditions — z-score mean reversion level, time stop, or momentum exhaustion signal.",
  "what_breaks_it": "2-3 sentences: the specific scenario that invalidates this trade — theme reversal, correlation breakdown, or regime shift.",
  "risk_management": "1-2 sentences: position sizing, max loss, correlation considerations with other active pairs.",
  "learning_note": "1-2 sentences: a teaching moment — what general principle this trade illustrates about relative value or spread trading."
}

Rules:
- Write as a senior quant talking to a junior: clear, direct, no jargon without explanation.
- Reference the ACTUAL data in the signal (z-score value, momentum readings, themes).
- Be specific about the two assets — use their full names, not just tickers.
- If it's a mean reversion trade, explain the z-score reversion thesis.
- If it's a momentum trade, explain the trend-break continuation thesis.
- Keep each field under 80 words.
- Output valid JSON only."""


@app.post("/api/engine7-pairs/desk-view")
def engine7_desk_view(body: dict):
    """Engine 7: Generate a GPT-5.2 senior quant desk view for a pair signal."""
    signal = body.get("signal")
    if not signal:
        raise HTTPException(status_code=400, detail="Missing 'signal' in request body")

    try:
        from backend.llm_client import _get_openai_client, _parse_desk_brief_json

        client = _get_openai_client()
        if client is None:
            raise HTTPException(status_code=503, detail="OpenAI client unavailable")

        import json as _json
        payload = _json.dumps(signal, default=str)
        if len(payload) > 8000:
            payload = payload[:8000]

        resp = client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {"role": "system", "content": _E7_DESK_VIEW_SYSTEM},
                {"role": "user", "content": payload},
            ],
            temperature=0.3,
            max_completion_tokens=1200,
            timeout=30,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or ""
        parsed = _parse_desk_brief_json(content)
        if parsed is None:
            raise HTTPException(status_code=502, detail="LLM returned unparseable response")

        parsed["_source"] = "gpt-5.2"
        parsed["_pair"] = signal.get("pair_id", "")
        return parsed

    except HTTPException:
        raise
    except Exception as exc:
        LOG.exception("Engine7 desk-view failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Desk view generation failed: {exc}")


@app.get("/api/engine7-pairs/themes")
def engine7_pairs_themes(
    request: Request,
    date: Optional[str] = Query(None, description="Date (YYYY-MM-DD), defaults to today"),
):
    """Engine 7: Active themes from the deterministic classifier.

    If LLM annotation is enabled and available, includes it as a separate
    llmAnnotation field.  Shows which pairs each theme enables.
    """
    flags = get_flags()
    if not flags.ENABLE_ENGINE7_PAIRS:
        raise HTTPException(
            status_code=503,
            detail="Engine 7 (Thematic Relative Value / Pairs) is disabled.",
        )

    try:
        import datetime as _dt
        from backend.engine7_theme import (
            THEME_PAIR_ELIGIBILITY,
            annotate_themes_llm,
            classify_themes_deterministic,
            fetch_headlines,
        )

        today = _dt.date.today()
        if date:
            try:
                today = _dt.date.fromisoformat(str(date)[:10])
            except Exception:
                today = _dt.date.today()

        date_str = today.isoformat()
        headlines = fetch_headlines(date_str, lookback_days=7)
        theme_result = classify_themes_deterministic(headlines)

        active = []
        for t in theme_result.themes:
            if not t.active:
                continue
            eligible_pairs = THEME_PAIR_ELIGIBILITY.get(t.theme, [])
            active.append({
                **t.to_dict(),
                "eligiblePairs": eligible_pairs,
            })

        out: dict = {
            "date": date_str,
            "headlineCount": theme_result.headline_count,
            "activeThemes": active,
            "allThemes": [t.to_dict() for t in theme_result.themes],
        }

        if flags.ENGINE7_ENABLE_LLM_ANNOTATION:
            store = get_store_optional()
            llm_ann = annotate_themes_llm(headlines, date_str, store=store)
            out["llmAnnotation"] = llm_ann

        return out

    except Exception as exc:
        LOG.exception("Engine7 themes failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Engine 7 themes failed: {exc}")


@app.get("/api/engine7-pairs/{pair_id}")
def engine7_pairs_detail(
    request: Request,
    pair_id: str,
    date: Optional[str] = Query(None, description="Scan date (YYYY-MM-DD), defaults to today"),
):
    """Engine 7: Single pair deep-dive analysis.

    Returns full analysis for one pair including ratio chart data, z-score
    history, theme alignment detail, ORATS overlay status, and overlap flags.
    """
    flags = get_flags()
    if not flags.ENABLE_ENGINE7_PAIRS:
        raise HTTPException(
            status_code=503,
            detail="Engine 7 (Thematic Relative Value / Pairs) is disabled.",
        )

    try:
        from backend.engine7_screener import analyze_single_pair_detail

        store = get_store_optional()

        result = analyze_single_pair_detail(
            pair_id=pair_id,
            as_of_date=date,
            enable_orats=flags.ENGINE7_ENABLE_ORATS_VOL,
            enable_llm_annotation=flags.ENGINE7_ENABLE_LLM_ANNOTATION,
            theme_required=flags.ENGINE7_THEME_REQUIRED,
            z_score_window=flags.ENGINE7_Z_SCORE_WINDOW,
            z_entry_threshold=flags.ENGINE7_Z_ENTRY_THRESHOLD,
            z_momentum_threshold=flags.ENGINE7_Z_MOMENTUM_THRESHOLD,
            min_score=flags.ENGINE7_MIN_SCORE_DEFAULT,
            aplus_threshold=flags.ENGINE7_APLUS_THRESHOLD,
            redis_store=store,
        )

        if result is None:
            raise HTTPException(status_code=404, detail=f"Pair '{pair_id}' not found in library")

        if "error" in result:
            raise HTTPException(status_code=502, detail=result["error"])

        return result

    except HTTPException:
        raise
    except Exception as exc:
        LOG.exception("Engine7 detail failed for %s: %s", pair_id, exc)
        raise HTTPException(status_code=500, detail=f"Engine 7 detail failed: {exc}")


# ---------------------------------------------------------------------------
# Raven-Tech 2.0 – Command Center & Flow Pressure
# ---------------------------------------------------------------------------


@app.get("/command-center")
def command_center_page():
    """Command Center: weekly planning + intraday monitoring."""
    cc_path = STATIC_DIR / "command-center.html"
    if not cc_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/command-center.html")
    return FileResponse(str(cc_path))


@app.get("/research-lab")
def research_lab_page():
    """Research Lab: LLM feature discovery + backtest queue."""
    rl_path = STATIC_DIR / "research-lab.html"
    if not rl_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/research-lab.html")
    return FileResponse(str(rl_path))


_fp_cache = TTLCache(maxsize=10, ttl=60)
_fp_cache_lock = threading.Lock()

_desk_brief_cache = TTLCache(maxsize=5, ttl=30 * 60)
_desk_brief_cache_lock = threading.Lock()

_sequencer_events: Dict[str, List[dict]] = {}  # in-memory store: week_id -> events
_sequencer_lock = threading.Lock()
_sequencer_prior_state: Dict[str, str] = {}  # previous state for change detection


_cc_init_lock = threading.Lock()
_cc_init_running = False


def _emit_sequencer_events_from_snapshot(store, snapshot_id: str) -> None:
    """Compare new snapshot state to prior state and emit SequencerEvents.

    Mirrors the logic in scripts/refresh_engine5_snapshot.py so the Command
    Center bootstrap also feeds the Weekly Signal Sequencer.
    """
    try:
        from backend.sequencer import detect_state_changes, current_week_id

        snap = store.get_json(f"engine5:snapshot:{snapshot_id}")
        if not snap:
            return

        data = snap.get("data", {})
        regime = data.get("regime", {})
        vol = data.get("volLeadLag", {})

        current_state = {
            "regime": regime.get("label") or regime.get("current_label") or "",
            "vol_leadlag": vol.get("vol_lag_state") or vol.get("volLagState") or "",
        }

        prior_raw = store.get_json("sequencer:prior_state")
        prior_state = prior_raw if isinstance(prior_raw, dict) else {}

        if prior_state:
            events = detect_state_changes(previous=prior_state, current=current_state)
            if events:
                wid = current_week_id()
                existing_raw = store.get_json(f"sequencer:week:{wid}")
                existing = existing_raw if isinstance(existing_raw, list) else []
                for ev in events:
                    existing.append(ev.to_dict())
                    LOG.info("Sequencer event: %s (%s -> %s)", ev.event_type, ev.from_state, ev.to_state)
                store.set_json(f"sequencer:week:{wid}", existing, ttl_s=30 * 86400)
            else:
                LOG.info("No sequencer state changes detected.")
        else:
            LOG.info("No prior sequencer state; initializing baseline.")

        # Save current state as prior for next run
        store.set_json("sequencer:prior_state", current_state, ttl_s=7 * 86400)
    except Exception as e:
        LOG.warning("Sequencer event emission failed: %s", e)


def _ensure_engine5_snapshot(flags) -> dict | None:
    """Return the best Engine 5 snapshot, auto-bootstrapping if needed."""
    store = get_store_optional()
    if not store or not flags.ENABLE_ENGINE5_LEAD_LAG:
        return None

    snap = _engine5_get_best_snapshot(store, flags)
    if snap is not None:
        # Even for existing snapshots, ensure sequencer baseline is set
        meta = snap.get("meta", {})
        sid = meta.get("snapshot_id") or meta.get("id")
        if sid:
            _emit_sequencer_events_from_snapshot(store, sid)
        return snap

    # No snapshot → auto-bootstrap pipeline (same logic as the /api/engine5/weekly-ideas endpoint)
    try:
        LOG.info("Command Center: auto-bootstrapping Engine 5 pipeline...")
        from backend.engine5_pipeline import run_pipeline
        exit_code, snapshot_id = run_pipeline(force=True, source="command_center")
        if exit_code == 0 and snapshot_id:
            # Emit sequencer events from the newly created snapshot
            _emit_sequencer_events_from_snapshot(store, snapshot_id)
            snap = store.get_json(f"engine5:snapshot:{snapshot_id}")
            return snap
    except Exception as e:
        LOG.warning("Engine 5 auto-bootstrap failed: %s", e)
    return None


def _ensure_engine3_cache(flags) -> None:
    """Run Engine 3 scan if no cached results exist."""
    if not flags.ENABLE_ENGINE3_RED_DOG:
        return
    with _engine3_cache_lock:
        if len(_engine3_cache) > 0:
            return  # already have cached results
    try:
        client = _get_client_optional()
        if not client:
            return
        LOG.info("Command Center: auto-running Engine 3 (Red Dog) scan...")
        result = compute_engine3_scan(
            client,
            as_of_date=None,
            min_score=50,
            direction=None,
            max_workers=flags.ENGINE3_MAX_WORKERS,
            use_cache=True,
        )
        # Inject gate decisions
        if flags.ENABLE_GATING and isinstance(result, dict):
            try:
                gate_ctx = _get_gate_context(flags)
                for key in ("aPlus", "standard", "watchlist"):
                    setups = result.get(key)
                    if isinstance(setups, list):
                        gate_scan_results(scan_results=setups, engine="engine3_red_dog", **gate_ctx)
                gs = summarize_gates((result.get("aPlus") or []) + (result.get("standard") or []))
                result["gateSummary"] = gs
                result["gateContext"] = gate_ctx
            except Exception:
                pass
        cache_key = (None, 50, None)
        with _engine3_cache_lock:
            _engine3_cache[cache_key] = result
        LOG.info("Command Center: Engine 3 scan complete (%d setups)", result.get("setupsFound", 0))
    except Exception as e:
        LOG.warning("Engine 3 auto-scan failed: %s", e)


def _ensure_engine4_cache(flags) -> None:
    """Run Engine 4 scan if no cached results exist."""
    if not flags.ENABLE_ENGINE4_ICHIMOKU:
        return
    with _engine4_cache_lock:
        if len(_engine4_cache) > 0:
            return  # already have cached results
    try:
        client = _get_client_optional()
        if not client:
            return
        LOG.info("Command Center: auto-running Engine 4 (Ichimoku) scan...")
        benzinga_client = _get_benzinga_client_optional()
        result = compute_engine4_scan(
            client,
            as_of_date=None,
            min_score=50,
            direction=None,
            benzinga_client=benzinga_client,
            max_workers=flags.ENGINE4_MAX_WORKERS,
        )
        # Inject gate decisions
        if flags.ENABLE_GATING and isinstance(result, dict):
            try:
                gate_ctx = _get_gate_context(flags)
                for key in ("actionable", "structure", "watchlist"):
                    setups = result.get(key)
                    if isinstance(setups, list):
                        gate_scan_results(scan_results=setups, engine="engine4_ichimoku", **gate_ctx)
                gs = summarize_gates((result.get("actionable") or []) + (result.get("structure") or []))
                result["gateSummary"] = gs
                result["gateContext"] = gate_ctx
            except Exception:
                pass
        cache_key = (None, 50, None)
        with _engine4_cache_lock:
            _engine4_cache[cache_key] = result
        LOG.info("Command Center: Engine 4 scan complete (%d actionable)", result.get("actionableCount", 0))
    except Exception as e:
        LOG.warning("Engine 4 auto-scan failed: %s", e)


@app.get("/api/command-center/init")
def api_command_center_init():
    """Bootstrap all data the Command Center needs.

    Runs Engine 5 (regime/vol), Engine 3 (Red Dog), and Engine 4 (Ichimoku)
    in parallel if their caches are empty. Returns immediately with status
    if already running from another request.
    """
    global _cc_init_running
    with _cc_init_lock:
        if _cc_init_running:
            return {"status": "already_running"}
        _cc_init_running = True

    flags = get_flags()
    results = {"engine5": "skipped", "engine3": "skipped", "engine4": "skipped"}

    def _run():
        global _cc_init_running
        try:
            # Engine 5 first (regime/vol data feeds into gating)
            try:
                snap = _ensure_engine5_snapshot(flags)
                results["engine5"] = "ok" if snap else "no_data"
            except Exception as e:
                results["engine5"] = f"error: {e}"

            # Engine 3 and 4 in parallel
            with ThreadPoolExecutor(max_workers=2) as pool:
                f3 = pool.submit(_ensure_engine3_cache, flags)
                f4 = pool.submit(_ensure_engine4_cache, flags)
                try:
                    f3.result(timeout=300)
                    results["engine3"] = "ok"
                except Exception as e:
                    results["engine3"] = f"error: {e}"
                try:
                    f4.result(timeout=300)
                    results["engine4"] = "ok"
                except Exception as e:
                    results["engine4"] = f"error: {e}"
        finally:
            with _cc_init_lock:
                _cc_init_running = False

    # Run in a background thread so the response returns immediately
    threading.Thread(target=_run, daemon=True).start()
    return {"status": "initializing", "message": "Bootstrapping engines in background..."}


@app.get("/api/command-center/flow-pressure")
def api_flow_pressure():
    """Flow Pressure snapshot across SPX, QQQ, and sector ETFs."""
    with _fp_cache_lock:
        cached = _fp_cache.get("latest")
    if cached is not None:
        return cached

    import datetime as _dt
    now = _dt.datetime.utcnow().isoformat() + "Z"

    # Gather regime and vol state from Engine 5 (auto-bootstrap if needed)
    regime_data = {}
    vol_data = {}
    try:
        flags = get_flags()
        snap = _ensure_engine5_snapshot(flags)
        if snap:
            data = snap.get("data", {})
            regime_data = data.get("regime", {})
            vol_data = data.get("volLeadLag", {})
    except Exception:
        pass

    # Build Flow Pressure for SPX (primary), QQQ, and sector ETFs
    symbols = ["SPX", "QQQ", "XLF", "XLK", "XLE", "XLU", "XLV", "XLI"]
    readings = []

    # Fetch shared vol metrics from ORATS cores (SPY as proxy)
    shared_iv7 = None
    shared_iv30 = None
    shared_rv10 = None
    shared_adv = None
    try:
        client = _get_client_optional()
        if client:
            core_rows = client.cores(ticker="SPY", fields="ticker,tradeDate,iv7,iv7d,iv30,iv30d,iv").rows or []
            if core_rows:
                row = core_rows[-1] if isinstance(core_rows, list) else core_rows
                if isinstance(row, dict):
                    for k in ("iv7", "iv7d"):
                        v = row.get(k)
                        if v is not None:
                            shared_iv7 = float(v) * 100.0  # ORATS returns decimal
                            break
                    for k in ("iv30", "iv30d", "iv"):
                        v = row.get(k)
                        if v is not None:
                            shared_iv30 = float(v) * 100.0
                            break
                else:
                    # Row might be an object with attributes
                    for k in ("iv7", "iv7d"):
                        v = getattr(row, k, None)
                        if v is not None:
                            shared_iv7 = float(v) * 100.0
                            break
                    for k in ("iv30", "iv30d", "iv"):
                        v = getattr(row, k, None)
                        if v is not None:
                            shared_iv30 = float(v) * 100.0
                            break

            # ADV from SPY volume (liquid enough to always be high)
            shared_adv = 5_000_000_000.0  # SPY trades ~$30B/day
    except Exception as e:
        LOG.debug("Flow Pressure: ORATS cores fetch failed: %s", e)

    # Fallback: derive vol metrics from Engine 5 data when ORATS is unavailable
    if vol_data:
        if shared_rv10 is None:
            rv_raw = vol_data.get("us_rv10") or vol_data.get("rv10")
            if rv_raw is not None:
                try:
                    shared_rv10 = float(rv_raw)
                except (ValueError, TypeError):
                    pass
        # If Engine 5 has US IV state, derive approximate iv7/iv30
        if shared_iv7 is None:
            us_iv = vol_data.get("us_iv_state", "").upper()
            # Map Engine 5 states to approximate IV levels for scoring
            _iv_approx = {"ELEVATED": 25.0, "HIGH": 30.0, "NORMAL": 16.0, "NEUTRAL": 16.0, "LOW": 12.0}
            if us_iv in _iv_approx:
                shared_iv7 = _iv_approx[us_iv]
        if shared_iv30 is None and shared_iv7 is not None:
            # Approximate IV30 based on typical term structure
            vol_dir = vol_data.get("global_vol_direction", "").lower()
            if vol_dir in ("rising", "expanding"):
                shared_iv30 = shared_iv7 * 0.90  # Inverted term structure
            else:
                shared_iv30 = shared_iv7 * 1.10  # Normal contango

    # Fetch macro event density from Benzinga
    event_count_5d = 0
    high_severity_count = 0
    try:
        bz = _get_benzinga_client_optional()
        if bz:
            import datetime as _dtm
            from backend.macro_events import macro_events_by_date
            today = _dtm.date.today()
            end_date = today + _dtm.timedelta(days=5)
            macro_by_date, _, _ = macro_events_by_date(
                bz=bz, start=today, end=end_date, importance_min=3,
            )
            for day_events in macro_by_date.values():
                for ev in day_events:
                    event_count_5d += 1
                    imp = ev.get("importance") or ev.get("severity") or 0
                    try:
                        if int(imp) >= 4:
                            high_severity_count += 1
                    except (ValueError, TypeError):
                        pass
    except Exception as e:
        LOG.debug("Flow Pressure: Benzinga macro fetch failed: %s", e)

    for sym in symbols:
        # Use gamma context from SPX for index symbols, simplified for sectors
        gamma_ctx = None
        try:
            cl = _get_client_optional()
            if cl and sym in ("SPX", "QQQ"):
                from backend.dealer_gamma_context import compute_dealer_gamma_context
                sym_for_strikes = "SPXW" if sym == "SPX" else sym
                rows = cl.live_strikes(ticker=sym_for_strikes, fields="strike,gamma,callOpenInterest,putOpenInterest,spotPrice").rows or []
                if rows:
                    gamma_ctx = compute_dealer_gamma_context(rows)
        except Exception:
            pass

        fp = compute_flow_pressure(
            symbol=sym,
            timestamp=now,
            gamma_ctx=gamma_ctx,
            iv7=shared_iv7,
            iv30=shared_iv30,
            rv10=shared_rv10,
            adv_20d=shared_adv if sym in ("SPX", "QQQ", "SPY") else None,
            event_count_5d=event_count_5d,
            high_severity_count=high_severity_count,
        )
        readings.append(fp)

    snapshot = compute_flow_pressure_snapshot(readings, timestamp=now)
    payload = {
        "flowPressure": snapshot.to_dict(),
        "regime": regime_data,
        "volState": vol_data,
    }

    with _fp_cache_lock:
        _fp_cache["latest"] = payload
    return payload


@app.get("/api/command-center/sequencer")
def api_sequencer():
    """Weekly Signal Sequencer: timeline of signal flips this week."""
    wid = current_week_id()

    # Try Redis first, fall back to in-memory
    events = []
    try:
        store = get_store_optional()
        if store:
            redis_events = store.get_json(f"sequencer:week:{wid}")
            if isinstance(redis_events, list):
                events = redis_events
    except Exception:
        pass

    if not events:
        with _sequencer_lock:
            events = _sequencer_events.get(wid, [])

    seq_events = [SequencerEvent.from_dict(e) for e in events]
    seq = build_weekly_sequence(wid, seq_events)

    # Build day-grouped timeline dict for the frontend
    trading_days = week_trading_days()
    timeline: Dict[str, list] = {d: [] for d in trading_days}
    _event_type_labels = {
        "REGIME_FLIP": "Regime",
        "FLOW_PRESSURE_FLIP": "Flow Pressure",
        "DEALER_GAMMA_SHIFT": "Gamma",
        "VOL_LEADLAG_FLIP": "Vol Lead-Lag",
        "EARNINGS_DISPERSION_SPIKE": "Earnings Disp.",
        "RED_DOG_BREADTH_CHANGE": "Red Dog",
        "ICHIMOKU_BREADTH_CHANGE": "Ichimoku",
    }
    for ev_dict in seq.events:
        ev_date = ev_dict.get("date", "")
        if ev_date in timeline:
            et = ev_dict.get("event_type", "")
            timeline[ev_date].append({
                "label": _event_type_labels.get(et, et.replace("_", " ").title()),
                "event_type": et,
                "from_state": ev_dict.get("from_state", ""),
                "to_state": ev_dict.get("to_state", ""),
                "summary": ev_dict.get("summary", ""),
                "source_engine": ev_dict.get("source_engine", ""),
            })

    # Build matched_pattern object for the frontend
    matched_pattern = {}
    if seq.pattern_match and seq.pattern_match in PATTERN_TEMPLATES:
        tmpl = PATTERN_TEMPLATES[seq.pattern_match]
        matched_pattern = {
            "key": seq.pattern_match,
            "label": tmpl.get("label", seq.pattern_match),
            "confidence": round(seq.pattern_confidence * 100),
            "primary_risk": seq.primary_risk,
            "favored_play_types": seq.favored_play_types,
        }

    raw_seq = seq.to_dict()
    raw_seq["timeline"] = timeline
    raw_seq["matched_pattern"] = matched_pattern

    return {
        "weekId": wid,
        "tradingDays": trading_days,
        "sequence": raw_seq,
        "patterns": {k: {"label": v["label"], "description": v["description"]}
                     for k, v in PATTERN_TEMPLATES.items()},
    }


def _build_deterministic_desk_brief(context: dict) -> dict:
    """Build a data-driven Desk Brief from available engine data.

    Used when LLM is disabled, API key is missing, or LLM call fails.
    Produces three concise sentences from the actual numbers.
    """
    # Market State
    fp = context.get("flow_pressure", {})
    regime = context.get("regime", {})
    vol = context.get("vol_state", {})
    fp_label = fp.get("composite_label", "Unknown")
    fp_score = fp.get("composite_score")
    regime_label = regime.get("label", "Unknown")
    vol_dir = vol.get("direction") or "unknown"

    state_parts = []
    if regime_label and regime_label != "Unknown":
        state_parts.append(f"Regime is {regime_label}")
    if fp_label and fp_label != "Unknown" and fp_score is not None:
        state_parts.append(f"Flow Pressure {fp_label} ({fp_score:.0f})")
    if vol_dir and vol_dir != "unknown":
        state_parts.append(f"vol {vol_dir}")
    market_state = ", ".join(state_parts) + "." if state_parts else "Awaiting engine data."

    # Weekly Bias
    pattern = context.get("matched_pattern")
    gate_summary = context.get("gate_summary", {})
    tradable_ct = gate_summary.get("TRADABLE", 0)
    watch_ct = gate_summary.get("WATCH", 0)
    suppress_ct = gate_summary.get("SUPPRESS", 0)
    total_ideas = context.get("tradable_ideas_count", 0)

    bias_parts = []
    if pattern:
        bias_parts.append(f"Pattern: {pattern}")
    if fp_label == "Risk-On":
        bias_parts.append("continuation and premium selling favored")
    elif fp_label == "Risk-Off":
        bias_parts.append("mean reversion and defined risk favored")
    elif fp_label == "Neutral":
        bias_parts.append("selectivity — higher quality setups only")
    if tradable_ct > 0:
        bias_parts.append(f"{tradable_ct} tradable idea(s)")
    if watch_ct > 0:
        bias_parts.append(f"{watch_ct} on watch")
    weekly_bias = "; ".join(bias_parts) + "." if bias_parts else "No clear bias — await more data."

    # Top Risks
    risk_parts = []
    macro = context.get("macro_events_next_5d", [])
    if macro and macro[0] != "No high-impact events":
        risk_parts.append(f"Macro: {macro[0]}")
    gamma = context.get("dealer_gamma", {})
    if gamma.get("sign") == "negative" and gamma.get("magnitude") in ("high", "medium"):
        risk_parts.append("dealer gamma hostile")
    if suppress_ct > 0:
        risk_parts.append(f"{suppress_ct} setup(s) suppressed by environment")
    if regime_label in ("Stressed", "Risk-Off"):
        risk_parts.append(f"regime at {regime_label}")
    top_risks = "; ".join(risk_parts) + "." if risk_parts else "No elevated risks detected."

    return {
        "market_state": market_state,
        "weekly_bias": weekly_bias,
        "top_risks": top_risks,
    }


def _gather_desk_brief_context() -> dict:
    """Gather rich context for the Desk Brief from all available sources."""
    context = {}
    try:
        fp_data = api_flow_pressure()
        context["flow_pressure"] = {
            "composite_score": fp_data.get("flowPressure", {}).get("composite_score"),
            "composite_label": fp_data.get("flowPressure", {}).get("composite_label"),
        }
        regime = fp_data.get("regime", {})
        context["regime"] = {
            "label": regime.get("label"),
            "score": regime.get("score"),
            "components": regime.get("components", {}),
        }
        vol = fp_data.get("volState", {})
        context["vol_state"] = {
            "direction": vol.get("global_vol_direction") or vol.get("direction"),
            "us_iv_state": vol.get("us_iv_state"),
            "vol_lag_state": vol.get("vol_lag_state"),
            "structure_bias": vol.get("structure_bias"),
        }
    except Exception:
        pass

    try:
        seq_data = api_sequencer()
        seq = seq_data.get("sequence", {})
        context["sequencer_events_this_week"] = len(seq.get("events", []))
        context["matched_pattern"] = seq.get("matched_pattern", {}).get("label")
        context["pattern_confidence"] = seq.get("matched_pattern", {}).get("confidence")
    except Exception:
        pass

    # Gate summary from tradable ideas
    try:
        ideas_data = api_tradable_ideas()
        ideas_list = ideas_data.get("ideas", [])
        gate_counts = {"TRADABLE": 0, "WATCH": 0, "SUPPRESS": 0}
        for idea in ideas_list:
            g = idea.get("gate", {})
            status = g.get("status", "") if isinstance(g, dict) else ""
            if status in gate_counts:
                gate_counts[status] += 1
        context["gate_summary"] = gate_counts
        context["tradable_ideas_count"] = len(ideas_list)
    except Exception:
        pass

    # Macro events for next 5 sessions
    try:
        import datetime as _dt_brief
        bz = _get_benzinga_client_optional()
        if bz:
            from backend.macro_events import macro_events_by_date
            today = _dt_brief.date.today()
            end_date = today + _dt_brief.timedelta(days=7)
            macro_by_date, _, _ = macro_events_by_date(
                bz=bz, start=today, end=end_date, importance_min=3,
            )
            macro_summary = []
            for day_str, day_events in sorted(macro_by_date.items()):
                high = [e for e in day_events if int(e.get("importance", 0) or 0) >= 4]
                if high:
                    macro_summary.append(f"{day_str}: {len(high)} high-impact event(s)")
            context["macro_events_next_5d"] = macro_summary[:5] if macro_summary else ["No high-impact events"]
    except Exception:
        pass

    # Dealer gamma context (SPX)
    try:
        cl = _get_client_optional()
        if cl:
            from backend.dealer_gamma_context import compute_dealer_gamma_context
            rows = cl.live_strikes(ticker="SPXW", fields="strike,gamma,callOpenInterest,putOpenInterest,spotPrice").rows or []
            if rows:
                gamma = compute_dealer_gamma_context(rows)
                context["dealer_gamma"] = {
                    "sign": gamma.get("netGammaSign"),
                    "magnitude": gamma.get("magnitudeBucket"),
                }
    except Exception:
        pass

    return context


@app.get("/api/command-center/desk-brief")
def api_desk_brief():
    """Desk Brief: LLM-generated narrative or deterministic synthesis."""
    with _desk_brief_cache_lock:
        cached = _desk_brief_cache.get("latest")
    if cached is not None:
        return cached

    context = _gather_desk_brief_context()

    flags = get_flags()
    if flags.ENABLE_LLM_NARRATIVE:
        # Try LLM first, fall back to deterministic
        brief = generate_desk_brief(context)
        # Check if LLM returned the generic fallback (meaning it failed)
        is_fallback = brief.get("market_state", "").startswith("Market data is being processed")
        if is_fallback:
            brief = _build_deterministic_desk_brief(context)
        payload = {"enabled": True, "brief": brief}
    else:
        # LLM disabled — use deterministic synthesis from actual data
        brief = _build_deterministic_desk_brief(context)
        payload = {"enabled": False, "brief": brief}

    with _desk_brief_cache_lock:
        _desk_brief_cache["latest"] = payload
    return payload


def _build_red_dog_why_now(s: dict) -> str:
    """Build a short 'Why Now' from Red Dog setup fields (max ~3 items)."""
    parts = []
    direction = s.get("direction", "")
    quality = s.get("quality", {})
    indicators = s.get("indicators", {})
    trend = s.get("trendAlignment", {})

    grade = quality.get("grade", "")
    if grade:
        parts.append(grade)

    rsi = indicators.get("rsi")
    if rsi is not None:
        if direction == "bullish" and rsi < 35:
            parts.append(f"RSI {rsi:.0f}")
        elif direction == "bearish" and rsi > 65:
            parts.append(f"RSI {rsi:.0f}")

    sma_dev = indicators.get("sma20DeviationPct")
    if sma_dev is not None and abs(sma_dev) > 3:
        parts.append(f"{abs(sma_dev):.1f}% from 20MA")

    vol_ratio = indicators.get("volumeRatio")
    if vol_ratio is not None and vol_ratio > 1.3:
        parts.append(f"Vol {vol_ratio:.1f}x")

    alignment = trend.get("alignment", "")
    if alignment == "aligned":
        parts.append("w/ trend")
    elif not s.get("gammaAligned", True):
        parts.append("gamma hostile")

    if not parts:
        return "Reversal signal"
    return ", ".join(parts[:4])


def _build_red_dog_what_breaks(s: dict) -> str:
    """Build a short 'What Breaks It' from Red Dog levels."""
    levels = s.get("levels", {})
    direction = s.get("direction", "")
    stop = levels.get("stopLoss")
    if stop is not None:
        return f"Stop ${stop:.2f}"
    return "Below stop level"


def _build_ichimoku_why_now(s: dict) -> str:
    """Build a short 'Why Now' from Ichimoku setup fields (max ~3 items)."""
    parts = []
    quality = s.get("quality", {})
    ichimoku = s.get("ichimoku", {})
    indicators = s.get("indicators", {})
    freshness = s.get("freshness", {})

    grade = quality.get("grade", "")
    if grade:
        parts.append(grade)

    cloud_bias = ichimoku.get("cloudBias", "")
    kijun_slope = indicators.get("kijunSlope", "")
    if cloud_bias and kijun_slope and kijun_slope != "flat":
        parts.append(f"cloud {cloud_bias}, Kijun {kijun_slope}")
    elif cloud_bias:
        parts.append(f"cloud {cloud_bias}")

    bars_since = freshness.get("barsSinceReclaim")
    if bars_since is not None and bars_since <= 3:
        parts.append(f"fresh ({bars_since}d)")

    vol_ratio = indicators.get("volumeRatio")
    if vol_ratio is not None and vol_ratio > 1.25:
        parts.append(f"Vol {vol_ratio:.1f}x")

    chikou_tangled = indicators.get("chikouTangled")
    if chikou_tangled is False:
        parts.append("Chikou clear")

    if not parts:
        return "Continuation signal"
    return ", ".join(parts[:3])


def _build_ichimoku_what_breaks(s: dict) -> str:
    """Build a short 'What Breaks It' from Ichimoku levels."""
    ichimoku = s.get("ichimoku", {})
    levels = s.get("levels", {})
    direction = s.get("direction", "")
    kijun = ichimoku.get("kijun")
    stop = levels.get("stopLoss")

    if kijun is not None:
        verb = "below" if direction == "bullish" else "above"
        return f"Kijun ${kijun:.2f}"
    if stop is not None:
        return f"Stop ${stop:.2f}"
    return "Below Kijun"


@app.get("/api/command-center/tradable-ideas")
def api_tradable_ideas():
    """Aggregated tradable ideas across all engines with gate status."""
    ideas = []
    flags = get_flags()

    # Auto-run Engine 3 and 4 scans if caches are empty
    _ensure_engine3_cache(flags)
    _ensure_engine4_cache(flags)

    # Collect from Engine 3 (Red Dog)
    try:
        if flags.ENABLE_ENGINE3_RED_DOG:
            client = _get_client_optional()
            if client:
                with _engine3_cache_lock:
                    cached = None
                    for k, v in list(_engine3_cache.items()):
                        cached = v
                        break
                if cached and isinstance(cached, dict):
                    setups = cached.get("watchlist") or cached.get("aPlus") or []
                    if isinstance(setups, list):
                        for s in setups[:10]:
                            if isinstance(s, dict):
                                quality = s.get("quality", {})
                                score = quality.get("score") if isinstance(quality, dict) else None
                                if score is None:
                                    score = s.get("score", 0)
                                ideas.append({
                                    "ticker": s.get("ticker", ""),
                                    "engine": "Engine 3 Red Dog",
                                    "setupType": "Mean Reversion",
                                    "direction": s.get("direction", ""),
                                    "score": score,
                                    "gate": s.get("gate", {"status": "TRADABLE", "reasons": []}),
                                    "whyNow": _build_red_dog_why_now(s),
                                    "whatBreaks": _build_red_dog_what_breaks(s),
                                })
    except Exception:
        pass

    # Collect from Engine 4 (Ichimoku)
    try:
        if flags.ENABLE_ENGINE4_ICHIMOKU:
            with _engine4_cache_lock:
                cached = None
                for k, v in list(_engine4_cache.items()):
                    cached = v
                    break
            if cached and isinstance(cached, dict):
                setups = (cached.get("actionable") or []) + (cached.get("structure") or [])
                if isinstance(setups, list):
                    for s in setups[:10]:
                        if isinstance(s, dict):
                            quality = s.get("quality", {})
                            score = quality.get("score") if isinstance(quality, dict) else None
                            if score is None:
                                score = s.get("score", 0)
                            ideas.append({
                                "ticker": s.get("ticker", ""),
                                "engine": "Engine 4 Ichimoku",
                                "setupType": "Trend Continuation",
                                "direction": s.get("direction", ""),
                                "score": score,
                                "gate": s.get("gate", {"status": "TRADABLE", "reasons": []}),
                                "whyNow": _build_ichimoku_why_now(s),
                                "whatBreaks": _build_ichimoku_what_breaks(s),
                            })
    except Exception:
        pass

    # Sort by score descending
    ideas.sort(key=lambda x: x.get("score", 0), reverse=True)

    # Count by engine for the frontend
    rd_count = sum(1 for i in ideas if "Red Dog" in i.get("engine", ""))
    ich_count = sum(1 for i in ideas if "Ichimoku" in i.get("engine", ""))

    return {
        "ideas": ideas,
        "count": len(ideas),
        "engines": {
            "red_dog": {"count": rd_count, "enabled": flags.ENABLE_ENGINE3_RED_DOG},
            "ichimoku": {"count": ich_count, "enabled": flags.ENABLE_ENGINE4_ICHIMOKU},
        },
    }


@app.get("/api/command-center/alerts")
def api_alerts():
    """Alerts feed: state flips detected this week from the Sequencer.

    Returns recent SequencerEvents for the current ISO week, formatted
    as human-readable alert cards for the Command Center Alerts panel.
    """
    wid = current_week_id()

    events = []
    try:
        store = get_store_optional()
        if store:
            redis_events = store.get_json(f"sequencer:week:{wid}")
            if isinstance(redis_events, list):
                events = redis_events
    except Exception:
        pass

    if not events:
        with _sequencer_lock:
            events = _sequencer_events.get(wid, [])

    _alert_type_labels = {
        "REGIME_FLIP": "Regime Flip",
        "FLOW_PRESSURE_FLIP": "Flow Pressure Shift",
        "DEALER_GAMMA_SHIFT": "Dealer Gamma Shift",
        "VOL_LEADLAG_FLIP": "Vol Lead-Lag Change",
        "EARNINGS_DISPERSION_SPIKE": "Earnings Dispersion Spike",
        "RED_DOG_BREADTH_CHANGE": "Red Dog Breadth",
        "ICHIMOKU_BREADTH_CHANGE": "Ichimoku Breadth",
    }

    alerts = []
    for ev in events:
        if not isinstance(ev, dict):
            continue
        et = ev.get("event_type", "")
        alerts.append({
            "id": ev.get("id", ""),
            "timestamp": ev.get("timestamp", ""),
            "date": ev.get("date", ""),
            "type": _alert_type_labels.get(et, et.replace("_", " ").title()),
            "event_type": et,
            "from_state": ev.get("from_state", ""),
            "to_state": ev.get("to_state", ""),
            "summary": ev.get("summary", ""),
            "source_engine": ev.get("source_engine", ""),
        })

    # Most recent first
    alerts.sort(key=lambda a: a.get("timestamp", ""), reverse=True)

    return {"alerts": alerts, "count": len(alerts), "weekId": wid}


# ---------------------------------------------------------------------------
# Raven-Tech 2.0 – Research Lab (LLM Feature Discovery)
# ---------------------------------------------------------------------------


@app.get("/api/research-lab/features")
def api_research_features():
    """Get the current feature discovery queue."""
    store = get_store_optional()
    if store is None:
        return {"features": [], "count": 0}

    raw = store.get_json("research:feature_queue")
    features = raw if isinstance(raw, list) else []
    return {"features": features, "count": len(features)}


@app.post("/api/research-lab/suggest")
def api_research_suggest():
    """Trigger LLM feature discovery."""
    flags = get_flags()
    if not flags.ENABLE_LLM_DISCOVERY:
        raise HTTPException(status_code=503, detail="LLM feature discovery is disabled.")

    # Build context of existing features
    context = {
        "existing_features": [
            "flow_pressure (0-100, 5 sub-components)",
            "regime (Risk-On/Transitional/Risk-Off/Stressed, 4-factor)",
            "vol_lead_lag (global_vol_score, us_iv_state, vol_lag_state)",
            "dealer_gamma (netGex, magnitude, sign)",
            "earnings_breach_rate, implied_move, realized_move",
        ],
        "data_sources": ["ORATS (IV, skew, greeks)", "EODHD (global bars)", "Benzinga (macro events)"],
    }

    features = suggest_features(context)

    # Store in Redis queue
    store = get_store_optional()
    if store and features:
        existing = store.get_json("research:feature_queue") or []
        if not isinstance(existing, list):
            existing = []
        existing.extend(features)
        store.set_json("research:feature_queue", existing, ttl_s=30 * 86400)

    return {"suggested": features, "count": len(features)}


# ============================================================================
# Front Layer – Market Intelligence
# ============================================================================


_dms_cache: TTLCache = TTLCache(maxsize=4, ttl=300)  # 5-min in-memory cache
_morning_brief_cache: TTLCache = TTLCache(maxsize=2, ttl=3600)
_weekly_roadmap_cache: TTLCache = TTLCache(maxsize=2, ttl=3600)


@app.get("/market-intelligence")
def market_intelligence_page():
    """Market Intelligence: Front Layer landing page."""
    mi_path = STATIC_DIR / "market-intelligence.html"
    if not mi_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/market-intelligence.html")
    return FileResponse(str(mi_path))


@app.get("/api/front-layer/daily-market-state")
def api_front_layer_dms():
    """Return today's DailyMarketState (build or load cached)."""
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    today_str = dt.date.today().isoformat()

    # Check in-memory cache
    cached = _dms_cache.get(f"dms:{today_str}")
    if cached is not None:
        return cached

    # Try Redis
    store = get_store_optional()
    dms = load_dms(today_str, store) if store else None

    if dms is not None:
        result = dms.to_dict()
        _dms_cache[f"dms:{today_str}"] = result
        return result

    # Build fresh DMS from live engine outputs
    dms_dict = _build_live_dms(today_str, store)
    _dms_cache[f"dms:{today_str}"] = dms_dict
    return dms_dict


@app.post("/api/front-layer/refresh")
def api_front_layer_refresh():
    """Force-refresh: pull live data from all engines, rebuild DMS, bust caches.

    This is a manual desk trigger that:
    - Bypasses all in-memory and Redis caches
    - Fetches the freshest data from every engine and data source
    - Rebuilds the DailyMarketState with a new timestamp
    - Persists the updated snapshot (additive to rolling history)
    - Re-generates the Morning Brief with fresh context
    - Does NOT interfere with cron schedules or retention policy

    Use during the trading day after major events: commodity shocks,
    crypto sell-offs, surprise news releases, regime flips, etc.
    """
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    now = dt.datetime.now(dt.timezone.utc)
    today_str = dt.date.today().isoformat()
    store = get_store_optional()

    # ── 1. Bust all in-memory caches ────────────────────────────────
    _dms_cache.clear()
    _morning_brief_cache.clear()
    _weekly_roadmap_cache.clear()

    # ── 2. Build fresh DMS (all live data, no cache reads) ──────────
    dms_dict = _build_live_dms(today_str, store)
    # Tag the refresh so the desk knows this was a manual pull
    dms_dict["_refresh"] = {
        "triggered_at": now.isoformat().replace("+00:00", "Z"),
        "source": "manual_desk_refresh",
    }

    # ── 3. Persist (overwrites today's snapshot; rolling history intact)
    if store:
        dms_obj = DailyMarketState.from_dict(dms_dict)
        persist_dms(dms_obj, store, ttl_s=flags.FRONT_LAYER_DMS_TTL_S)

    # ── 4. Re-cache in memory so subsequent GET reads are fresh ─────
    _dms_cache[f"dms:{today_str}"] = dms_dict

    # ── 5. Re-generate Morning Brief with the fresh DMS ─────────────
    brief = None
    brief_source = "disabled"
    brief_error = None
    if flags.ENABLE_FRONT_LAYER_LLM:
        try:
            history = load_dms_history(store, n=flags.FRONT_LAYER_DMS_HISTORY_DAYS) if store else []
            history_dicts = [h.to_dict() for h in history]
            brief = generate_morning_brief(dms_dict, history_dicts)
            brief_source = brief.get("_source", "unknown") if brief else "error"
            _morning_brief_cache[f"brief:{today_str}"] = brief
            if store:
                store.set_json(f"front_layer:brief:{today_str}", brief, ttl_s=7 * 86400)
        except Exception as e:
            brief_error = str(e)
            LOG.warning("Refresh: Morning Brief re-generation failed: %s", e)

    # ── 6. LLM diagnostics ─────────────────────────────────────────
    llm_diag: Dict[str, Any] = {
        "enabled": flags.ENABLE_FRONT_LAYER_LLM,
        "brief_source": brief_source,
    }
    if brief_error:
        llm_diag["brief_error"] = brief_error
    # Surface the fallback reason from the LLM pipeline itself
    if brief and brief.get("_fallback_reason"):
        llm_diag["fallback_reason"] = brief["_fallback_reason"]
    # Check if OpenAI key is present (don't leak the key itself)
    openai_key = (os.getenv("OPENAI_API_KEY") or "").strip()
    llm_diag["openai_key_set"] = bool(openai_key)
    llm_diag["openai_key_len"] = len(openai_key) if openai_key else 0

    return {
        "status": "ok",
        "refreshed_at": now.isoformat().replace("+00:00", "Z"),
        "date": today_str,
        "regime": dms_dict.get("regime", {}),
        "flow_pressure": dms_dict.get("flow_pressure", {}),
        "cross_asset_score": dms_dict.get("cross_asset_stress", {}).get("composite_score"),
        "cross_asset_readings": len(dms_dict.get("cross_asset_stress", {}).get("readings", [])),
        "asymmetry_count": len(dms_dict.get("asymmetry_signals", [])),
        "theme_count": len(dms_dict.get("news_themes", [])),
        "brief_regenerated": brief_source == "llm",
        "llm": llm_diag,
    }


def _build_live_dms(today_str: str, store) -> dict:
    """Build a DailyMarketState from live engine data.

    Reads from existing engines without modifying their logic.
    """
    flags = get_flags()

    # --- Engine 5: regime + vol ---
    regime_data = {}
    vol_direction = ""
    iv_stress = 50.0

    try:
        from backend.engine5_pipeline import run_pipeline
        from backend.engine5_snapshot import load_best_snapshot
        snapshot = load_best_snapshot(store) if store else None
        if snapshot:
            regime_data = snapshot.get("regime", {})
            vol_ll = snapshot.get("vol_lead_lag", {})
            vol_direction = str(vol_ll.get("vol_lag_state", ""))
            iv_stress = float(regime_data.get("components", {}).get("iv_stress", 50.0))
    except Exception as e:
        LOG.warning("Front Layer: Engine 5 data unavailable: %s", e)

    # --- Flow Pressure ---
    fp_snapshot = {}
    try:
        fp_cache_key = "command_center:flow_pressure"
        fp_cached = _dms_cache.get(fp_cache_key)
        if fp_cached:
            fp_snapshot = fp_cached
        elif store:
            fp_data = store.get_json("flow_pressure:latest_snapshot")
            if fp_data:
                fp_snapshot = fp_data
    except Exception as e:
        LOG.warning("Front Layer: Flow Pressure data unavailable: %s", e)

    # --- Sequencer ---
    seq_summary = {}
    try:
        from backend.sequencer import current_week_id, build_weekly_sequence
        wk = current_week_id()
        events_raw = []
        if store:
            events_raw = store.get_json(f"sequencer:week:{wk}") or []
        seq = build_weekly_sequence(events_raw, week_id=wk)
        seq_summary = seq.to_dict()
    except Exception as e:
        LOG.warning("Front Layer: Sequencer data unavailable: %s", e)

    # --- News risk ---
    event_count = 0
    high_sev = 0
    upcoming: List[str] = []
    try:
        cal = build_calendar_payload(mode="week")
        events = cal.get("events", [])
        event_count = len(events)
        high_sev = sum(1 for ev in events if str(ev.get("importance", "")).lower() in ("high", "critical"))
        upcoming = [str(ev.get("title", "")) for ev in events[:5] if ev.get("title")]
    except Exception as e:
        LOG.warning("Front Layer: Calendar data unavailable: %s", e)

    # --- News Themes ---
    themes_list: List[dict] = []
    try:
        headlines: List[str] = []
        # Try EODHD
        try:
            from backend.eodhd_client import EodhdClient
            eodhd = EodhdClient.from_env()
            resp = eodhd.get_news(topic="market", limit=50)
            headlines.extend(extract_headlines_from_eodhd(resp.rows))
        except Exception:
            pass
        # Try Benzinga
        try:
            benz = BenzingaClient.from_env()
            resp = benz.news(page_size=50)
            headlines.extend(extract_headlines_from_benzinga(resp.rows))
        except Exception:
            pass

        if headlines:
            prior_themes = load_theme_history(store, n_days=flags.FRONT_LAYER_THEME_LOOKBACK_DAYS) if store else []
            theme_snap = score_themes(headlines=headlines, prior_snapshots=prior_themes, date_str=today_str)
            themes_list = theme_snap.themes
            if store:
                persist_theme_snapshot(theme_snap, store)
    except Exception as e:
        LOG.warning("Front Layer: News theme scoring failed: %s", e)

    # --- Cross-Asset Stress ---
    cross_asset_snap: Optional[dict] = None
    try:
        from backend.eodhd_client import EodhdClient as _EodhdCls
        _eodhd = _EodhdCls.from_env()
        # Fetch S&P 500 for equity_return_1d
        spx_return = 0.0
        try:
            spx_resp = _eodhd.get_eod("GSPC.INDX", period="d")
            spx_bars = sorted(spx_resp.rows, key=lambda b: str(b.get("date", "")))
            if len(spx_bars) >= 2:
                cur_c = float(spx_bars[-1].get("adjusted_close") or spx_bars[-1].get("close", 0))
                prv_c = float(spx_bars[-2].get("adjusted_close") or spx_bars[-2].get("close", 0))
                if prv_c:
                    spx_return = round((cur_c - prv_c) / abs(prv_c) * 100, 4)
        except Exception:
            pass

        readings: List[AssetStressReading] = []
        for key, meta in CROSS_ASSET_UNIVERSE.items():
            try:
                resp = _eodhd.get_eod(meta["symbol"], period="d")
                bars = sorted(resp.rows, key=lambda b: str(b.get("date", "")))
                if len(bars) >= 2:
                    cur_c = float(bars[-1].get("adjusted_close") or bars[-1].get("close", 0))
                    prv_c = float(bars[-2].get("adjusted_close") or bars[-2].get("close", 0))
                    history = [float(b.get("adjusted_close") or b.get("close", 0)) for b in bars[-30:]]
                    r = compute_asset_stress(
                        symbol_key=key,
                        current_close=cur_c,
                        prior_close=prv_c,
                        equity_return_1d=spx_return,
                        history_closes=history,
                    )
                    readings.append(r)
            except Exception:
                pass

        if readings:
            now_ts = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
            cross_asset_snap = build_cross_asset_snapshot(
                readings=readings,
                timestamp=now_ts,
            ).to_dict()
            LOG.info("Front Layer: Cross-asset stress: %d readings", len(readings))
    except Exception as e:
        LOG.warning("Front Layer: Cross-asset stress unavailable: %s", e)

    # --- Build DMS ---
    dms = build_daily_market_state(
        date_str=today_str,
        regime=regime_data,
        flow_pressure_snapshot=fp_snapshot,
        vol_direction=vol_direction,
        iv_stress=iv_stress,
        event_count_5d=event_count,
        high_severity_count=high_sev,
        upcoming_events=upcoming,
        cross_asset_stress=cross_asset_snap,
        news_themes=themes_list,
        sequencer_summary=seq_summary,
    )

    # Detect asymmetries
    dms_dict = dms.to_dict()
    history = load_dms_history(store, n=flags.FRONT_LAYER_DMS_HISTORY_DAYS) if store else []
    history_dicts = [h.to_dict() for h in history]
    asymmetries = detect_asymmetries(dms_dict, history_dicts)
    dms_dict["asymmetry_signals"] = asymmetries

    # Persist
    if store:
        dms_updated = DailyMarketState.from_dict(dms_dict)
        persist_dms(dms_updated, store, ttl_s=flags.FRONT_LAYER_DMS_TTL_S)

    return dms_dict


@app.get("/api/front-layer/morning-brief")
def api_front_layer_morning_brief():
    """Return today's Morning Brief (LLM-generated)."""
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    today_str = dt.date.today().isoformat()

    # Check cache
    cached = _morning_brief_cache.get(f"brief:{today_str}")
    if cached is not None:
        return cached

    # Get DMS
    store = get_store_optional()
    dms = load_dms(today_str, store) if store else None
    if dms is None:
        # Try building live
        dms_dict = _build_live_dms(today_str, store)
    else:
        dms_dict = dms.to_dict()

    # Get history
    history = load_dms_history(store, n=flags.FRONT_LAYER_DMS_HISTORY_DAYS) if store else []
    history_dicts = [h.to_dict() for h in history]

    if flags.ENABLE_FRONT_LAYER_LLM:
        brief = generate_morning_brief(dms_dict, history_dicts)
    else:
        brief = {
            "market_posture": "LLM generation disabled. Review DailyMarketState directly.",
            "changes_vs_yesterday": "Enable ENABLE_FRONT_LAYER_LLM for narrative generation.",
            "active_themes": "See Active Themes panel.",
            "cross_asset_signals": "See Cross-Asset Stress panel.",
            "engine_alignment": "See Engine Gates in DailyMarketState.",
            "watch_list": "None",
            "stand_down": "Review regime state manually.",
            "_source": "disabled",
            "_generated_at": dt.datetime.utcnow().isoformat() + "Z",
        }

    _morning_brief_cache[f"brief:{today_str}"] = brief
    return brief


@app.get("/api/front-layer/weekly-roadmap")
def api_front_layer_weekly_roadmap():
    """Return the Weekly Roadmap (LLM-generated, Sunday night)."""
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    today_str = dt.date.today().isoformat()

    cached = _weekly_roadmap_cache.get(f"roadmap:{today_str}")
    if cached is not None:
        return cached

    store = get_store_optional()

    # Try loading cached roadmap from Redis
    if store:
        roadmap_data = store.get_json(f"front_layer:roadmap:{today_str}")
        if roadmap_data:
            _weekly_roadmap_cache[f"roadmap:{today_str}"] = roadmap_data
            return roadmap_data

    dms = load_dms(today_str, store) if store else None
    if dms is None:
        dms_dict = _build_live_dms(today_str, store)
    else:
        dms_dict = dms.to_dict()

    history = load_dms_history(store, n=flags.FRONT_LAYER_DMS_HISTORY_DAYS) if store else []
    history_dicts = [h.to_dict() for h in history]

    if flags.ENABLE_FRONT_LAYER_LLM:
        roadmap = generate_weekly_roadmap(dms_dict, history_dicts)
    else:
        roadmap = {
            "regime_flow_summary": "LLM generation disabled.",
            "expected_pattern": "Check sequencer panel.",
            "high_risk_days": [],
            "engine_behaviors": "See Engine Gates.",
            "earnings_focus": [],
            "asymmetry_radar": "No asymmetries detected.",
            "break_the_plan": "Review regime transition triggers.",
            "_source": "disabled",
            "_generated_at": dt.datetime.utcnow().isoformat() + "Z",
        }

    # Persist roadmap
    if store:
        store.set_json(f"front_layer:roadmap:{today_str}", roadmap, ttl_s=7 * 86400)

    _weekly_roadmap_cache[f"roadmap:{today_str}"] = roadmap
    return roadmap


@app.get("/api/front-layer/cross-asset-stress")
def api_front_layer_cross_asset():
    """Return live cross-asset stress snapshot."""
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    # Return latest DMS cross_asset_stress if available
    store = get_store_optional()
    today_str = dt.date.today().isoformat()
    dms = load_dms(today_str, store) if store else None
    if dms and dms.cross_asset_stress:
        return dms.cross_asset_stress

    return {"readings": [], "composite_score": 50.0, "composite_label": "Neutral", "timestamp": ""}


@app.get("/api/front-layer/news-themes")
def api_front_layer_news_themes():
    """Return active news theme readings."""
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    store = get_store_optional()
    today_str = dt.date.today().isoformat()

    if store:
        data = store.get_json(f"front_layer:themes:{today_str}")
        if data:
            return data

    # Return from DMS if available
    dms = load_dms(today_str, store) if store else None
    if dms and dms.news_themes:
        return {"date": today_str, "themes": dms.news_themes}

    return {"date": today_str, "themes": [], "dominant_theme": "", "total_headline_count": 0}


@app.get("/api/front-layer/asymmetry-radar")
def api_front_layer_asymmetry():
    """Return current asymmetry radar signals."""
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    store = get_store_optional()
    today_str = dt.date.today().isoformat()
    dms = load_dms(today_str, store) if store else None

    if dms and dms.asymmetry_signals:
        return {"signals": dms.asymmetry_signals, "count": len(dms.asymmetry_signals)}

    # Build live
    dms_dict = _build_live_dms(today_str, store)
    signals = dms_dict.get("asymmetry_signals", [])
    return {"signals": signals, "count": len(signals)}


@app.get("/api/front-layer/history")
def api_front_layer_history(days: int = Query(default=7, ge=1, le=120)):
    """Return rolling DMS history."""
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    store = get_store_optional()
    if not store:
        return {"snapshots": [], "count": 0}

    history = load_dms_history(store, n=days)
    return {
        "snapshots": [h.to_dict() for h in history],
        "count": len(history),
    }


@app.get("/api/front-layer/diff")
def api_front_layer_diff():
    """Return diff between today's and yesterday's DMS."""
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    store = get_store_optional()
    if not store:
        return {"has_changes": False, "changes": {}, "error": "No persistence layer"}

    today_str = dt.date.today().isoformat()
    yesterday_str = (dt.date.today() - dt.timedelta(days=1)).isoformat()

    today_dms = load_dms(today_str, store)
    yesterday_dms = load_dms(yesterday_str, store)

    if not today_dms or not yesterday_dms:
        return {"has_changes": False, "changes": {}, "error": "Insufficient history for diff"}

    return compute_dms_diff(today_dms, yesterday_dms)


@app.get("/api/front-layer/backfill-status")
def api_front_layer_backfill_status():
    """Report whether historical DMS data has been seeded.

    Returns snapshot count, date range, and per-day data quality flags.
    Useful for the UI to show whether the backfill script has been run.
    """
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER:
        raise HTTPException(status_code=503, detail="Front Layer is disabled.")

    store = get_store_optional()
    if not store:
        return {
            "seeded": False,
            "snapshot_count": 0,
            "date_range": None,
            "days": [],
        }

    index = store.get_json(DMS_INDEX_KEY) or []
    if not isinstance(index, list):
        index = []

    if not index:
        return {
            "seeded": False,
            "snapshot_count": 0,
            "date_range": None,
            "days": [],
        }

    # Gather per-day summaries (limited to last 14 for UI)
    days = []
    for date_str in index[:14]:
        dms = load_dms(date_str, store)
        if dms is None:
            continue
        d = dms.to_dict()
        has_cross_asset = bool(d.get("cross_asset_stress", {}).get("readings"))
        has_themes = bool(d.get("news_themes"))
        has_regime = d.get("regime", {}).get("state", "Transitional") != "Transitional" or bool(d.get("regime", {}).get("drivers"))
        days.append({
            "date": date_str,
            "has_cross_asset": has_cross_asset,
            "has_themes": has_themes,
            "has_regime": has_regime,
        })

    sorted_dates = sorted(index)
    return {
        "seeded": len(index) >= 3,
        "snapshot_count": len(index),
        "date_range": {
            "earliest": sorted_dates[0] if sorted_dates else None,
            "latest": sorted_dates[-1] if sorted_dates else None,
        },
        "days": days,
    }


# ---------------------------------------------------------------------------
# Engine 8: Post-Event Trade Extension
# ---------------------------------------------------------------------------


@app.get("/api/engine8/evaluate")
async def engine8_evaluate(
    ticker: str = Query(..., description="US equity ticker"),
    earnings_date: str = Query(..., description="Earnings date (YYYY-MM-DD)"),
    timing: str = Query(..., description="BMO or AMC"),
):
    """Engine 8 lifecycle evaluation.

    All three parameters are required — the desk provides them:
      - ticker: what to evaluate
      - earnings_date: when earnings are/were
      - timing: BMO (before market open) or AMC (after market close)

    Phase detection is deterministic from earnings_date vs today:
      Phase A (pre-earnings): earnings_date >= today (AMC same-day = pre)
      Phase B (post-earnings): earnings_date < today (BMO same-day = post)
    """
    import asyncio

    flags = get_flags()
    if not flags.ENABLE_ENGINE8_POST_EVENT:
        raise HTTPException(status_code=404, detail="Engine 8 is not enabled")

    ticker = ticker.strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker is required")

    timing = timing.strip().upper()
    if timing not in ("BMO", "AMC"):
        raise HTTPException(status_code=400, detail="timing must be BMO or AMC")

    try:
        ed = dt.date.fromisoformat(earnings_date[:10])
    except (ValueError, IndexError):
        raise HTTPException(status_code=400, detail="Invalid earnings_date format (YYYY-MM-DD)")

    orats = _get_client_optional()
    if orats is None:
        raise HTTPException(status_code=503, detail="ORATS client unavailable")

    store = get_store_optional()
    today = dt.date.today()

    try:
        from backend.price_service import get_price_service
        price_svc = get_price_service()
    except Exception:
        price_svc = None

    bz = _get_benzinga_client_optional()

    # -- Phase detection (deterministic) ---------------------------------------
    is_pre_earnings = ed > today
    if ed == today and timing == "AMC":
        is_pre_earnings = True

    # =========================================================================
    # PHASE A: Pre-Earnings — run Engine 1, persist, return IC analysis
    # =========================================================================
    if is_pre_earnings:
        try:
            from backend.engine8_e1_bridge import run_engine1_for_phase_a

            e1_result = await asyncio.get_event_loop().run_in_executor(
                None, lambda: run_engine1_for_phase_a(
                    ticker=ticker,
                    orats_client=orats,
                    store=store,
                    earnings_date=ed,
                    today=today,
                    benzinga_client=bz,
                    price_svc=price_svc,
                ),
            )

            summary = e1_result.get("summary", {})
            trade_builder = e1_result.get("tradeBuilder")
            go_no_go = e1_result.get("goNoGo", {})
            regime = e1_result.get("regime", {})
            current = e1_result.get("current", {})
            expected_move = e1_result.get("expectedMove", {})
            strike_targets = e1_result.get("strikeTargets")
            baseline = e1_result.get("baseline", {})
            playbook = e1_result.get("playbook")
            hold_risk = e1_result.get("earningsHoldRisk", {})

            return {
                "phase": "pre_earnings",
                "ticker": ticker,
                "earnings_date": ed.isoformat(),
                "timing": timing,
                "countdown_days": (ed - today).days,
                "stock_price": current.get("stockPrice"),
                "engine1": {
                    "summary": {
                        "breach_rate_pct": summary.get("breach_rate_pct"),
                        "events_used": summary.get("events_used"),
                        "events_found": summary.get("events_found"),
                        "upBreachRatePct": summary.get("upBreachRatePct"),
                        "downBreachRatePct": summary.get("downBreachRatePct"),
                        "avgUpOvershootPct": summary.get("avgUpOvershootPct"),
                        "avgDownOvershootPct": summary.get("avgDownOvershootPct"),
                        "avg_above_breach_pct": summary.get("avg_above_breach_pct"),
                        "tailBias": summary.get("tailBias"),
                        "avg_implied_all_pct": summary.get("avg_implied_all_pct"),
                    },
                    "baseline": {
                        "avg_ratio_realized_to_implied": baseline.get("avg_ratio_realized_to_implied"),
                    },
                    "current": {
                        "stockPrice": current.get("stockPrice"),
                        "asOfDate": current.get("asOfDate"),
                        "source": current.get("source"),
                        "impliedMovePct": current.get("impliedMovePct"),
                        "impErnMv": current.get("impErnMv"),
                        "delayedImpliedMovePct": current.get("delayedImpliedMovePct"),
                        "delayedUpdatedAt": current.get("delayedUpdatedAt"),
                        "delayedTradeDate": current.get("delayedTradeDate"),
                    },
                    "expectedMove": {
                        "expectedMovePct": (expected_move or {}).get("expectedMovePct"),
                        "expectedMoveDollars": (expected_move or {}).get("expectedMoveDollars"),
                        "expiry": (expected_move or {}).get("expiry"),
                        "source": (expected_move or {}).get("source"),
                    },
                    "strikeTargets": strike_targets,
                    "tradeBuilder": trade_builder,
                    "goNoGo": go_no_go,
                    "regime": {
                        "label": regime.get("label"),
                        "guidance": regime.get("guidance"),
                    },
                    "holdRisk": {
                        "breach_1_5x": (hold_risk.get("unconditional", {}).get("earnings_close", {}).get("1.5")),
                        "breach_2_0x": (hold_risk.get("unconditional", {}).get("earnings_close", {}).get("2.0")),
                    },
                },
                "playbook": playbook,
                "decision": None,
            }
        except Exception as e:
            LOG.exception("Engine 8 Phase A failed for %s", ticker)
            raise HTTPException(status_code=500, detail=f"Engine 8 Phase A error: {e}") from e

    # =========================================================================
    # PHASE B: Post-Earnings — load Engine 1, run extension pipeline
    # =========================================================================
    try:
        from backend.engine8_e1_bridge import load_engine1_for_phase_b, derive_trade_outcome_from_e1
        from backend.engine8_pipeline import evaluate_ticker

        engine1_trade = None
        e1_persisted = None
        if store is not None:
            e1_persisted = await asyncio.get_event_loop().run_in_executor(
                None, lambda: load_engine1_for_phase_b(
                    ticker=ticker, earnings_date=ed.isoformat(), store=store,
                ),
            )
            if e1_persisted:
                engine1_trade = e1_persisted

        # Fall back to in-memory breach cache
        if engine1_trade is None:
            try:
                key = _breach_cache_key(ticker, 20, 5, 1.0, flags.cache_fingerprint())
                with _breach_cache_lock:
                    cached_breach = _breach_cache.get(key)
                if cached_breach and isinstance(cached_breach, dict):
                    engine1_trade = cached_breach
            except Exception:
                pass

        result = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: evaluate_ticker(
                ticker=ticker,
                engine1_trade=engine1_trade,
                earnings_date=ed,
                earnings_timing=timing,
                orats_client=orats,
                price_svc=price_svc,
                store=store,
                flags=flags,
            ),
        )

        result["phase"] = "post_earnings"
        result["timing"] = timing

        # Attach Engine 1 summary for the IC outcome card
        if e1_persisted:
            tb = e1_persisted.get("tradeBuilder")
            current_price_val = None
            if price_svc:
                try:
                    bars = price_svc.fetch_daily_bars(ticker, today - dt.timedelta(days=5), today)
                    if bars:
                        bars.sort(key=lambda b: b.date, reverse=True)
                        current_price_val = bars[0].close
                except Exception:
                    pass

            trade_outcome = derive_trade_outcome_from_e1(e1_persisted, current_price_val, flags.ENGINE8_MAX_CONTROLLED_LOSS_PCT)
            result["engine1_summary"] = {
                "had_phase_a": True,
                "trade_outcome": trade_outcome,
                "tradeBuilder": tb,
                "breach_rate_pct": (e1_persisted.get("summary") or {}).get("breach_rate_pct"),
                "expected_move_pct": (e1_persisted.get("current") or {}).get("impliedMovePct"),
            }
        else:
            result["engine1_summary"] = {
                "had_phase_a": False,
                "trade_outcome": "unknown",
                "message": "No pre-earnings setup found. Run Engine 8 before earnings to set up the lifecycle.",
            }

        return result
    except Exception as e:
        LOG.exception("Engine 8 Phase B failed for %s", ticker)
        raise HTTPException(status_code=500, detail=f"Engine 8 error: {e}") from e


@app.get("/api/engine8/history")
async def engine8_history(
    ticker: str = Query(..., description="US equity ticker"),
    n: int = Query(40, ge=1, le=100, description="Number of historical events"),
):
    """Return historical pattern analysis for a ticker (debugging/transparency)."""
    import asyncio

    flags = get_flags()
    if not flags.ENABLE_ENGINE8_POST_EVENT:
        raise HTTPException(status_code=404, detail="Engine 8 is not enabled")

    ticker = ticker.strip().upper()
    if not ticker:
        raise HTTPException(status_code=400, detail="ticker is required")

    orats = _get_client_optional()
    if orats is None:
        raise HTTPException(status_code=503, detail="ORATS client unavailable")

    try:
        from backend.price_service import get_price_service
        price_svc = get_price_service()
    except Exception:
        price_svc = None

    try:
        from backend.engine8_pipeline import _build_all_event_rows
        from backend.config import FeatureFlags
        from dataclasses import replace as dc_replace

        effective_flags = dc_replace(flags, ENGINE8_LOOKBACK_EVENTS=n)

        loop = asyncio.get_event_loop()
        today = dt.date.today()

        event_rows = await loop.run_in_executor(
            None,
            lambda: _build_all_event_rows(
                ticker=ticker,
                current_earnings_date=today,
                orats_client=orats,
                price_svc=price_svc,
                flags=effective_flags,
            ),
        )
        return {
            "ticker": ticker,
            "event_count": len(event_rows),
            "events": event_rows,
        }
    except Exception as e:
        LOG.exception("Engine 8 history failed for %s", ticker)
        raise HTTPException(status_code=500, detail=f"Engine 8 history error: {e}") from e


# ---------------------------------------------------------------------------
# Engine 8 – LLM Desk Notes for Earnings Playbook
# ---------------------------------------------------------------------------

_E8_DESK_NOTES_SYSTEM = """You are a senior quant on an options-focused systematic desk.
A junior desk quant is reviewing an earnings playbook for an upcoming earnings event.
They need your guidance on how to interpret and trade the scenarios.

You will receive a JSON payload with:
- ticker, earnings_date, timing (BMO/AMC)
- breach_stats: historical breach rate, avg overshoot, realized/implied ratio
- expected_move: ORATS EM %, straddle EM, strike targets at 1.0x/1.5x/2.0x
- playbook: scenario matrix with magnitude buckets, continuation/reversion rates, actions
- thresholds: dollar price levels at EM multiples

Write a concise desk briefing in this exact JSON structure:

{
  "overall_thesis": "3-4 sentences: What this ticker's earnings history tells us. Is it a momentum name (gaps continue) or a mean-reverter (gaps fade)? What's the historical edge?",
  "iron_condor_view": "3-4 sentences: Given the breach rate and EM data, how should we think about selling an iron condor here? Wing placement relative to EM multiples. Is the premium worth the risk given the breach history?",
  "scenario_playbook": "4-6 sentences: Walk through the key scenarios. If it gaps up within EM — what do we do? If it gaps beyond 1.5x EM? If it gaps down? Reference the actual continuation rates and drift numbers.",
  "entry_timing": "2-3 sentences: When to put the trade on (days before earnings?), how to manage delta exposure into the event, and when to act post-announcement.",
  "risk_management": "2-3 sentences: Position sizing relative to the EM, stop-loss levels, max acceptable loss. How the breach rate informs our risk budget.",
  "what_breaks_it": "2-3 sentences: What scenario invalidates the playbook — regime change, unusual vol, earnings restatement, guidance surprise beyond historical norms.",
  "desk_takeaway": "2-3 sentences: The one key insight a junior quant should remember about trading this name around earnings. What makes this ticker different from the average stock."
}

Key data fields in each scenario:
- high_vol_pct: % of events where volume was >1.5x the 20-day average. High volume confirms information flow (continuation). Low volume = overreaction (fade candidate).
- hold_pct: % of events where the gap held intraday (didn't fade by close). HOLD events have the strongest PEAD (post-earnings drift).
- optimal_hold_days: the horizon (1d, 3d, or 5d) with the highest continuation rate — the suggested holding period.
- continuation_rate_3d: the 3-day continuation rate — often the sweet spot for PEAD capture.
- avg_rel_volume: average relative volume across events in this scenario.

Rules:
- Write as a senior quant talking to a junior: clear, direct, practical.
- Reference the ACTUAL numbers from the data (breach rate, EM %, continuation rates, volume, specific dollar levels).
- Be specific about this ticker — don't give generic earnings trading advice.
- If breach rate is high (>25%), emphasize the risk to short premium strategies.
- If high_vol_pct is high (>60%), note that volume confirms the gap is real (not overreaction).
- If continuation rates are strongly directional, highlight the PEAD opportunity and recommend the optimal_hold_days.
- If HOLD structure events dominate (hold_pct > 50%), call out that gap-and-hold is the strongest PEAD signal.
- Keep each field under 100 words.
- Output valid JSON only."""


@app.post("/api/engine8/desk-notes")
def engine8_desk_notes(body: dict):
    """Engine 8: Generate GPT-5.2 senior quant desk notes for the earnings playbook."""
    payload_data = body.get("payload")
    if not payload_data:
        raise HTTPException(status_code=400, detail="Missing 'payload' in request body")

    try:
        from backend.llm_client import _get_openai_client, _parse_desk_brief_json

        client = _get_openai_client()
        if client is None:
            raise HTTPException(status_code=503, detail="OpenAI client unavailable — set OPENAI_API_KEY")

        import json as _json
        payload_str = _json.dumps(payload_data, default=str)
        if len(payload_str) > 12000:
            payload_str = payload_str[:12000]

        resp = client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {"role": "system", "content": _E8_DESK_NOTES_SYSTEM},
                {"role": "user", "content": payload_str},
            ],
            temperature=0.3,
            max_completion_tokens=1800,
            timeout=45,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or ""
        parsed = _parse_desk_brief_json(content)
        if parsed is None:
            raise HTTPException(status_code=502, detail="LLM returned unparseable response")

        parsed["_source"] = "gpt-5.2"
        parsed["_ticker"] = payload_data.get("ticker", "")
        return parsed

    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Engine 8 desk-notes LLM failed")
        raise HTTPException(status_code=500, detail=f"LLM error: {e}") from e


# ---------------------------------------------------------------------------
# Engine 8 – Per-Row Scenario Playbook (GPT-5.2 Trade Ticket)
# ---------------------------------------------------------------------------

_E8_ROW_PLAYBOOK_SYSTEM = """You are a senior quant on an options-focused systematic desk writing a trade ticket for ONE specific earnings scenario.

Context: The desk runs short iron condors into earnings. After the event, the IC is closed or expires. The trader now needs to decide whether to deploy a directional follow-through trade based on the gap that occurred. Your job is to give them an actionable blueprint for THIS specific scenario.

You will receive a JSON payload with:
- scenario: a single row from the scenario matrix (magnitude bucket, direction, structure, continuation/reversion rates at 1d/3d/5d, drift, volume confirmation, hold %, optimal hold days, action, confidence, reason)
- matched_events: the actual historical earnings events that fell into this bucket (dates, actual moves, forward returns, volume)
- context.ticker, context.stock_price, context.em_pct
- context.breach_stats: historical breach rate, overshoot, realized/implied ratio
- context.thresholds: dollar price levels at 1.0x/1.5x/2.0x EM
- context.strike_targets: IC wing distances at EM multiples
- context.dealer_flow (when available): real-time dealer gamma positioning for both the ticker and SPX:
  - ticker_gamma: netGammaSign (positive=dealer long gamma, dampens moves; negative=dealer short gamma, amplifies moves), magnitudeBucket (low/medium/high), callPutImbalance, topGammaStrikes, putWallStrike, callWallStrike, tailIgnition (up/down risk scores 0-100 with labels)
  - spx_gamma: same structure for SPX — provides the macro gamma backdrop

Write a trade ticket in this exact JSON structure:

{
  "verdict": "CONTINUE or FADE or PASS",
  "conviction": "HIGH or MEDIUM or LOW",
  "one_liner": "One sentence: the core thesis for this scenario in plain desk language.",
  "entry_plan": {
    "trigger": "Exact condition that activates this trade. Reference dollar levels from thresholds.",
    "instrument": "Specific instrument recommendation: shares, debit spread with strikes, or skip. Be concrete.",
    "timing": "When to enter relative to the open — first 30 min, wait for structure confirmation, etc.",
    "size": "Position sizing guidance as % of book or risk units. Scale to conviction."
  },
  "exit_plan": {
    "profit_target": "Where to take profit — % of gap, dollar level, or % of max spread value.",
    "stop_loss": "Hard stop condition — price level or % retracement that invalidates the thesis.",
    "time_stop": "When to close if thesis hasn't played out. Reference optimal_hold_days.",
    "hold_period": "Recommended hold in days. Reference the horizon with highest edge."
  },
  "risk_notes": "Breach rate context, tail risk, what the realized/implied ratio tells us about this name's tendency to surprise. 2-3 sentences.",
  "historical_anchor": "Cite the matched events — how many, what happened, what the average drift was. Be specific with dates and numbers. 2-3 sentences.",
  "what_if_wrong": "If this scenario plays out opposite to the action — what does the trader do? Flip, stop out, or wait? 2-3 sentences.",
  "gamma_read": "Interpret the dealer_flow data: Is the ticker in positive or negative gamma? How does that affect post-earnings drift (negative gamma amplifies, positive dampens)? What does SPX gamma tell us about the macro backdrop? Reference put/call walls, tail ignition scores, and top gamma strikes. If no dealer_flow data, say 'No gamma context available.' 2-3 sentences.",
  "desk_voice": "The senior quant's parting words. Is this a bread-and-butter setup or an edge case? How does it compare to the average earnings trade? Be direct. 2-3 sentences."
}

Rules:
- Write as a senior quant on the desk, not a textbook. Be direct and practical.
- Reference the ACTUAL numbers: continuation rates, drift percentages, event counts, dollar levels, dates from matched events.
- If the action is PASS, the verdict must be PASS. Still fill out the blueprint explaining WHY there is no edge.
- If continuation_rate_5d >= 70%, lean into the CONTINUE thesis hard. Cite the rate and sample size.
- If high_vol_pct >= 60%, note that volume confirms the information content of the gap.
- If hold_pct >= 50%, highlight that gap-and-hold is the strongest PEAD signal.
- For FADE scenarios, look for low volume + high reversion rates. The instrument should be a reversal play.
- For CONTINUE scenarios, the instrument should capture drift in the gap direction.
- If dealer_flow.ticker_gamma is provided: negative gamma (dealer short gamma) amplifies moves — favors continuation plays. Positive gamma dampens moves — fade setups need stronger conviction. Reference the put/call wall strikes as support/resistance levels.
- If dealer_flow.spx_gamma is provided: negative SPX gamma means broader market moves are amplified — increases tail risk on ALL trades. Positive SPX gamma is stabilizing.
- If tailIgnition scores are HIGH (>60), warn about tail risk in that direction.
- Keep each field concise — under 75 words per field.
- Output valid JSON only."""


def _fetch_dealer_gamma_summary(orats, ticker: str) -> dict | None:
    """Best-effort fetch of dealer gamma context for a single ticker."""
    try:
        from backend.dealer_gamma_context import compute_dealer_gamma_context
        from backend.oi_clusters import compute_open_interest_clusters
        from backend.engine2_gamma_addons import compute_tail_ignition

        resp = orats.live_strikes(
            ticker=ticker,
            fields="strike,gamma,callOpenInterest,putOpenInterest,spotPrice,stockPrice,callVolume,putVolume",
        )
        rows = resp.rows if resp and getattr(resp, "rows", None) else []
        if not rows:
            return None

        dg = compute_dealer_gamma_context(rows, contract_multiplier=100, band_pct=0.05, top_n=5)
        spot = dg.get("spot")

        put_wall_strike = None
        call_wall_strike = None
        try:
            oi = compute_open_interest_clusters(rows, band_pct=0.10, top_n=5, cluster_steps=2)
            pw = oi.get("putWall")
            cw = oi.get("callWall")
            if pw:
                put_wall_strike = pw.get("peakStrike")
            if cw:
                call_wall_strike = cw.get("peakStrike")
        except Exception:
            pass

        ti = None
        try:
            ti = compute_tail_ignition(
                rows,
                spot=float(spot) if spot else None,
                put_wall_strike=put_wall_strike,
                call_wall_strike=call_wall_strike,
                contract_multiplier=100,
            )
        except Exception:
            pass

        summary = {
            "ticker": ticker,
            "spot": spot,
            "netGex": dg.get("netGex"),
            "netGammaSign": dg.get("netGammaSign"),
            "magnitudeBucket": dg.get("magnitudeBucket"),
            "callPutImbalance": dg.get("callPutImbalance"),
            "topGammaStrikes": dg.get("topGammaStrikes", [])[:3],
            "putWallStrike": put_wall_strike,
            "callWallStrike": call_wall_strike,
        }
        if ti and ti.get("enabled"):
            summary["tailIgnition"] = {
                "down": {"score": ti["down"]["score"], "label": ti["down"]["label"]},
                "up": {"score": ti["up"]["score"], "label": ti["up"]["label"]},
                "gammaFlipStrike": ti.get("gammaFlipStrike"),
            }
        return summary
    except Exception as exc:
        LOG.debug("Dealer gamma fetch failed for %s: %s", ticker, exc)
        return None


@app.post("/api/engine8/row-playbook")
def engine8_row_playbook(body: dict):
    """Engine 8: Generate GPT-5.2 trade ticket for a single scenario row."""
    scenario = body.get("scenario")
    context = body.get("context", {})
    if not scenario:
        raise HTTPException(status_code=400, detail="Missing 'scenario' in request body")

    try:
        from backend.llm_client import _get_openai_client, _parse_desk_brief_json

        llm_client = _get_openai_client()
        if llm_client is None:
            raise HTTPException(status_code=503, detail="OpenAI client unavailable — set OPENAI_API_KEY")

        ticker = (context.get("ticker") or "").upper()

        # Best-effort: enrich with dealer gamma for ticker + SPX
        gamma_context: dict = {}
        orats = _get_client_optional()
        if orats and ticker:
            ticker_gamma = _fetch_dealer_gamma_summary(orats, ticker)
            if ticker_gamma:
                gamma_context["ticker_gamma"] = ticker_gamma
            spx_gamma = _fetch_dealer_gamma_summary(orats, "SPX")
            if spx_gamma:
                gamma_context["spx_gamma"] = spx_gamma

        import json as _json
        payload = {
            "scenario": scenario,
            "matched_events": scenario.get("matched_events", []),
            "context": {
                "ticker": ticker,
                "stock_price": context.get("stock_price"),
                "em_pct": context.get("em_pct"),
                "breach_stats": context.get("breach_stats", {}),
                "thresholds": context.get("thresholds", {}),
                "strike_targets": context.get("strike_targets", {}),
                "dealer_flow": gamma_context if gamma_context else None,
            },
        }
        payload_str = _json.dumps(payload, default=str)
        if len(payload_str) > 16000:
            payload_str = payload_str[:16000]

        resp = llm_client.chat.completions.create(
            model="gpt-5.2",
            messages=[
                {"role": "system", "content": _E8_ROW_PLAYBOOK_SYSTEM},
                {"role": "user", "content": payload_str},
            ],
            temperature=0.3,
            max_completion_tokens=2000,
            timeout=60,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or ""
        parsed = _parse_desk_brief_json(content)
        if parsed is None:
            raise HTTPException(status_code=502, detail="LLM returned unparseable response")

        parsed["_source"] = "gpt-5.2"
        parsed["_scenario_key"] = scenario.get("key", "")
        return parsed

    except HTTPException:
        raise
    except Exception as e:
        LOG.exception("Engine 8 row-playbook LLM failed")
        raise HTTPException(status_code=500, detail=f"LLM error: {e}") from e


@app.post("/api/front-layer/asset-insight")
def api_front_layer_asset_insight(body: dict):
    """Generate a desk-level LLM insight for a single cross-asset stress reading.

    Request body: { "asset": { ...AssetStressReading dict... } }
    The DMS context is loaded automatically from today's snapshot.
    """
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER or not flags.ENABLE_FRONT_LAYER_LLM:
        raise HTTPException(status_code=503, detail="Front Layer LLM is disabled.")

    asset = body.get("asset")
    if not asset or not isinstance(asset, dict):
        raise HTTPException(status_code=400, detail="Missing 'asset' in request body.")

    # Load today's DMS for context
    today_str = dt.date.today().isoformat()
    store = get_store_optional()
    dms_dict = _dms_cache.get(f"dms:{today_str}")
    if not dms_dict and store:
        dms_obj = load_dms(today_str, store)
        if dms_obj:
            dms_dict = dms_obj.to_dict()
    dms_dict = dms_dict or {}

    insight = generate_asset_insight(asset, dms_dict)
    return insight


@app.post("/api/front-layer/card-insight")
def api_front_layer_card_insight(body: dict):
    """Generate a desk-level LLM insight for any MI card type.

    Request body: { "card_type": "composite|theme|regime|flow|asymmetry|diff", "card_data": { ... } }
    """
    flags = get_flags()
    if not flags.ENABLE_FRONT_LAYER or not flags.ENABLE_FRONT_LAYER_LLM:
        raise HTTPException(status_code=503, detail="Front Layer LLM is disabled.")

    card_type = body.get("card_type", "").strip()
    card_data = body.get("card_data")
    valid_types = {
        # Market Intelligence
        "composite", "theme", "regime", "flow", "asymmetry", "diff",
        # Engine 5 – Lead-Lag
        "e5_regime", "e5_vol", "e5_narrative", "e5_index_bias",
        "e5_sector_bias", "e5_trade_idea", "e5_triggers", "e5_component",
        # Engine 1 – Breach / Earnings Hold Risk
        "e1_decision", "e1_hold_risk", "e1_monte_carlo", "e1_regime",
        "e1_skew_wings", "e1_event_risk", "e1_gamma_context",
        "e1_quarter", "e1_strike_targets", "e1_dealer_gamma",
        # Engine 1 – Earnings Playbook Cards
        "e1_iv_check", "e1_premium_richness", "e1_liquidity_check", "e1_macro_overlay",
        # Engine 2 – SPX Iron Condor Scanner
        "e2_regime", "e2_macro", "e2_odds", "e2_dealer_gamma",
        "e2_gex", "e2_hedging_pressure", "e2_tail_ignition",
        "e2_vol_pressure", "e2_expected_move", "e2_technicals",
        # Engine 3 – Red Dog
        "rd_signal", "rd_gamma", "rd_trend", "rd_scan_summary", "rd_gate",
        # Engine 4 – Ichimoku
        "ik_signal", "ik_gamma", "ik_scan_summary", "ik_gate",
    }

    if card_type not in valid_types:
        raise HTTPException(status_code=400, detail=f"Invalid card_type. Must be one of: {', '.join(sorted(valid_types))}")
    if not card_data or not isinstance(card_data, dict):
        raise HTTPException(status_code=400, detail="Missing 'card_data' in request body.")

    # Load today's DMS for context; fall back to client-provided summary
    today_str = dt.date.today().isoformat()
    store = get_store_optional()
    dms_dict = _dms_cache.get(f"dms:{today_str}")
    if not dms_dict and store:
        dms_obj = load_dms(today_str, store)
        if dms_obj:
            dms_dict = dms_obj.to_dict()
    if not dms_dict:
        # Use client-provided context (e.g. Engine 5 data) as fallback
        dms_dict = body.get("dms_summary") or {}

    insight = generate_card_insight(card_type, card_data, dms_dict)
    return insight
