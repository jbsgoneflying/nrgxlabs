"""Raven-Tech Front Layer – LLM Pipeline (Read-Only).

Generates Morning Brief and Weekly Roadmap from DailyMarketState.
Also includes deterministic Asymmetry Radar detection.

Hard Rules:
  - LLM never sees raw prices or P&L
  - LLM never outputs trades
  - LLM must cite which fields informed each statement
  - All outputs timestamped with source attribution
  - Fallback mode if LLM is unavailable
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rate limiter (separate budget from desk brief)
# ---------------------------------------------------------------------------


class _FrontLayerRateLimiter:
    """Token-bucket rate limiter for Front Layer LLM calls."""

    def __init__(self, max_calls_per_minute: int = 4):
        self._lock = threading.Lock()
        self._max = max_calls_per_minute
        self._timestamps: List[float] = []

    def acquire(self) -> bool:
        with self._lock:
            now = time.time()
            self._timestamps = [t for t in self._timestamps if now - t < 60.0]
            if len(self._timestamps) >= self._max:
                return False
            self._timestamps.append(now)
            return True


_rate_limiter = _FrontLayerRateLimiter()


# ---------------------------------------------------------------------------
# OpenAI client (reuse pattern from llm_client.py)
# ---------------------------------------------------------------------------


def _get_openai_client():
    """Lazy-load OpenAI client. Returns None if not available."""
    try:
        import openai
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None
        return openai.OpenAI(api_key=api_key)
    except ImportError:
        LOG.warning("openai package not installed; Front Layer LLM disabled")
        return None
    except Exception as e:
        LOG.warning("Failed to create OpenAI client: %s", e)
        return None


def _load_prompt(name: str) -> str:
    """Load a prompt template from backend/prompts/."""
    prompt_dir = Path(__file__).parent / "prompts"
    path = prompt_dir / name
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    return ""


def _parse_llm_json(content: str) -> Optional[dict]:
    """Parse LLM response, handling markdown code blocks."""
    content = content.strip()
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:-1])
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        LOG.warning("LLM returned invalid JSON")
        return None


# ---------------------------------------------------------------------------
# Morning Brief
# ---------------------------------------------------------------------------

_MORNING_BRIEF_FALLBACK: Dict[str, Any] = {
    "market_posture": "Market data is being processed. Review DailyMarketState cards directly.",
    "changes_vs_yesterday": "Diff data unavailable. Check regime and flow pressure cards.",
    "active_themes": "Theme scoring in progress. See Active Themes panel.",
    "cross_asset_signals": "Cross-asset data loading. Check stress grid.",
    "engine_alignment": "Engine gate status available in the engine gates panel.",
    "watch_list": "None",
    "stand_down": "Review regime state for stand-down guidance.",
    "_source": "fallback",
}

_MORNING_BRIEF_REQUIRED_KEYS = {
    "market_posture", "changes_vs_yesterday", "active_themes",
    "cross_asset_signals", "engine_alignment", "watch_list", "stand_down",
}


def _fallback_brief(reason: str) -> Dict[str, Any]:
    """Return morning brief fallback with reason attached."""
    fb = dict(_MORNING_BRIEF_FALLBACK)
    fb["_fallback_reason"] = reason
    return _add_timestamp(fb)


def generate_morning_brief(
    dms_today: dict,
    dms_history: Optional[List[dict]] = None,
) -> Dict[str, Any]:
    """Generate the Pre-Open Morning Brief from DailyMarketState.

    Args:
        dms_today: Today's DailyMarketState dict.
        dms_history: Rolling prior DailyMarketState dicts (newest first).

    Returns:
        Dict with morning brief sections. Includes _generated_at timestamp.
    """
    if not _rate_limiter.acquire():
        LOG.info("Morning brief rate-limited; returning fallback")
        return _fallback_brief("Rate limited (max 4 calls/minute)")

    client = _get_openai_client()
    if client is None:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            reason = "OPENAI_API_KEY not set in environment"
        else:
            reason = "OpenAI client failed to initialize (check openai package installation)"
        LOG.warning("Morning brief: %s", reason)
        return _fallback_brief(reason)

    system_prompt = _load_prompt("morning_brief.txt")
    if not system_prompt:
        reason = "Prompt file backend/prompts/morning_brief.txt not found"
        LOG.warning(reason)
        return _fallback_brief(reason)

    # Build context payload
    context = {
        "today": _sanitize_dms(dms_today),
    }
    if dms_history:
        context["prior_days"] = [_sanitize_dms(d) for d in dms_history[:5]]

    payload_str = json.dumps(context, default=str)
    # Truncate to fit token budget (~4000 tokens)
    if len(payload_str) > 12000:
        payload_str = payload_str[:12000] + "..."

    model = os.getenv("LLM_MODEL_NARRATIVE", "gpt-4o-mini").strip()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_str},
            ],
            temperature=0.2,
            max_tokens=800,
            timeout=15,
        )

        content = response.choices[0].message.content.strip()
        result = _parse_llm_json(content)

        if result is None or not _MORNING_BRIEF_REQUIRED_KEYS.issubset(set(result.keys())):
            LOG.warning("Morning brief LLM response missing required keys; got: %s",
                        list(result.keys()) if result else "None")
            return _fallback_brief("LLM returned invalid/incomplete JSON (model: " + model + ")")

        # Sanitize output lengths
        brief = {}
        for key in _MORNING_BRIEF_REQUIRED_KEYS:
            val = result.get(key, "")
            if isinstance(val, list):
                brief[key] = val
            else:
                brief[key] = str(val)[:500]

        brief["_source"] = "llm"
        return _add_timestamp(brief)

    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        LOG.warning("Morning brief LLM call failed: %s", reason)
        return _fallback_brief(reason)


# ---------------------------------------------------------------------------
# Weekly Roadmap
# ---------------------------------------------------------------------------

_WEEKLY_ROADMAP_FALLBACK: Dict[str, Any] = {
    "regime_flow_summary": "Weekly analysis pending. Review regime and flow pressure trend.",
    "expected_pattern": "Pattern detection in progress. Check sequencer panel.",
    "high_risk_days": [],
    "engine_behaviors": "Engine gate summary available in Command Center.",
    "earnings_focus": [],
    "asymmetry_radar": "No asymmetries detected.",
    "break_the_plan": "Check regime transition triggers for invalidation conditions.",
    "_source": "fallback",
}

_WEEKLY_ROADMAP_REQUIRED_KEYS = {
    "regime_flow_summary", "expected_pattern", "high_risk_days",
    "engine_behaviors", "earnings_focus", "asymmetry_radar", "break_the_plan",
}


def _fallback_roadmap(reason: str) -> Dict[str, Any]:
    """Return weekly roadmap fallback with reason attached."""
    fb = dict(_WEEKLY_ROADMAP_FALLBACK)
    fb["_fallback_reason"] = reason
    return _add_timestamp(fb)


def generate_weekly_roadmap(
    dms_today: dict,
    dms_history: Optional[List[dict]] = None,
) -> Dict[str, Any]:
    """Generate the Sunday Night Weekly Roadmap from DailyMarketState.

    Args:
        dms_today: Today's DailyMarketState dict.
        dms_history: Rolling prior week DailyMarketState dicts (newest first).

    Returns:
        Dict with weekly roadmap sections. Includes _generated_at timestamp.
    """
    if not _rate_limiter.acquire():
        LOG.info("Weekly roadmap rate-limited; returning fallback")
        return _fallback_roadmap("Rate limited (max 4 calls/minute)")

    client = _get_openai_client()
    if client is None:
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            reason = "OPENAI_API_KEY not set in environment"
        else:
            reason = "OpenAI client failed to initialize (check openai package installation)"
        LOG.warning("Weekly roadmap: %s", reason)
        return _fallback_roadmap(reason)

    system_prompt = _load_prompt("weekly_roadmap.txt")
    if not system_prompt:
        reason = "Prompt file backend/prompts/weekly_roadmap.txt not found"
        LOG.warning(reason)
        return _fallback_roadmap(reason)

    context = {
        "today": _sanitize_dms(dms_today),
    }
    if dms_history:
        context["prior_days"] = [_sanitize_dms(d) for d in dms_history[:7]]

    payload_str = json.dumps(context, default=str)
    if len(payload_str) > 15000:
        payload_str = payload_str[:15000] + "..."

    model = os.getenv("LLM_MODEL_NARRATIVE", "gpt-4o-mini").strip()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_str},
            ],
            temperature=0.2,
            max_tokens=1000,
            timeout=20,
        )

        content = response.choices[0].message.content.strip()
        result = _parse_llm_json(content)

        if result is None or not _WEEKLY_ROADMAP_REQUIRED_KEYS.issubset(set(result.keys())):
            LOG.warning("Weekly roadmap LLM response missing required keys; got: %s",
                        list(result.keys()) if result else "None")
            return _fallback_roadmap("LLM returned invalid/incomplete JSON (model: " + model + ")")

        roadmap: Dict[str, Any] = {}
        for key in _WEEKLY_ROADMAP_REQUIRED_KEYS:
            val = result.get(key, "")
            if isinstance(val, list):
                roadmap[key] = val
            else:
                roadmap[key] = str(val)[:500]

        # Enforce max 2 earnings focus
        if isinstance(roadmap.get("earnings_focus"), list):
            roadmap["earnings_focus"] = roadmap["earnings_focus"][:2]

        roadmap["_source"] = "llm"
        return _add_timestamp(roadmap)

    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        LOG.warning("Weekly roadmap LLM call failed: %s", reason)
        return _fallback_roadmap(reason)


# ---------------------------------------------------------------------------
# Asset Insight (desk-level LLM tooltip)
# ---------------------------------------------------------------------------

_ASSET_INSIGHT_SYSTEM = """You are a senior cross-asset desk strategist at a proprietary trading firm.

