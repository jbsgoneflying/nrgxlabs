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

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request, Form, UploadFile, File
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from cachetools import TTLCache
from pydantic import BaseModel, Field
import uuid
import pathlib

from backend.earnings_logic import BreachInputError, compute_breach_stats, compute_current_snapshot
from backend.config import get_flags
from backend.benzinga_client import BenzingaClient
from backend.orats_client import OratsClient, OratsError
from backend.spx_ic_engine import compute_engine2_spx_ic
from backend.redis_store import get_store_optional
from backend.askraven import UploadedImage, build_context_pack, call_openai


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


def _path_is_public(path: str) -> bool:
    p = str(path or "")
    if p.startswith("/static/"):
        return True
    if p in ("/api/health",):
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
    <title>RavenTech — Access</title>
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
        <img src="/static/RavenONLY.png" alt="RavenTech" />
        <div>
          <div class="loginTitle">RavenTech — Private Beta</div>
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

_breach_cache = TTLCache(maxsize=512, ttl=6 * 60 * 60)  # 6 hours
_breach_cache_lock = threading.Lock()

_spx_ic_cache = TTLCache(maxsize=128, ttl=30 * 60)  # 30 minutes (interactive)
_spx_ic_cache_lock = threading.Lock()

# AskRaven/session store (Redis recommended; optional for local dev)
_store = get_store_optional()


def _store_set_latest(engine: str, payload: dict) -> None:
    """
    Best-effort: persist the latest engine payload for AskRaven grounding.
    We intentionally ignore failures so the core app keeps working.
    """
    try:
        if _store is None:
            return
        if not isinstance(payload, dict):
            return
        _store.set_json(f"latest_report:{str(engine)}", payload)
    except Exception:
        return


ASKRAVEN_MAX_IMAGES = 4
ASKRAVEN_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10MB
ASKRAVEN_UPLOAD_DIR = pathlib.Path(os.getenv("ASKRAVEN_UPLOAD_DIR") or "/tmp/askraven_uploads")


def _is_webp(content: bytes) -> bool:
    # Minimal WEBP signature: RIFF....WEBP
    return bool(len(content) >= 12 and content[0:4] == b"RIFF" and content[8:12] == b"WEBP")


