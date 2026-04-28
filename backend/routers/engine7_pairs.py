"""Engine 7 (backend) / Engine 6 (UI): Thematic Relative Value (Pairs) router.

Backend module is named engine7 for historical reasons. Users see this as
Engine 6 in the navigation. See ENGINE_REGISTRY in config.py.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import threading
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from backend.deps import (
    LOG,
    get_client,
    get_client_optional,
    engine7_cache,
    engine7_cache_lock,
)
from backend.config import get_flags
from backend.orats_client import OratsError
from backend.redis_store import get_store_optional

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_gate_context(flags) -> dict:
    """Gather regime and vol context for gating decisions.

    Routes through the canonical Market Intelligence v2 service when
    available so E3/E4/E7 gates always agree. Falls back to the raw
    Engine 5 snapshot when MI v2 is disabled.
    """
    ctx = {
        "regime_label": "",
        "vol_direction": "",
        "gamma_ctx": None,
        "high_events_within_days": 0,
    }

    if getattr(flags, "ENABLE_MI_V2", True):
        try:
            from backend.market_intel import regime_snapshot
            mi = regime_snapshot()
            if mi.label:
                ctx["regime_label"] = mi.label
            term = str((mi.vol_state or {}).get("term_structure", "")).lower()
            if term == "backwardation":
                ctx["vol_direction"] = "rising"
            elif term == "contango":
                ctx["vol_direction"] = "falling"
            elif term == "flat":
                ctx["vol_direction"] = "stable"
        except Exception:
            pass

    try:
        store = get_store_optional()
        if store and flags.ENABLE_ENGINE5_LEAD_LAG:
            from backend.routers.engine5_lead_lag import _engine5_get_best_snapshot

            snap = _engine5_get_best_snapshot(store, flags)
            if snap:
                data = snap.get("data", {})
                regime = data.get("regime", {})
                if not ctx["regime_label"]:
                    ctx["regime_label"] = regime.get("label") or regime.get("current_label") or ""
                if not ctx["vol_direction"]:
                    vol = data.get("volLeadLag", {})
                    ctx["vol_direction"] = vol.get("global_vol_direction") or vol.get("globalVolDirection") or ""
    except Exception:
        pass
    return ctx


# ---------------------------------------------------------------------------
# LLM system prompt
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@router.get("/api/engine7-pairs")
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
            enable_llm_theme_scoring=flags.ENGINE7_ENABLE_LLM_THEME_SCORING,
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
                                regime_allow=flags.GATE_PAIRS_REGIME_ALLOW,
                                vol_state_allow=flags.GATE_PAIRS_VOL_STATE_ALLOW,
                            )
                            sig["gateDecision"] = gd.to_dict()
            except Exception as gate_err:
                LOG.warning("Gate injection failed for engine7: %s", gate_err)

        result["updatedAt"] = dt.datetime.utcnow().isoformat() + "Z"
        return result

    except Exception as exc:
        LOG.exception("Engine7 scan failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Engine 7 scan failed: {exc}")


@router.post("/api/engine7-pairs/clear-cache")
def engine7_clear_cache():
    """Force-clear Engine 7 in-memory caches (scan + theme). Bars kept."""
    from backend.engine7_screener import clear_engine7_caches
    clear_engine7_caches()
    return {"status": "ok", "message": "Engine 7 scan and theme caches cleared"}


@router.post("/api/engine7-pairs/nightly-review")
def engine7_nightly_review(
    date: Optional[str] = Query(None, description="Review date (YYYY-MM-DD), defaults to today"),
):
    """Engine 7: Run the LLM nightly theme review pipeline.

    Analyzes recent headlines with gpt-5.5 to identify emerging macro
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


@router.get("/api/engine7-pairs/dynamic-themes")
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


@router.post("/api/engine7-pairs/desk-view")
def engine7_desk_view(body: dict):
    """Engine 7: Generate a GPT-5.5 senior quant desk view for a pair signal."""
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
            model="gpt-5.5",
            messages=[
                {"role": "system", "content": _E7_DESK_VIEW_SYSTEM},
                {"role": "user", "content": payload},
            ],
            temperature=1,
            max_completion_tokens=1200,
            timeout=30,
            response_format={"type": "json_object"},
        )
        content = resp.choices[0].message.content or ""
        parsed = _parse_desk_brief_json(content)
        if parsed is None:
            raise HTTPException(status_code=502, detail="LLM returned unparseable response")

        parsed["_source"] = "gpt-5.5"
        parsed["_pair"] = signal.get("pair_id", "")
        return parsed

    except HTTPException:
        raise
    except Exception as exc:
        LOG.exception("Engine7 desk-view failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Desk view generation failed: {exc}")


@router.get("/api/engine7-pairs/themes")
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


@router.get("/api/engine7-pairs/{pair_id}")
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