Given a single asset's stress reading and the broader market context (DailyMarketState),
produce a concise, desk-ready insight explaining:

1. WHAT THIS ASSET IS TELLING US — plain English, no jargon. What is this move or lack of move signaling?
2. WHY IT MATTERS FOR EQUITIES — how does this asset historically relate to US equity risk?
3. CONTEXT — is today's reading unusual vs recent history? Is it confirming or contradicting other signals?
4. DESK TAKEAWAY — one sentence: what should the desk do with this information?

Rules:
- Never recommend specific trades or positions
- Never mention prices, P&L, or dollar amounts
- Always cite the stress score, direction, and equity relationship in your reasoning
- Use the regime, flow pressure, and theme context to add depth
- Keep total response under 200 words
- Be direct and actionable in tone — this is for professional traders

Return valid JSON:
{
  "what_its_telling_us": "...",
  "why_it_matters": "...",
  "context": "...",
  "desk_takeaway": "..."
}"""

_ASSET_INSIGHT_REQUIRED_KEYS = {"what_its_telling_us", "why_it_matters", "context", "desk_takeaway"}


def generate_asset_insight(
    asset_reading: dict,
    dms_summary: dict,
) -> Dict[str, Any]:
    """Generate a desk-level LLM insight for a single cross-asset stress reading.

    Args:
        asset_reading: Single AssetStressReading dict (symbol, name, stress_score, etc.)
        dms_summary: Condensed DailyMarketState context (regime, flow, vol, themes).

    Returns:
        Dict with insight sections + _source tag.
    """
    fallback = {
        "what_its_telling_us": "Insight unavailable. Review the stress score and direction above.",
        "why_it_matters": "Check the equity relationship label for confirmation or divergence signals.",
        "context": "Compare today's reading against recent history in the DMS diff panel.",
        "desk_takeaway": "Use the composite stress score and individual readings to inform positioning.",
        "_source": "fallback",
    }

    if not _rate_limiter.acquire():
        LOG.info("Asset insight rate-limited; returning fallback")
        fallback["_fallback_reason"] = "Rate limited (max 4 calls/minute). Wait a moment and try again."
        return fallback

    client = _get_openai_client()
    if client is None:
        fallback["_fallback_reason"] = "OpenAI client unavailable"
        return fallback

    # Build compact context
    context = {
        "asset": asset_reading,
        "market": {
            "regime": dms_summary.get("regime", {}),
            "flow_pressure": dms_summary.get("flow_pressure", {}),
            "vol_state": dms_summary.get("vol_state", {}),
            "composite_stress": dms_summary.get("cross_asset_stress", {}).get("composite_score"),
            "composite_label": dms_summary.get("cross_asset_stress", {}).get("composite_label"),
            "dominant_theme": next(
                (t.get("theme") for t in dms_summary.get("news_themes", [])
                 if float(t.get("intensity", 0)) > 20), None
            ),
        },
    }

    payload_str = json.dumps(context, default=str)
    model = os.getenv("LLM_MODEL_NARRATIVE", "gpt-4o-mini").strip()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _ASSET_INSIGHT_SYSTEM},
                {"role": "user", "content": payload_str},
            ],
            temperature=0.3,
            max_tokens=400,
            timeout=12,
        )

        content = response.choices[0].message.content.strip()
        result = _parse_llm_json(content)

        if result is None or not _ASSET_INSIGHT_REQUIRED_KEYS.issubset(set(result.keys())):
            LOG.warning("Asset insight LLM response missing required keys")
            fallback["_fallback_reason"] = "LLM returned invalid JSON"
            return fallback

        insight = {}
        for key in _ASSET_INSIGHT_REQUIRED_KEYS:
            val = result.get(key, "")
            insight[key] = str(val)[:400]

        insight["_source"] = "llm"
        insight["_asset"] = asset_reading.get("name", "")
        return insight

    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        LOG.warning("Asset insight LLM call failed: %s", reason)
        fallback["_fallback_reason"] = reason
        return fallback


# ---------------------------------------------------------------------------
# Generalized Card Insight (desk-level LLM tooltip for any MI card)
# ---------------------------------------------------------------------------

_CARD_INSIGHT_PROMPTS: Dict[str, str] = {
    "composite": """You are a senior cross-asset desk strategist.

