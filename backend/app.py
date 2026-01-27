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
from dataclasses import replace
from typing import Optional

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
from backend.calendar_api import Engine1UniversePolicy, build_calendar_payload
from backend.condor_rank import compute_condor_rank
from backend.calendar_snapshot import EARNINGS_SNAPSHOT_KEY, load_earnings_snapshot
from backend.fmp_snapshot import FMP_EARNINGS_SNAPSHOT_KEY, load_fmp_earnings_snapshot
from backend.macro_event_stats import compute_macro_event_stats
from backend.fmp_client import FmpClient, FmpError
from backend.engine3_screener import compute_engine3_scan, compute_single_ticker_scan


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
      - all other hosts (e.g. app.raven-tech.co) -> app calendar
    """
    if _is_root_domain_host(request.headers.get("host")):
        landing_path = STATIC_DIR / "landing.html"
        if not landing_path.exists():
            raise HTTPException(status_code=500, detail="Missing static/landing.html")
        return FileResponse(str(landing_path))

    cal_path = STATIC_DIR / "calendar.html"
    if not cal_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/calendar.html")
    return FileResponse(str(cal_path))


@app.get("/breach")
def breach_page():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/index.html")
    return FileResponse(str(index_path))


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


@app.get("/api/calendar")
def calendar(
    view: str = Query("month", description="month|week|day"),
    anchor: str = Query(None, description="YYYY-MM-DD (anchor date)"),
    tz: str = Query("America/New_York"),
    engine1Only: int = Query(0, ge=0, le=1),
    includeEvents: int = Query(1, ge=0, le=1),
    maxTickers: int = Query(12000, ge=200, le=50000),
):
    """
    Earnings calendar endpoint for the front page.

    Design goals:
    - One response for the visible range (month/week/day)
    - Macro events fetched once per range (Benzinga economics)
    - Engine-1 eligibility evaluated via ORATS /cores snapshot with long TTL cache
    """
    try:
        a = str(anchor or dt.date.today().isoformat())[:10]
        v = str(view or "month").strip().lower()
        if v not in ("month", "week", "day"):
            raise HTTPException(status_code=400, detail="Unsupported view. Allowed: month|week|day")
        e1 = bool(int(engine1Only))
        inc = bool(int(includeEvents))

        flags_fp = get_flags().cache_fingerprint()
        cache_ttl_s = int(float(os.getenv("CALENDAR_CACHE_TTL_S") or 0))
        key = ("calendar", v, a, str(tz or ""), int(e1), int(inc), int(maxTickers), flags_fp)
        if cache_ttl_s > 0:
            with _calendar_cache_lock:
                cached = _calendar_cache.get(key)
            if cached is not None:
                return cached

        fmp = _get_fmp_client_optional()
        if fmp is None:
            raise HTTPException(status_code=503, detail="FMP unavailable (missing FMP_API_KEY).")

        payload = build_calendar_payload(
            view=v,
            anchor=a,
            tz=tz,
            engine1_only=e1,
            include_events=inc,
            benzinga_client=_get_benzinga_client_optional(),
            fmp_client=fmp,
            max_tickers=int(maxTickers),
        )
        if cache_ttl_s > 0:
            with _calendar_cache_lock:
                _calendar_cache[key] = payload
        return payload
    except HTTPException:
        raise
    except OratsError as e:
        LOG.exception("ORATS failure (calendar)")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure (calendar)")
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

    Scans SP100 + Nasdaq100 for Red Dog Reversal setups with A+ quality scoring.

    Returns setups categorized by grade:
    - aPlus: Score >= 75 (high-quality setups)
    - standard: Score 50-74 (decent setups)
    - watchlist: Combined and sorted by score
    """
    _require_auth(request)

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
    _require_auth(request)

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


