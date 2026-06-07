"""NRGX Labs v2 FastAPI entrypoint.

Runs on port 8001 (set by V2_BIND_PORT). Reverse-proxied at
``v2.app.nrgxlabs.com`` (or ``/v2/*`` on the root domain pre-DNS).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .auth import invite_gate
from .config import get_config
from .routers import (
    analogues,
    committee,
    conformal,
    counterfactual,
    health,
    paths,
    regime,
)

try:
    load_dotenv()
except Exception:
    pass


def _configure_logging() -> None:
    level = (os.getenv("LOG_LEVEL") or "INFO").upper()
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )


_configure_logging()
LOG = logging.getLogger("v2.app")

CFG = get_config()

app = FastAPI(title="NRGX Labs v2", version=__version__)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(CFG.cors_origins),
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _gate(request: Request, call_next):
    return await invite_gate(request, call_next, CFG)


app.include_router(health.router)
app.include_router(regime.router)
app.include_router(analogues.router)
app.include_router(counterfactual.router)
app.include_router(conformal.router)
app.include_router(committee.router)
app.include_router(paths.router)


# ── Static frontend ──
ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "static-v2"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static-v2")


@app.get("/", response_class=HTMLResponse)
def index():
    f = STATIC_DIR / "index.html"
    if f.exists():
        return FileResponse(str(f))
    return HTMLResponse(
        "<h1>NRGX Labs v2</h1><p>Frontend not yet built. See /api/v2/health.</p>"
    )


@app.get("/favicon.ico")
def favicon():
    f = STATIC_DIR / "assets" / "favicon-v2.svg"
    if f.exists():
        return FileResponse(str(f), media_type="image/svg+xml")
    return HTMLResponse("", status_code=204)


# Engine landing pages share one HTML file; engine.js inspects the URL path
# and renders the per-engine spec. Real per-engine UIs land as each v2 engine
# ships in Phase 2.
def _engine_page():
    f = STATIC_DIR / "engine.html"
    if f.exists():
        return FileResponse(str(f))
    return FileResponse(str(STATIC_DIR / "index.html"))


for slug in ("e1", "e2", "e14", "e15", "mi"):
    app.get(f"/{slug}", response_class=HTMLResponse)(_engine_page)


LOG.info("NRGX Labs v2 ready - version=%s public=%s", __version__, CFG.public_access)