Given the composite cross-asset stress snapshot (all asset readings, composite score, composite label)
and the broader DailyMarketState context, produce a desk-ready insight:

1. WHAT THE COMPOSITE IS TELLING US — Is cross-asset stress confirming or contradicting equities right now?
2. KEY DRIVERS — Which 1-2 asset classes are driving the composite score today and why?
3. HISTORICAL CONTEXT — Is this composite level unusual? What typically happens at this level?
4. DESK TAKEAWAY — One sentence: what should the desk understand from this composite reading?

Rules: Never recommend trades. Never mention prices or P&L. Be direct, cite scores. Under 200 words.

Return valid JSON:
{ "what_its_telling_us": "...", "key_drivers": "...", "historical_context": "...", "desk_takeaway": "..." }""",

    "theme": """You are a senior macro-narrative analyst at a proprietary trading firm.

Given a single news theme cluster (theme name, intensity, acceleration, persistence, affected sectors, keyword hits)
and the broader DailyMarketState context, produce a desk-ready insight:

1. WHAT THIS THEME MEANS — Plain English: what is this narrative about and why is it showing up?
2. MARKET IMPACT — How does this type of theme historically affect equities, vol, or sector rotation?
3. MOMENTUM READ — Is this theme accelerating, fading, or steady? What does the persistence tell us?
4. DESK TAKEAWAY — One sentence: what should the desk watch for related to this theme?

