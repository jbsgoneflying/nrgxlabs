"""Engine 7 – LLM-augmented keyword auto-promotion.

Nightly review pipeline that uses gpt-5.5 to identify emerging macro
narratives not covered by the static THEME_KEYWORD_MAP, then auto-promotes
them into the theme classifier via a two-track system:

  Track 1 (high-urgency): headline saturation >= 10% → active immediately.
  Track 2 (emerging):     saturation < 10%, confirmed in 2 of 3 consecutive
                          nightly reviews → promoted to active.

Active dynamic themes auto-expire after 7 days without reappearance.
Max 3 concurrent active dynamic themes at any time.
Static themes are NEVER modified — dynamic themes are additive only.

Persistence: data/dynamic_themes.json (survives restarts, easy to audit).
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

_LOG = logging.getLogger(__name__)

_DYNAMIC_THEMES_PATH = Path(__file__).resolve().parent.parent / "data" / "dynamic_themes.json"
_FILE_LOCK = threading.Lock()

_LLM_MODEL = "gpt-5.5"
_MAX_ACTIVE_DYNAMIC = 3
_TRACK1_SATURATION = 0.10
_TRACK2_CONFIRM_NIGHTS = 2
_TRACK2_WINDOW_NIGHTS = 3
_EXPIRY_DAYS = 7


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------


def _read_store() -> Dict[str, Any]:
    """Read dynamic_themes.json. Returns empty structure on any error."""
    try:
        if _DYNAMIC_THEMES_PATH.exists():
            with open(_DYNAMIC_THEMES_PATH, "r") as f:
                return json.load(f)
    except Exception as exc:
        _LOG.warning("Failed to read dynamic_themes.json: %s", exc)
    return {"version": 1, "last_review": None, "themes": {}, "audit_log": []}


def _write_store(store: Dict[str, Any]) -> None:
    """Atomically write dynamic_themes.json."""
    try:
        _DYNAMIC_THEMES_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _DYNAMIC_THEMES_PATH.with_suffix(".tmp")
        with open(tmp, "w") as f:
            json.dump(store, f, indent=2, default=str)
        tmp.replace(_DYNAMIC_THEMES_PATH)
    except Exception as exc:
        _LOG.error("Failed to write dynamic_themes.json: %s", exc)


def _append_audit(store: Dict[str, Any], date_str: str, action: str,
                  theme: str, reason: str,
                  headline_sample: Optional[List[str]] = None) -> None:
    log = store.setdefault("audit_log", [])
    log.append({
        "date": date_str,
        "action": action,
        "theme": theme,
        "reason": reason,
        "headline_sample": (headline_sample or [])[:5],
    })
    # Keep audit log bounded (last 200 entries)
    if len(log) > 200:
        store["audit_log"] = log[-200:]


# ---------------------------------------------------------------------------
# LLM prompt
# ---------------------------------------------------------------------------

_REVIEW_SYSTEM_PROMPT = """You are a senior macro strategist at a systematic trading desk.

Your job: analyze today's financial headlines and identify EMERGING macro narratives
that are NOT already covered by our existing static theme list.

Our static themes (DO NOT propose these):
- ai_expansion, ai_deceleration
- rates_rising, rates_falling
- risk_off, risk_on
- liquidity_expansion, liquidity_tightening
- energy_shock
- sector_rotation

We trade these 20 ETF pairs:
QQQ/IWM, QQQ/SPY, SOXX/IGV, XLF/XLK, IBIT/QQQ, RSP/SPY,
GLD/TLT, HYG/TLT, XHB/SPY, UUP/SPY,
VIXY/SPY, SPHB/SPLV, XLY/XLP, SPY/EFA, XLI/XLU,
SPY/IWM, XLB/XLI, XBI/XLV, XLE/SPY, USO/SPY

For each NEW narrative you identify:
1. Create a snake_case theme_id (e.g., "china_stimulus", "trump_tariff_escalation")
2. A human-readable label
3. 5-15 keyword phrases that would appear in headlines about this theme
4. Which of our 20 pairs would be affected, and the directional bias:
   - "long_ratio" = the first/long leg outperforms
   - "short_ratio" = the second/short leg outperforms
5. Estimate what percentage of today's headlines relate to this narrative (0.0-1.0)