def _sniff_image_type(content: bytes) -> str | None:
    # Minimal magic checks: png/jpeg/gif/webp
    if len(content) >= 8 and content[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if len(content) >= 3 and content[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if len(content) >= 6 and content[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if _is_webp(content):
        return "image/webp"
    return None


def _ensure_upload_dir() -> None:
    try:
        ASKRAVEN_UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    except Exception:
        return


class ChatMessageRequest(BaseModel):
    engine: str = Field(..., description="engine1|engine2")
    message: str = Field(..., description="User question")
    image_ids: list[str] = Field(default_factory=list)

def _truncate(s: str, n: int) -> str:
    t = str(s or "").replace("\n", " ").strip()
    return (t[:n] + "…") if len(t) > n else t


def _benzinga_news_context(*, engine: str, report: dict, question: str) -> dict | None:
    """
    Best-effort, recent Benzinga news snapshot for AskRaven.
    Uses BenzingaClient's internal cache, and we also keep our own tiny TTL in Redis if available.
    """
    bz = _get_benzinga_client_optional()
    if bz is None:
        return None

    eng = str(engine or "").strip().lower()
    q = str(question or "").lower()
    wants_news = any(k in q for k in ("news", "headline", "headlines", "pre-market", "premarket", "gap", "catalyst", "catalysts"))
    # Always attach for Engine 2 (market context), or when explicitly requested.
    if not (wants_news or eng == "engine2"):
        return None

    tickers = None
    if eng == "engine1":
        t = str((report or {}).get("ticker") or "").strip().upper()
        tickers = t if t else None
    else:
        # SPX often isn't a standard equity ticker in news feeds; SPY reliably is.
        tickers = "SPY,SPX"

    now = dt.datetime.utcnow().date()
    date_to = now.isoformat()
    date_from = (now - dt.timedelta(days=2)).isoformat()
    cache_key = f"benzinga_news:{tickers}:{date_from}:{date_to}"

    if _store is not None:
        cached = _store.get_json(cache_key)
        if isinstance(cached, dict) and cached.get("items"):
            return cached

    try:
        resp = bz.news(
            tickers=tickers,
            date_from=date_from,
            date_to=date_to,
            page=0,
            page_size=50,
            sort="updated",
        )
        rows = resp.rows or []
    except Exception:
        rows = []

    items = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        title = _truncate(str(r.get("title") or r.get("headline") or ""), 220)
        if not title:
            continue
        items.append(
            {
                "title": title,
                "created": _truncate(str(r.get("created") or r.get("created_at") or r.get("published") or ""), 40),
                "updated": _truncate(str(r.get("updated") or r.get("updated_at") or ""), 40),
                "url": _truncate(str(r.get("url") or ""), 260),
                "source": _truncate(str(r.get("source") or ""), 40),
                "summary": _truncate(str(r.get("summary") or r.get("teaser") or ""), 280),
            }
        )
        if len(items) >= 12:
            break

    out = {
        "enabled": True,
        "provider": "benzinga",
        "tickers": tickers,
        "window": {"from": date_from, "to": date_to},
        "items": items,
        "notes": ["Recent Benzinga headlines snapshot (best-effort)."],
    }
    if _store is not None:
        _store.set_json(cache_key, out, ttl_s=15 * 60)  # 15 min
    return out


@app.post("/api/chat/upload")
async def chat_upload(files: list[UploadFile] = File(...)):
    if _store is None:
        raise HTTPException(status_code=500, detail="Redis not configured (REDIS_URL).")
    if not files:
        raise HTTPException(status_code=400, detail="No files provided.")
    if len(files) > ASKRAVEN_MAX_IMAGES:
        raise HTTPException(status_code=400, detail=f"Max {ASKRAVEN_MAX_IMAGES} images per upload.")

    _ensure_upload_dir()
    ids: list[str] = []
    for f in files:
        content = await f.read()
        if content is None:
            continue
        if len(content) > ASKRAVEN_MAX_IMAGE_BYTES:
            raise HTTPException(status_code=400, detail=f"Image too large (max {ASKRAVEN_MAX_IMAGE_BYTES} bytes).")
        sniffed = _sniff_image_type(content)
        if sniffed is None:
            raise HTTPException(status_code=400, detail="Unsupported image type. Use png/jpg/gif/webp.")
        ext = "png" if sniffed == "image/png" else "jpg" if sniffed == "image/jpeg" else "gif" if sniffed == "image/gif" else "webp"
        image_id = uuid.uuid4().hex
        path = ASKRAVEN_UPLOAD_DIR / f"{image_id}.{ext}"
        try:
            path.write_bytes(content)
        except Exception:
            raise HTTPException(status_code=500, detail="Failed to store upload.") from None
        meta = {"id": image_id, "path": str(path), "content_type": sniffed, "bytes": int(len(content))}
        _store.set_json(f"image_meta:{image_id}", meta)
        ids.append(image_id)

    return {"ok": True, "image_ids": ids, "max_images": ASKRAVEN_MAX_IMAGES, "max_bytes_each": ASKRAVEN_MAX_IMAGE_BYTES}


@app.post("/api/chat/message")
async def chat_message(req: ChatMessageRequest):
    if _store is None:
        raise HTTPException(status_code=500, detail="Redis not configured (REDIS_URL).")
    engine = str(req.engine or "").strip().lower()
    if engine not in ("engine1", "engine2"):
        raise HTTPException(status_code=400, detail="engine must be engine1 or engine2.")
    msg = str(req.message or "").strip()
    if not msg:
        raise HTTPException(status_code=400, detail="message is required.")
    if len(msg) > 8000:
        raise HTTPException(status_code=400, detail="message too long (max 8000 chars).")

    report = _store.get_json(f"latest_report:{engine}")
    if report is None:
        raise HTTPException(status_code=400, detail=f"No recent {engine} report found. Run the engine first.")
    if not isinstance(report, dict):
        raise HTTPException(status_code=500, detail="Stored report is invalid.")

    image_ids = list(req.image_ids or [])[:ASKRAVEN_MAX_IMAGES]
    images: list[UploadedImage] = []
    for image_id in image_ids:
        meta = _store.get_json(f"image_meta:{str(image_id)}")
        if not isinstance(meta, dict):
            continue
        path = meta.get("path")
        ct = meta.get("content_type") or "application/octet-stream"
        if not path:
            continue
        try:
            b = pathlib.Path(str(path)).read_bytes()
        except Exception:
            continue
        if len(b) > ASKRAVEN_MAX_IMAGE_BYTES:
            continue
        images.append(UploadedImage(content=b, content_type=str(ct), image_id=str(image_id)))

    ctx = build_context_pack(engine=engine, report=report)
    # Add Benzinga News snapshot (market/ticker) for “news of the day” style questions.
    news_ctx = _benzinga_news_context(engine=engine, report=report, question=msg)
    if news_ctx is not None:
        ctx["news"] = news_ctx
    try:
        reply = call_openai(question=msg, context_pack=ctx, images=images)
    except RuntimeError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    except Exception as e:
        # Surface a small, safe snippet so we can debug model/config issues in production.
        snippet = str(e) if e is not None else ""
        snippet = (snippet or "").replace("\n", " ").strip()
        if len(snippet) > 380:
            snippet = snippet[:380] + "…"
        # Some OpenAI SDK errors include a JSON-like body; try to include it if available.
        body = getattr(e, "body", None)
        if body is not None:
            try:
                body_txt = json.dumps(body, ensure_ascii=False)[:380]
                snippet = f"{snippet} body={body_txt}"
            except Exception:
                pass
        raise HTTPException(status_code=502, detail=f"LLM call failed: {type(e).__name__}: {snippet}") from e

    return {"ok": True, "engine": engine, "reply": reply, "used": {"images": len(images), "hasReport": True}}

def _get_client() -> OratsClient:
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is None:
            _client = OratsClient.from_env()
    return _client


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


def _breach_cache_key(ticker: str, n: int, years: int, k: float, flags_fp: tuple | None = None) -> tuple:
    # token is never part of key; include feature flags to prevent mixing methodologies
    fp = flags_fp if flags_fp is not None else get_flags().cache_fingerprint()
    return (ticker.strip().upper(), int(n), int(years), float(k), fp)

def _spx_ic_cache_key(params: dict, flags_fp: tuple) -> tuple:
    # stable primitives only
    items = tuple(sorted((k, str(v)) for k, v in (params or {}).items()))
    return ("spx_ic", items, flags_fp)


# Static frontend
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index():
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
    }


@app.get("/spx")
def spx_page():
    spx_path = STATIC_DIR / "spx.html"
    if not spx_path.exists():
        raise HTTPException(status_code=500, detail="Missing static/spx.html")
    return FileResponse(str(spx_path))


@app.get("/api/spx-ic")
def spx_ic(
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
        params = {
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

        ws: List[float] = []
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
        _store_set_latest("engine2", payload)
        return payload
    except OratsError as e:
        LOG.exception("ORATS failure (spx-ic)")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure (spx-ic)")
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
                    fresh["current"] = compute_current_snapshot(client=_get_client(), ticker=ticker.strip().upper())
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
        _store_set_latest("engine1", payload)
        return payload
    except BreachInputError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except OratsError as e:
        LOG.exception("ORATS failure")
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        LOG.exception("Unhandled failure")
        raise HTTPException(status_code=500, detail="Internal error") from e