Rules: Never recommend trades. Be direct, cite intensity/acceleration. Under 200 words.

Return valid JSON:
{ "what_this_theme_means": "...", "market_impact": "...", "momentum_read": "...", "desk_takeaway": "..." }""",

    "regime": """You are a senior market regime analyst at a proprietary trading firm.

Given the current regime state (score, label, engine gates) and the broader DailyMarketState context,
produce a desk-ready insight:

1. WHAT THE REGIME IS TELLING US — What does this regime score and label mean in practical terms?
2. ENGINE IMPLICATIONS — Given the current gate states, which engines should be active and which should be cautious?
3. REGIME CONTEXT — Is this regime stable, transitioning, or stressed? How long has it persisted?
4. DESK TAKEAWAY — One sentence: how should the desk think about risk allocation given this regime?

Rules: Never recommend specific trades. Cite the regime score and gate states. Under 200 words.

Return valid JSON:
{ "what_regime_tells_us": "...", "engine_implications": "...", "regime_context": "...", "desk_takeaway": "..." }""",

    "flow": """You are a senior flow and positioning analyst at a proprietary trading firm.

Given the current flow pressure reading (score, state) and the broader DailyMarketState context,
produce a desk-ready insight:

1. WHAT FLOW IS TELLING US — What does this flow pressure score and state mean? Is money moving in or out?
2. FLOW VS REGIME — Is flow confirming or diverging from the regime? What does that divergence imply?
3. CONTEXT — Is today's flow reading unusual vs recent history? Any inflection signals?
4. DESK TAKEAWAY — One sentence: what does flow pressure tell the desk about near-term positioning sentiment?

Rules: Never recommend trades. Cite the flow score and regime. Under 200 words.

Return valid JSON:
{ "what_flow_tells_us": "...", "flow_vs_regime": "...", "context": "...", "desk_takeaway": "..." }""",

    "asymmetry": """You are a senior risk intelligence analyst at a proprietary trading firm.

Given a specific asymmetry signal (type, description, severity, action, sources) and the broader
DailyMarketState context, produce a desk-ready insight:

1. WHAT THIS ASYMMETRY MEANS — Plain English: what dislocation or divergence has been detected?
2. WHY IT MATTERS — What is the historical significance of this type of asymmetry?
3. WHAT TO WATCH — What would confirm or invalidate this signal?
4. DESK TAKEAWAY — One sentence: how should the desk think about this asymmetry?

Rules: ALWAYS say "Monitor only / No action yet". Never recommend trades. Under 200 words.

Return valid JSON:
{ "what_this_means": "...", "why_it_matters": "...", "what_to_watch": "...", "desk_takeaway": "..." }""",

    "diff": """You are a senior market intelligence analyst at a proprietary trading firm.

Given the day-over-day changes between yesterday's and today's DailyMarketState (changed fields, old vs new values)
and today's full DMS context, produce a desk-ready insight:

1. WHAT CHANGED — Summarize the most important changes in plain English. What shifted overnight?
2. SIGNIFICANCE — Are these changes meaningful or noise? Which changes break from recent patterns?
3. CASCADING EFFECTS — Do any of these changes affect how the desk should think about other signals?
4. DESK TAKEAWAY — One sentence: what is the single most important thing that changed?