Rules:
- Only propose themes that are GENUINELY NEW and not covered by our static list
- Keywords should be lowercase phrases (2-4 words ideal) that appear naturally in headlines
- Only map to pairs from our 20-pair universe listed above (use format TICKER1_TICKER2)
- If no new narratives are emerging, return an empty array — don't force it
- Maximum 5 proposals per review

Respond with valid JSON only:
{
  "proposals": [
    {
      "theme_id": "example_theme",
      "label": "Example Theme",
      "keywords": ["keyword one", "keyword two"],
      "pair_mappings": [
        {"pair_id": "QQQ_IWM", "bias": "long_ratio"}
      ],
      "headline_saturation": 0.15,
      "reasoning": "One sentence explaining why this is a distinct new narrative"
    }
  ]
}"""


def _build_review_prompt(headlines: List[str]) -> str:
    """Build the user message with today's headlines."""
    hl_text = "\n".join(f"- {h}" for h in headlines[:100])
    return f"Today's headlines ({len(headlines)} total, showing top 100):\n\n{hl_text}"


# ---------------------------------------------------------------------------
# Pair validation
# ---------------------------------------------------------------------------

_VALID_PAIR_IDS: Optional[set] = None


def _get_valid_pair_ids() -> set:
    """Load valid pair_ids from the pair library. Cached after first call."""
    global _VALID_PAIR_IDS
    if _VALID_PAIR_IDS is not None:
        return _VALID_PAIR_IDS
    try:
        from backend.engine7_pairs import load_pair_library
        lib = load_pair_library()
        _VALID_PAIR_IDS = {p.pair_id for p in lib}
    except Exception:
        _VALID_PAIR_IDS = set()
    return _VALID_PAIR_IDS


def _validate_proposal(proposal: dict) -> Optional[dict]:
    """Validate and clean a single LLM proposal. Returns None if invalid."""
    theme_id = proposal.get("theme_id", "").strip().lower().replace(" ", "_")
    if not theme_id or len(theme_id) < 3:
        return None

    label = proposal.get("label", "").strip()
    if not label:
        label = theme_id.replace("_", " ").title()

    keywords = proposal.get("keywords", [])
    if not isinstance(keywords, list):
        return None
    keywords = [str(k).strip().lower() for k in keywords if str(k).strip()]
    if len(keywords) < 2:
        return None

    valid_pairs = _get_valid_pair_ids()
    raw_mappings = proposal.get("pair_mappings", [])
    pair_mappings = []
    for pm in raw_mappings:
        if not isinstance(pm, dict):
            continue
        pid = pm.get("pair_id", "")
        bias = pm.get("bias", "")
        if pid in valid_pairs and bias in ("long_ratio", "short_ratio"):
            pair_mappings.append({"pair_id": pid, "bias": bias})

    if not pair_mappings:
        _LOG.info("LLM review: rejecting '%s' — no valid pair mappings", theme_id)
        return None

    saturation = float(proposal.get("headline_saturation", 0.0))
    reasoning = str(proposal.get("reasoning", ""))

    return {
        "theme_id": theme_id,
        "label": label,
        "keywords": keywords[:15],
        "pair_mappings": pair_mappings,
        "headline_saturation": round(min(max(saturation, 0.0), 1.0), 4),
        "reasoning": reasoning[:200],
    }


# ---------------------------------------------------------------------------
# Two-track promotion logic
# ---------------------------------------------------------------------------


