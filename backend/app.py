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

    # App subdomain -> Command Center (Raven-Tech 2.0 default)
    cc_path = STATIC_DIR / "command-center.html"
    if cc_path.exists():
        return FileResponse(str(cc_path))
    # Fallback to home dashboard if command-center.html not found
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
    cal_path = STATIC_DIR / "calendar.html"
    if not cal_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/calendar.html")
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

            # Derive rv10 from Engine 5 vol data if available
            if vol_data:
                # Engine 5 vol data may contain realized vol
                rv_raw = vol_data.get("us_rv10") or vol_data.get("rv10")
                if rv_raw is not None:
                    shared_rv10 = float(rv_raw)

            # ADV from SPY volume (liquid enough to always be high)
            shared_adv = 5_000_000_000.0  # SPY trades ~$30B/day
    except Exception as e:
        LOG.debug("Flow Pressure: ORATS cores fetch failed: %s", e)

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

    return {
        "weekId": wid,
        "tradingDays": week_trading_days(),
        "sequence": seq.to_dict(),
        "patterns": {k: {"label": v["label"], "description": v["description"]}
                     for k, v in PATTERN_TEMPLATES.items()},
    }


@app.get("/api/command-center/desk-brief")
def api_desk_brief():
    """Desk Brief: LLM-generated narrative compression."""
    flags = get_flags()
    if not flags.ENABLE_LLM_NARRATIVE:
        return {
            "enabled": False,
            "brief": {
                "market_state": "LLM narrative is disabled. Review metrics cards directly.",
                "weekly_bias": "Consult Flow Pressure and Regime cards for current bias.",
                "top_risks": "Check Macro Event Density panel for upcoming catalysts.",
            },
        }

    with _desk_brief_cache_lock:
        cached = _desk_brief_cache.get("latest")
    if cached is not None:
        return cached

    # Gather context for LLM
    context = {}
    try:
        fp_data = api_flow_pressure()
        context["flow_pressure"] = fp_data.get("flowPressure", {})
        context["regime"] = fp_data.get("regime", {})
        context["vol_state"] = fp_data.get("volState", {})
    except Exception:
        pass

    try:
        seq_data = api_sequencer()
        context["sequencer"] = seq_data.get("sequence", {})
    except Exception:
        pass

    brief = generate_desk_brief(context)
    payload = {"enabled": True, "brief": brief}

    with _desk_brief_cache_lock:
        _desk_brief_cache["latest"] = payload
    return payload


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
                    # Try to get cached scan
                    cached = None
                    for k, v in list(_engine3_cache.items()):
                        cached = v
                        break
                if cached and isinstance(cached, dict):
                    setups = cached.get("watchlist") or cached.get("aPlus", {}).get("setups", [])
                    if isinstance(setups, list):
                        for s in setups[:10]:
                            if isinstance(s, dict):
                                ideas.append({
                                    "ticker": s.get("ticker", ""),
                                    "engine": "Engine 3 Red Dog",
                                    "setupType": "Mean Reversion",
                                    "direction": s.get("direction", ""),
                                    "score": s.get("score", 0),
                                    "gate": s.get("gate", {"status": "TRADABLE", "reasons": []}),
                                    "whyNow": f"Red Dog signal score {s.get('score', 0)}",
                                    "whatBreaks": s.get("invalidation", "Price exceeds stop level"),
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
                # Engine 4 returns "actionable" and "structure" lists (not "watchlist"/"aPlus")
                setups = (cached.get("actionable") or []) + (cached.get("structure") or [])
                if isinstance(setups, list):
                    for s in setups[:10]:
                        if isinstance(s, dict):
                            ideas.append({
                                "ticker": s.get("ticker", ""),
                                "engine": "Engine 4 Ichimoku",
                                "setupType": "Trend Continuation",
                                "direction": s.get("direction", ""),
                                "score": s.get("score", 0),
                                "gate": s.get("gate", {"status": "TRADABLE", "reasons": []}),
                                "whyNow": f"Ichimoku signal score {s.get('score', 0)}",
                                "whatBreaks": s.get("invalidation", "Price breaks below Kijun"),
                            })
    except Exception:
        pass

    # Sort by score descending
    ideas.sort(key=lambda x: x.get("score", 0), reverse=True)

    return {"ideas": ideas, "count": len(ideas)}


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