Rules: Never recommend trades. Be specific about which fields changed and by how much. Under 200 words.

Return valid JSON:
{ "what_changed": "...", "significance": "...", "cascading_effects": "...", "desk_takeaway": "..." }""",

    # ── Engine 5: Lead-Lag card types ──────────────────────────────────

    "e5_regime": """You are a senior global macro strategist at a proprietary options desk.

Given the Engine 5 Global Regime classification (label, score, stress components for FX, yield,
commodity, and IV, allowed structures, position size modifier, suppression flags), explain to the desk:

1. WHAT THE REGIME MEANS — What does this regime label and score mean for how the desk trades this week?
2. STRUCTURE GUIDANCE — Given the allowed structures and position size modifier, what does the desk lean into vs avoid?
3. STRESS COMPONENTS — Which of the 4 stress components (FX, Yield, Commodity, IV) are driving the regime and why does that matter?
4. DESK TAKEAWAY — One sentence: how should the desk size and structure given this regime?

Rules: Never recommend specific trades. Cite the stress scores and regime label. Under 250 words.

Return valid JSON:
{ "what_regime_means": "...", "structure_guidance": "...", "stress_components": "...", "desk_takeaway": "..." }""",

    "e5_vol": """You are a senior volatility strategist at a proprietary options desk.

Given the Engine 5 Vol Lead-Lag data (global vol score, direction, US IV state, vol lag state,
structure bias, strike width multiplier, vol size multiplier, component z-scores), explain:

1. WHAT VOL IS TELLING US — Is vol leading or lagging the move? Is risk underpriced or overpriced?
2. STRUCTURE IMPACT — How does the vol lag state affect which option structures the desk should favor?
3. SIZING IMPLICATIONS — What do the strike width and vol size multipliers mean for position construction?
4. DESK TAKEAWAY — One sentence: what is vol telling the desk to do or not do right now?

Rules: Never recommend specific trades. Cite vol scores and states. Under 250 words.

Return valid JSON:
{ "what_vol_tells_us": "...", "structure_impact": "...", "sizing_implications": "...", "desk_takeaway": "..." }""",

    "e5_narrative": """You are a senior global macro strategist at a proprietary options desk.

Given the Engine 5 Global Signal Summary (dominant theme, leaders active, leaders confirming,
narrative text) and the current regime context, explain:

1. WHAT THE NARRATIVE MEANS — What is the dominant global theme and what is it signaling for US equities?
2. LEADERSHIP READ — What does the leader count (active vs confirming) tell us about conviction?
3. CROSS-MARKET CONTEXT — How do the global signals tie into the regime and vol state?
4. DESK TAKEAWAY — One sentence: what is the global signal telling the desk about positioning this week?

Rules: Never recommend specific trades. Cite the narrative and leadership counts. Under 200 words.

Return valid JSON:
{ "what_narrative_means": "...", "leadership_read": "...", "cross_market_context": "...", "desk_takeaway": "..." }""",

    "e5_index_bias": """You are a senior index strategist at a proprietary options desk.

Given a single index bias reading (index symbol, direction, confidence, note) from Engine 5's
global lead-lag analysis, and the broader regime context, explain:

1. WHAT THIS INDEX BIAS MEANS — What is the lead-lag system seeing for this index and why?
2. CONFIDENCE READ — How strong is this signal? What does the confidence level imply for sizing?
3. REGIME ALIGNMENT — Does this index bias confirm or diverge from the broader regime?
4. DESK TAKEAWAY — One sentence: how should the desk think about this index bias for the week?

Rules: Never recommend specific trades. Cite the direction and confidence. Under 180 words.

Return valid JSON:
{ "what_bias_means": "...", "confidence_read": "...", "regime_alignment": "...", "desk_takeaway": "..." }""",

    "e5_sector_bias": """You are a senior sector rotation analyst at a proprietary options desk.

Given a single sector bias (sector ETF, name, direction, confidence, vol bias, sources) from
Engine 5's global lead-lag analysis, and the broader regime context, explain:

1. WHAT THIS SECTOR SIGNAL MEANS — What are the global lead-lag signals telling us about this sector?
2. VOL BIAS IMPACT — How does the vol bias for this sector affect structure selection?
3. SOURCE ANALYSIS — What do the signal sources tell us about the quality and persistence of this bias?
4. DESK TAKEAWAY — One sentence: how should the desk think about this sector for the week?

Rules: Never recommend specific trades. Cite the sources and confidence. Under 200 words.

Return valid JSON:
{ "what_sector_means": "...", "vol_bias_impact": "...", "source_analysis": "...", "desk_takeaway": "..." }""",

    "e5_trade_idea": """You are a senior options strategist at a proprietary desk reviewing a model-generated trade idea.