def _apply_proposals(store: Dict[str, Any], proposals: List[dict],
                     date_str: str, headlines: List[str]) -> None:
    """Apply validated proposals using the two-track system."""
    from backend.engine7_theme import THEME_KEYWORD_MAP

    themes = store.setdefault("themes", {})

    for prop in proposals:
        tid = prop["theme_id"]

        # Never overwrite a static theme
        if tid in THEME_KEYWORD_MAP:
            _LOG.info("LLM review: skipping '%s' — collides with static theme", tid)
            continue

        saturation = prop["headline_saturation"]
        existing = themes.get(tid)

        if existing:
            # Update existing dynamic theme
            existing["last_seen"] = date_str
            appearances = existing.setdefault("appearances", [])
            if date_str not in appearances:
                appearances.append(date_str)
            existing["headline_saturation"] = max(
                existing.get("headline_saturation", 0), saturation
            )
            # Merge any new keywords the LLM found
            old_kws = set(existing.get("keywords", []))
            for kw in prop["keywords"]:
                if kw not in old_kws:
                    existing.setdefault("keywords", []).append(kw)

            # Check Track 2 promotion for pending themes
            if existing.get("status") == "pending":
                recent_appearances = [
                    d for d in appearances
                    if (dt.date.fromisoformat(date_str) - dt.date.fromisoformat(d)).days
                    < _TRACK2_WINDOW_NIGHTS
                ]
                if len(recent_appearances) >= _TRACK2_CONFIRM_NIGHTS:
                    existing["status"] = "active"
                    existing["promoted_at"] = date_str
                    existing["activation_reason"] = "track2_consecutive"
                    _LOG.info("LLM review: PROMOTED '%s' via Track 2 (2/%d nights)", tid, _TRACK2_WINDOW_NIGHTS)
                    _append_audit(store, date_str, "promote", tid,
                                  f"Track 2: seen {len(recent_appearances)}/{_TRACK2_WINDOW_NIGHTS} nights",
                                  headlines[:3])
        else:
            # New theme proposal
            entry = {
                "label": prop["label"],
                "keywords": prop["keywords"],
                "pair_mappings": prop["pair_mappings"],
                "activation_threshold": 0.01,
                "first_seen": date_str,
                "last_seen": date_str,
                "appearances": [date_str],
                "headline_saturation": saturation,
                "reasoning": prop["reasoning"],
            }

            if saturation >= _TRACK1_SATURATION:
                entry["status"] = "active"
                entry["promoted_at"] = date_str
                entry["activation_reason"] = "track1_saturation"
                _LOG.info(
                    "LLM review: ACTIVATED '%s' via Track 1 (%.1f%% saturation)",
                    tid, saturation * 100,
                )
                _append_audit(store, date_str, "activate", tid,
                              f"Track 1: {saturation:.1%} saturation",
                              headlines[:3])
            else:
                entry["status"] = "pending"
                entry["activation_reason"] = None
                entry["promoted_at"] = None
                _LOG.info(
                    "LLM review: PENDING '%s' (%.1f%% saturation, needs %d/%d nights)",
                    tid, saturation * 100, _TRACK2_CONFIRM_NIGHTS, _TRACK2_WINDOW_NIGHTS,
                )
                _append_audit(store, date_str, "pending", tid,
                              f"Track 2 pending: {saturation:.1%} saturation")

            themes[tid] = entry


def _run_expiry(store: Dict[str, Any], date_str: str) -> None:
    """Remove dynamic themes not seen in the last EXPIRY_DAYS nightly runs."""
    themes = store.get("themes", {})
    today = dt.date.fromisoformat(date_str)
    expired = []

    for tid, tdata in list(themes.items()):
        last_seen = tdata.get("last_seen", "")
        if not last_seen:
            expired.append(tid)
            continue
        try:
            days_since = (today - dt.date.fromisoformat(last_seen)).days
        except Exception:
            days_since = 999
        if days_since > _EXPIRY_DAYS:
            expired.append(tid)

    for tid in expired:
        _LOG.info("LLM review: EXPIRED '%s' (not seen in %d days)", tid, _EXPIRY_DAYS)
        _append_audit(store, date_str, "expire", tid,
                      f"Not seen in {_EXPIRY_DAYS} days")
        del themes[tid]


def _enforce_max_active(store: Dict[str, Any], date_str: str) -> None:
    """If more than MAX_ACTIVE dynamic themes, keep only the top by saturation."""
    themes = store.get("themes", {})
    active = [(tid, t) for tid, t in themes.items() if t.get("status") == "active"]

    if len(active) <= _MAX_ACTIVE_DYNAMIC:
        return

    active.sort(key=lambda x: x[1].get("headline_saturation", 0), reverse=True)
    demoted = active[_MAX_ACTIVE_DYNAMIC:]

    for tid, tdata in demoted:
        tdata["status"] = "pending"
        _LOG.info("LLM review: DEMOTED '%s' (max %d active exceeded)", tid, _MAX_ACTIVE_DYNAMIC)
        _append_audit(store, date_str, "demote", tid,
                      f"Max {_MAX_ACTIVE_DYNAMIC} active dynamic themes exceeded")


# ---------------------------------------------------------------------------
# LLM call
# ---------------------------------------------------------------------------


