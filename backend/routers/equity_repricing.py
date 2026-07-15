"""Equity Repricing Lab — shadow command-surface API (post-promotion only).

Routes are registered but return 404-style disabled payloads unless
REPRICING_LAB_ENABLED=1. No ENGINE_REGISTRY entry; no live capital path.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse

from backend.config import get_flags

router = APIRouter(prefix="/api/equity-repricing", tags=["equity-repricing-lab"])


def _require_enabled():
    flags = get_flags()
    if not flags.REPRICING_LAB_ENABLED:
        raise HTTPException(
            status_code=404,
            detail="Equity Repricing Lab disabled (REPRICING_LAB_ENABLED=0)",
        )
    return flags


@router.get("/health")
def lab_health():
    flags = get_flags()
    return {
        "enabled": bool(flags.REPRICING_LAB_ENABLED),
        "shadowOnly": bool(flags.REPRICING_LAB_SHADOW_ONLY),
        "shortEnabled": bool(flags.REPRICING_LAB_SHORT_ENABLED),
        "engineRegistered": False,
    }


@router.get("/scout")
def scout():
    flags = _require_enabled()
    from backend.repricing_lab.signals import list_shadow_candidates
    return {
        "shadowOnly": bool(flags.REPRICING_LAB_SHADOW_ONLY),
        "candidates": list_shadow_candidates(),
    }


@router.get("/validation")
def validation():
    _require_enabled()
    from backend.repricing_lab.signals import decay_check
    # Placeholder metrics until live shadow accumulates
    return {
        "decay": decay_check(expectancy_r=0.0, n=0),
        "note": "Shadow validation populates after promotion + prospective period",
    }


@router.get("/page", response_class=HTMLResponse)
def page():
    """Minimal scout page — only meaningful when the lab flag is on."""
    flags = get_flags()
    if not flags.REPRICING_LAB_ENABLED:
        return HTMLResponse(
            "<!doctype html><title>Lab</title><p>Equity Repricing Lab is disabled.</p>",
            status_code=404,
        )
    html = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"/>
<title>NRGX Equity Repricing Lab (Shadow)</title>
<link rel="stylesheet" href="/static/styles.css"/>
<script defer src="/static/nav.js"></script>
<script defer src="/static/equity-repricing.js"></script>
</head><body>
<main class="page">
  <h1>Equity Repricing Lab</h1>
  <p class="lede">Shadow command surface — research-promoted signals only. No live risk while shadow-only is on.</p>
  <section id="scout"><h2>Scout</h2><div id="scout-list">Loading…</div></section>
  <section id="validation"><h2>Validation</h2><pre id="validation-body"></pre></section>
</main>
</body></html>"""
    return HTMLResponse(html)