Given a trade idea from Engine 5 (symbol, structure, directional lean, confidence, regime context,
source driver, IV rank, expected move, invalidation status, invalidation rules, vol adjustments),
explain to the desk:

1. IDEA THESIS — What is the lead-lag system seeing that generated this idea? What is the thesis?
2. STRUCTURE RATIONALE — Why this structure type? How does it fit the regime and vol environment?
3. RISK MANAGEMENT — What are the invalidation levels and rules? When should this idea be abandoned?
4. DESK TAKEAWAY — One sentence: is this idea worth desk attention and what would confirm or kill it?

Rules: These are MODEL SUGGESTIONS, never confirmed orders. Say "model suggests" not "you should".
Cite the confidence, invalidation status, and source driver. Under 250 words.

Return valid JSON:
{ "idea_thesis": "...", "structure_rationale": "...", "risk_management": "...", "desk_takeaway": "..." }""",

    "e5_triggers": """You are a senior regime transition analyst at a proprietary options desk.

Given the Engine 5 Regime Transition Triggers (top drivers with values, flip-up conditions,
flip-down conditions, proximity flags, boundary distances), explain:

1. WHERE WE ARE — What are the top drivers of the current regime and how close are we to a flip?
2. WHAT WOULD FLIP UP — What conditions would push us to a more risk-on regime? How likely?
3. WHAT WOULD FLIP DOWN — What conditions would push us to a more stressed regime? How likely?
4. DESK TAKEAWAY — One sentence: how should the desk prepare for a potential regime transition?

Rules: Never recommend specific trades. Cite the boundary distances and proximity flags. Under 250 words.

Return valid JSON:
{ "where_we_are": "...", "what_flips_up": "...", "what_flips_down": "...", "desk_takeaway": "..." }""",

    "e5_component": """You are a senior cross-asset stress analyst at a proprietary options desk.

Given a single regime stress component (name and score — one of FX Stress, Yield Stress,
Commodity Stress, or IV Stress) from Engine 5, and the broader regime context, explain:

1. WHAT THIS STRESS READING MEANS — What does this score tell us about conditions in this asset class?
2. EQUITY TRANSMISSION — How does stress in this asset class historically transmit to US equities and options?
3. RELATIVE CONTEXT — Is this reading elevated, normal, or low relative to what we typically see?
4. DESK TAKEAWAY — One sentence: what should the desk watch for in this asset class?

Rules: Never recommend specific trades. Cite the score. Under 180 words.