def _call_llm(headlines: List[str]) -> List[dict]:
    """Call gpt-5.5 to identify emerging narratives. Returns raw proposals."""
    try:
        from backend.llm_client import _get_openai_client
    except ImportError:
        _LOG.warning("LLM client not available for nightly review")
        return []

    client = _get_openai_client()
    if client is None:
        _LOG.warning("OpenAI client unavailable for nightly review")
        return []

    user_msg = _build_review_prompt(headlines)

    try:
        resp = client.chat.completions.create(
            model=_LLM_MODEL,
            messages=[
                {"role": "system", "content": _REVIEW_SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            temperature=1,
            max_tokens=2000,
            timeout=60,
        )
        content = resp.choices[0].message.content or ""
        # Strip markdown fences if present
        if content.startswith("```"):
            content = content.split("\n", 1)[-1]
            if content.endswith("```"):
                content = content[:-3]
        parsed = json.loads(content)
        return parsed.get("proposals", [])
    except json.JSONDecodeError as exc:
        _LOG.warning("LLM review: JSON parse failed: %s", exc)
        return []
    except Exception as exc:
        _LOG.warning("LLM review: API call failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def review_and_propose(date_str: Optional[str] = None) -> Dict[str, Any]:
    """Run the nightly LLM review pipeline.

    Returns a summary dict with proposals, activations, and current state.
    """
    if not date_str:
        date_str = dt.date.today().isoformat()

    _LOG.info("Engine7 LLM nightly review starting for %s", date_str)

    # 1. Fetch headlines
    from backend.engine7_theme import fetch_headlines
    headlines = fetch_headlines(date_str, lookback_days=7)
    if not headlines:
        _LOG.warning("Engine7 LLM review: 0 headlines, skipping")
        return {"status": "skipped", "reason": "no_headlines", "date": date_str}

    # 2. Call LLM
    raw_proposals = _call_llm(headlines)
    _LOG.info("Engine7 LLM review: %d raw proposals from %s", len(raw_proposals), _LLM_MODEL)

    # 3. Validate proposals
    validated = []
    for rp in raw_proposals:
        v = _validate_proposal(rp)
        if v:
            validated.append(v)
    _LOG.info("Engine7 LLM review: %d validated proposals", len(validated))

    # 4. Apply to store
    with _FILE_LOCK:
        store = _read_store()
        _apply_proposals(store, validated, date_str, headlines)
        _run_expiry(store, date_str)
        _enforce_max_active(store, date_str)
        store["last_review"] = date_str
        _write_store(store)

    # 5. Build summary
    active_dynamic = {
        tid: t for tid, t in store.get("themes", {}).items()
        if t.get("status") == "active"
    }
    pending_dynamic = {
        tid: t for tid, t in store.get("themes", {}).items()
        if t.get("status") == "pending"
    }

    summary = {
        "status": "ok",
        "date": date_str,
        "model": _LLM_MODEL,
        "headlineCount": len(headlines),
        "rawProposals": len(raw_proposals),
        "validatedProposals": len(validated),
        "activeDynamic": list(active_dynamic.keys()),
        "pendingDynamic": list(pending_dynamic.keys()),
        "activeDynamicCount": len(active_dynamic),
        "pendingDynamicCount": len(pending_dynamic),
        "proposals": validated,
    }
    _LOG.info(
        "Engine7 LLM review complete: %d active, %d pending dynamic themes",
        len(active_dynamic), len(pending_dynamic),
    )
    return summary


def load_dynamic_themes() -> Dict[str, dict]:
    """Load active dynamic themes for merge into the classifier.

    Returns a dict of theme_id -> theme definition (same shape as
    THEME_KEYWORD_MAP entries) for themes with status == "active".
    Catches all exceptions — returns empty dict on failure so the
    scanner degrades gracefully to static-only.
    """
    try:
        store = _read_store()
        result: Dict[str, dict] = {}
        for tid, tdata in store.get("themes", {}).items():
            if tdata.get("status") != "active":
                continue
            result[tid] = {
                "label": tdata.get("label", tid.replace("_", " ").title()),
                "keywords": tdata.get("keywords", []),
                "activation_threshold": tdata.get("activation_threshold", 0.01),
                "pair_mappings": tdata.get("pair_mappings", []),
                "dynamic": True,
                "promoted_at": tdata.get("promoted_at"),
                "activation_reason": tdata.get("activation_reason"),
                "headline_saturation": tdata.get("headline_saturation", 0),
            }
        return result
    except Exception as exc:
        _LOG.warning("Failed to load dynamic themes: %s — falling back to static-only", exc)
        return {}