Return valid JSON:
{ "what_stress_means": "...", "equity_transmission": "...", "relative_context": "...", "desk_takeaway": "..." }""",
}

_CARD_INSIGHT_KEYS: Dict[str, set] = {
    "composite": {"what_its_telling_us", "key_drivers", "historical_context", "desk_takeaway"},
    "theme": {"what_this_theme_means", "market_impact", "momentum_read", "desk_takeaway"},
    "regime": {"what_regime_tells_us", "engine_implications", "regime_context", "desk_takeaway"},
    "flow": {"what_flow_tells_us", "flow_vs_regime", "context", "desk_takeaway"},
    "asymmetry": {"what_this_means", "why_it_matters", "what_to_watch", "desk_takeaway"},
    "diff": {"what_changed", "significance", "cascading_effects", "desk_takeaway"},
    # Engine 5 card types
    "e5_regime": {"what_regime_means", "structure_guidance", "stress_components", "desk_takeaway"},
    "e5_vol": {"what_vol_tells_us", "structure_impact", "sizing_implications", "desk_takeaway"},
    "e5_narrative": {"what_narrative_means", "leadership_read", "cross_market_context", "desk_takeaway"},
    "e5_index_bias": {"what_bias_means", "confidence_read", "regime_alignment", "desk_takeaway"},
    "e5_sector_bias": {"what_sector_means", "vol_bias_impact", "source_analysis", "desk_takeaway"},
    "e5_trade_idea": {"idea_thesis", "structure_rationale", "risk_management", "desk_takeaway"},
    "e5_triggers": {"where_we_are", "what_flips_up", "what_flips_down", "desk_takeaway"},
    "e5_component": {"what_stress_means", "equity_transmission", "relative_context", "desk_takeaway"},
}


def generate_card_insight(
    card_type: str,
    card_data: dict,
    dms_summary: dict,
) -> Dict[str, Any]:
    """Generate a desk-level LLM insight for any card type.

    Supports Market Intelligence cards (composite, theme, regime, flow, asymmetry,
    diff) and Engine 5 Lead-Lag cards (e5_regime, e5_vol, e5_narrative,
    e5_index_bias, e5_sector_bias, e5_trade_idea, e5_triggers, e5_component).

    Args:
        card_type:   Card type identifier (see _CARD_INSIGHT_PROMPTS keys).
        card_data:   The specific data for this card.
        dms_summary: Condensed DailyMarketState or E5 context dict.

    Returns:
        Dict with insight sections + _source tag.
    """
    required_keys = _CARD_INSIGHT_KEYS.get(card_type, set())
    system_prompt = _CARD_INSIGHT_PROMPTS.get(card_type)

    fallback: Dict[str, Any] = {k: "Insight unavailable." for k in required_keys}
    fallback["_source"] = "fallback"
    fallback["_card_type"] = card_type

    if not system_prompt:
        fallback["_fallback_reason"] = f"Unknown card type: {card_type}"
        return fallback

    if not _rate_limiter.acquire():
        LOG.info("Card insight rate-limited for %s", card_type)
        fallback["_fallback_reason"] = "Rate limited (max 4 calls/minute). Wait a moment and try again."
        return fallback

    client = _get_openai_client()
    if client is None:
        fallback["_fallback_reason"] = "OpenAI client unavailable"
        return fallback

    # Build compact context
    context = {
        "card": card_data,
        "market": {
            "regime": dms_summary.get("regime", {}),
            "flow_pressure": dms_summary.get("flow_pressure", {}),
            "vol_state": dms_summary.get("vol_state", {}),
            "composite_stress": dms_summary.get("cross_asset_stress", {}).get("composite_score"),
            "composite_label": dms_summary.get("cross_asset_stress", {}).get("composite_label"),
            "active_themes": [
                {"theme": t.get("theme"), "intensity": t.get("intensity"), "acceleration": t.get("acceleration")}
                for t in dms_summary.get("news_themes", [])
                if float(t.get("intensity", 0)) > 10
            ],
        },
    }

    payload_str = json.dumps(context, default=str)
    model = os.getenv("LLM_MODEL_NARRATIVE", "gpt-4o-mini").strip()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_str},
            ],
            temperature=0.3,
            max_tokens=400,
            timeout=12,
        )

        content = response.choices[0].message.content.strip()
        result = _parse_llm_json(content)

        if result is None or not required_keys.issubset(set(result.keys())):
            LOG.warning("Card insight (%s) LLM response missing required keys", card_type)
            fallback["_fallback_reason"] = "LLM returned invalid JSON"
            return fallback

        insight: Dict[str, Any] = {}
        for key in required_keys:
            val = result.get(key, "")
            insight[key] = str(val)[:500]

        insight["_source"] = "llm"
        insight["_card_type"] = card_type
        return insight

    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        LOG.warning("Card insight (%s) LLM call failed: %s", card_type, reason)
        fallback["_fallback_reason"] = reason
        return fallback


# ---------------------------------------------------------------------------
# Asymmetry Radar (deterministic – NOT LLM)
# ---------------------------------------------------------------------------


def detect_asymmetries(
    dms_today: dict,
    dms_history: Optional[List[dict]] = None,
) -> List[Dict[str, Any]]:
    """Detect rare high-impact asymmetric conditions.

    Pure deterministic logic – no LLM involved.
    Each alert tagged: "Monitor only / Await confirmation / No action yet"

    Conditions checked:
      1. Vol underpricing vs narrative acceleration
      2. FX stress without equity reaction
      3. Commodity spike with muted index response
      4. Regime-flow divergence
      5. Theme persistence without vol reaction
    """
    signals: List[Dict[str, Any]] = []

    if not dms_today:
        return signals

    regime = dms_today.get("regime", {})
    flow = dms_today.get("flow_pressure", {})
    vol = dms_today.get("vol_state", {})
    xstress = dms_today.get("cross_asset_stress", {})
    themes = dms_today.get("news_themes", [])
    regime_score = float(regime.get("score", 50))
    fp_score = float(flow.get("score", 50))
    vol_level = float(vol.get("level", 0))
    vol_skew = str(vol.get("skew", "neutral"))

    xstress_score = float(xstress.get("composite_score", 50))
    xstress_readings = xstress.get("readings", [])

    # --- 1. Vol underpricing vs narrative acceleration ---
    high_intensity_themes = [
        t for t in themes
        if float(t.get("intensity", 0)) > 60
        and str(t.get("acceleration", "")) == "rising"
    ]
    if high_intensity_themes and vol_skew != "elevated" and vol_level < 25:
        signals.append({
            "type": "vol_underpricing_vs_narrative",
            "description": (
                f"Narrative themes accelerating ({len(high_intensity_themes)} themes rising) "
                f"but vol skew is {vol_skew} and VIX-level proxy is {vol_level:.0f}. "
                "Vol may be underpricing tail risk."
            ),
            "severity": "elevated",
            "action": "Monitor only. Await confirmation from vol term structure.",
            "sources": ["news_themes", "vol_state.skew", "vol_state.level"],
        })

    # --- 2. FX stress without equity reaction ---
    fx_readings = [r for r in xstress_readings if r.get("asset_class") == "fx"]
    fx_stress_avg = 0.0
    if fx_readings:
        fx_stress_avg = sum(float(r.get("stress_score", 50)) for r in fx_readings) / len(fx_readings)

    if fx_stress_avg > 65 and fp_score > 45:
        signals.append({
            "type": "fx_stress_no_equity_reaction",
            "description": (
                f"FX stress elevated ({fx_stress_avg:.0f}) but flow pressure remains "
                f"neutral-to-positive ({fp_score:.0f}). Equities may be ignoring "
                "funding currency stress."
            ),
            "severity": "watch",
            "action": "Monitor only. FX stress may lead equities by 1-2 sessions.",
            "sources": ["cross_asset_stress.fx", "flow_pressure.score"],
        })

    # --- 3. Commodity spike with muted index response ---
    commodity_readings = [r for r in xstress_readings if r.get("asset_class") == "commodity"]
    commodity_stress_avg = 0.0
    if commodity_readings:
        commodity_stress_avg = sum(
            float(r.get("stress_score", 50)) for r in commodity_readings
        ) / len(commodity_readings)

    if commodity_stress_avg > 65 and regime_score < 50:
        signals.append({
            "type": "commodity_spike_muted_index",
            "description": (
                f"Commodity stress elevated ({commodity_stress_avg:.0f}) but regime score "
                f"remains moderate ({regime_score:.0f}). Supply-side or geopolitical "
                "risk may not yet be reflected in equities."
            ),
            "severity": "watch",
            "action": "Await confirmation. No action yet.",
            "sources": ["cross_asset_stress.commodity", "regime.score"],
        })

    # --- 4. Regime-flow divergence ---
    regime_label = str(regime.get("state", ""))
    fp_label = str(flow.get("state", ""))

    if regime_label in ("Risk-Off", "Stressed") and fp_label == "Risk-On":
        signals.append({
            "type": "regime_flow_divergence",
            "description": (
                f"Regime is {regime_label} (score {regime_score:.0f}) but flow pressure "
                f"reads {fp_label} ({fp_score:.0f}). Internal divergence may resolve "
                "sharply in either direction."
            ),
            "severity": "elevated",
            "action": "Monitor only. Divergence typically resolves within 1-3 sessions.",
            "sources": ["regime.state", "flow_pressure.state"],
        })

    # --- 5. Theme persistence without vol reaction ---
    persistent_themes = [
        t for t in themes
        if int(t.get("persistence_days", 0)) >= 5
        and float(t.get("intensity", 0)) > 40
    ]
    if persistent_themes and vol_skew == "low":
        signals.append({
            "type": "persistent_theme_no_vol",
            "description": (
                f"{len(persistent_themes)} theme(s) have been active for 5+ days "
                f"but vol skew is low. Market may be complacent."
            ),
            "severity": "watch",
            "action": "Monitor only. Await confirmation from vol term structure.",
            "sources": ["news_themes.persistence_days", "vol_state.skew"],
        })

    return signals


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_dms(dms: dict) -> dict:
    """Remove any fields that should not reach the LLM (raw prices, P&L, etc).

    The DailyMarketState should already be clean, but this is a defense-in-depth check.
    """
    if not isinstance(dms, dict):
        return {}

    # Whitelist of allowed top-level fields
    allowed = {
        "date", "generated_at", "regime", "flow_pressure", "vol_state",
        "engine_gates", "earnings_candidates", "index_state", "news_risk",
        "cross_asset_stress", "news_themes", "sequencer_summary",
        "asymmetry_signals",
    }
    sanitized = {k: v for k, v in dms.items() if k in allowed}

    # Strip any raw_price or pnl fields that might leak through
    return _recursive_strip(sanitized, {"raw_price", "price", "pnl", "profit", "loss", "close", "open", "high", "low"})


def _recursive_strip(obj: Any, forbidden_keys: set) -> Any:
    """Recursively remove forbidden keys from nested dicts."""
    if isinstance(obj, dict):
        return {
            k: _recursive_strip(v, forbidden_keys)
            for k, v in obj.items()
            if k.lower() not in forbidden_keys
        }
    elif isinstance(obj, list):
        return [_recursive_strip(item, forbidden_keys) for item in obj]
    return obj


def _add_timestamp(result: dict) -> dict:
    """Add generation timestamp to LLM output."""
    result = dict(result)
    result["_generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")
    return result
