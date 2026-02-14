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

    def __init__(self, max_calls_per_minute: int = 12):
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
    """Parse LLM response with robust fallback for GPT-5.2 verbosity.

    Handles:
    - Raw JSON
    - JSON wrapped in markdown fences (```json ... ```)
    - JSON preceded by preamble text ("Here is the analysis:\\n{...}")
    - JSON followed by trailing commentary
    """
    raw = content  # keep original for debug logging
    content = content.strip()

    # Strip markdown code fences
    if content.startswith("```"):
        lines = content.split("\n")
        # Remove first line (```json) and last line (```)
        content = "\n".join(lines[1:])
        if content.rstrip().endswith("```"):
            content = content.rstrip()[:-3]
        content = content.strip()

    # Attempt direct parse first
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass

    # Fallback: extract first {...} block via brace-matching
    start = content.find("{")
    if start == -1:
        LOG.warning("LLM returned no JSON object; raw (first 300 chars): %s", raw[:300])
        return None

    depth = 0
    end = start
    for i in range(start, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    if depth != 0:
        LOG.warning("LLM JSON brace mismatch; raw (first 300 chars): %s", raw[:300])
        return None

    try:
        return json.loads(content[start:end])
    except json.JSONDecodeError:
        LOG.warning("LLM JSON extraction failed; raw (first 300 chars): %s", raw[:300])
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
    # Truncate to fit token budget (GPT-5.2 400K context allows more data)
    if len(payload_str) > 30000:
        payload_str = payload_str[:30000] + "..."

    model = os.getenv("LLM_MODEL_NARRATIVE", "gpt-4o-mini").strip()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_str},
            ],
            temperature=0.3,
            max_completion_tokens=1500,
            timeout=45,
            response_format={"type": "json_object"},
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
                brief[key] = str(val)[:800]

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
    if len(payload_str) > 30000:
        payload_str = payload_str[:30000] + "..."

    model = os.getenv("LLM_MODEL_NARRATIVE", "gpt-4o-mini").strip()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_str},
            ],
            temperature=0.3,
            max_completion_tokens=2000,
            timeout=45,
            response_format={"type": "json_object"},
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
                roadmap[key] = str(val)[:800]

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
            temperature=0.4,
            max_completion_tokens=800,
            timeout=30,
            response_format={"type": "json_object"},
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
            insight[key] = str(val)[:800]

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

    # ── Engine 1: Breach / Earnings Hold Risk ──────────────────────────

    "e1_decision": """You are a senior earnings-event options strategist at a proprietary desk.

Given the Engine 1 GO/NO-GO decision panel for a specific ticker — including the breach rate,
breach multiple (k), go/no-go status, all individual checks (each with state pass/warn/fail,
label, and explanation), and warnings — explain to the desk:

1. DECISION SUMMARY — What is the GO/NO-GO verdict and what are the most critical checks driving it?
2. KEY RISKS — Which failed or warned checks should the desk pay the most attention to? Why?
3. WHAT TO WATCH — If this is a GO, what could change the picture? If NO-GO, what would need to improve?
4. EXECUTION GUIDANCE — How should the desk approach this ticker given the check results?
5. DESK TAKEAWAY — One sentence: trade it, skip it, or wait — and why?

Rules: Never say "buy" or "sell." Frame as risk management. Cite specific check names and values. Under 300 words.

Return valid JSON:
{ "decision_summary": "...", "key_risks": "...", "what_to_watch": "...", "execution_guidance": "...", "desk_takeaway": "..." }""",

    "e1_hold_risk": """You are a senior earnings-event options strategist at a proprietary desk.

Given the Earnings Hold Risk data for a specific ticker — unconditional breach rates at 1.0×, 1.5×,
2.0× EM for both earnings close and next-day close, conditional (flat open) breach rates, max observed
deviations, post-event drift data, and sample sizes — explain:

1. HOLD RISK ASSESSMENT — How dangerous is it to hold through this earnings event? What do the breach rates tell us?
2. CONDITIONAL VS UNCONDITIONAL — How different are the flat-open rates from unconditional? What does that divergence mean?
3. DRIFT ANALYSIS — What does the post-event drift tell us about intraday vs next-day risk? Where is the real risk?
4. STRUCTURE IMPLICATIONS — How should these numbers influence strike selection, wing width, and timing (close before or hold)?
5. DESK TAKEAWAY — One sentence: is this a hold-through or a close-before-event situation?

Rules: Cite the actual breach percentages and sample sizes. Under 300 words.

Return valid JSON:
{ "hold_risk_assessment": "...", "conditional_vs_unconditional": "...", "drift_analysis": "...", "structure_implications": "...", "desk_takeaway": "..." }""",

    "e1_monte_carlo": """You are a senior quantitative strategist at a proprietary options desk.

Given the Monte Carlo simulation results for an earnings event — breach probability (either, put, call),
expected loss at open, CVaR95, number of simulations, conditioning used, pool size, wing optimization
result, and diagnostic notes — explain:

1. WHAT THE SIMULATION SAYS — What is the Monte Carlo telling us about this earnings event that historical rates alone cannot?
2. PUT VS CALL SKEW — Is the breach risk symmetric or skewed? What does the put/call breakdown tell the desk?
3. TAIL RISK — What does CVaR95 vs expected loss tell us? How fat are the tails on this name?
4. WING OPTIMIZATION — If wing optimization was run, what did it find? How should it influence strike selection?
5. DESK TAKEAWAY — One sentence: what is the Monte Carlo's strongest signal for the desk?

Rules: Cite the probability percentages, expected loss, and CVaR values. Under 280 words.

Return valid JSON:
{ "what_simulation_says": "...", "put_vs_call_skew": "...", "tail_risk": "...", "wing_optimization": "...", "desk_takeaway": "..." }""",

    "e1_regime": """You are a senior regime analyst at a proprietary options desk focused on earnings events.

Given the regime overlay for an Engine 1 run — regime label, trade gate (OPEN/RESTRICT/CLOSED),
tail multiplier, regime score, IV percentile, and guidance message — explain:

1. REGIME READ — What does the current regime mean for earnings trades specifically?
2. GATE IMPLICATIONS — How does the trade gate status affect what the desk can do?
3. TAIL MULTIPLIER — What does the tail multiplier imply for strike placement and sizing?
4. DESK TAKEAWAY — One sentence: how does the regime shape this earnings trade?

Rules: Cite the regime label, gate status, and multiplier. Under 200 words.

Return valid JSON:
{ "regime_read": "...", "gate_implications": "...", "tail_multiplier_impact": "...", "desk_takeaway": "..." }""",

    "e1_skew_wings": """You are a senior volatility and skew analyst at a proprietary options desk.

Given the skew overlay and wing recommendation data — skew quality (rich/fair/cheap), risk reversal
(RR25), wing recommendation (structure mode, put/call multiples, confidence, rationale), and directional
tail stats — explain:

1. SKEW READ — What is the skew telling us about how the market is pricing directional risk for this event?
2. WING RECOMMENDATION — Why is the model recommending these specific put/call wing multiples? Is the asymmetry justified?
3. DIRECTIONAL RISK — What do the tail stats say about up vs down overshoot risk? How should the desk lean?
4. STRUCTURE SELECTION — Should the desk use symmetric or asymmetric wings? Iron condor or vertical?
5. DESK TAKEAWAY — One sentence: what is the skew and wing data telling the desk to build?

Rules: Cite the skew quality, RR25, and wing multiples. Under 280 words.

Return valid JSON:
{ "skew_read": "...", "wing_recommendation": "...", "directional_risk": "...", "structure_selection": "...", "desk_takeaway": "..." }""",

    "e1_event_risk": """You are a senior event risk analyst at a proprietary options desk.

Given the Event Risk assessment — composite score (0-100), label (LOW/MODERATE/HIGH/EXTREME), top
drivers (with names and values), impact on this run, and explanatory notes — explain:

1. EVENT RISK LEVEL — How elevated is the event risk and what does this score mean for the desk?
2. TOP DRIVERS — Which factors are driving the event risk score? What do they tell us about the environment?
3. IMPACT ON TRADE — How does the event risk change the trade setup, sizing, or timing?
4. DESK TAKEAWAY — One sentence: does the event risk warrant adjustments to the standard approach?

Rules: Cite the score, label, and top driver names. Under 220 words.

Return valid JSON:
{ "event_risk_level": "...", "top_drivers": "...", "impact_on_trade": "...", "desk_takeaway": "..." }""",

    "e1_gamma_context": """You are a senior gamma and dealer positioning analyst at a proprietary desk.

Given the Earnings Gamma Context for a ticker — dealer gamma (net sign, magnitude, band), tail ignition
scores (up/down, with air pocket data), spot price, implied move, and any warnings — explain:

1. DEALER POSITIONING — Are dealers long or short gamma around this earnings? What does that mean for the stock?
2. TAIL IGNITION RISK — What do the up/down tail ignition scores tell us? Is there an air pocket that could accelerate a move?
3. GAMMA-EARNINGS INTERACTION — How does dealer gamma around the event date interact with the earnings gap risk?
4. DESK TAKEAWAY — One sentence: does gamma context make this event more or less dangerous?

Rules: Cite the gamma sign, magnitude, tail scores. Under 250 words.

Return valid JSON:
{ "dealer_positioning": "...", "tail_ignition_risk": "...", "gamma_earnings_interaction": "...", "desk_takeaway": "..." }""",

    "e1_quarter": """You are a senior earnings seasonality analyst at a proprietary options desk.

Given the quarter seasonality data for a ticker — per-quarter breach rates, near-breach rates (0.8×,
0.9×), realized/implied ratio, max ratio, up/down breach rates, seasonality deltas vs baseline
(z-score, breach delta, overshoot delta), and recommendation — explain:

1. SEASONAL PATTERN — Which quarters are historically more or less dangerous for this ticker? Is there a clear pattern?
2. CURRENT QUARTER — How does the current quarter compare to the baseline? Is the desk in a high-risk or low-risk seasonal window?
3. STATISTICAL SIGNIFICANCE — Are the seasonal deviations large enough to matter, or is the sample too small to trust?
4. DESK TAKEAWAY — One sentence: should seasonality change the desk's sizing or approach this quarter?

Rules: Cite breach rates, z-scores, and sample sizes. Under 250 words.

Return valid JSON:
{ "seasonal_pattern": "...", "current_quarter": "...", "statistical_significance": "...", "desk_takeaway": "..." }""",

    "e1_strike_targets": """You are a senior strike selection analyst at a proprietary options desk.

Given the strike/buffer target data — symmetric and asymmetric modes, put/call strike distances at 1.0×,
1.5×, 2.0× EM, the underlying price, implied move, and regime tail multiplier — explain:

1. STRIKE MAP — What do the strike target levels mean? How far from the current price are the key risk boundaries?
2. SYMMETRIC VS ASYMMETRIC — When should the desk use symmetric vs asymmetric targets? What does the current setup favor?
3. TAIL MULTIPLIER EFFECT — How does the regime tail multiplier shift these targets? Is the desk being forced wider?
4. DESK TAKEAWAY — One sentence: where should the desk place strikes for this event?

Rules: Cite the strike levels and EM percentages. Under 220 words.

Return valid JSON:
{ "strike_map": "...", "symmetric_vs_asymmetric": "...", "tail_multiplier_effect": "...", "desk_takeaway": "..." }""",

    "e1_dealer_gamma": """You are a senior dealer gamma and OI analyst at a proprietary options desk.

Given dealer gamma data (market or ticker level) — net gamma sign (+/-), magnitude bucket (LOW/MED/HIGH),
band percentage, OI clusters (put wall, call wall, put/call cluster details), and any warnings — explain:

1. DEALER POSITIONING — What does the net gamma sign and magnitude tell us about market maker hedging behavior?
2. OI CLUSTERS — Where are the major put and call walls? What do these levels mean for support/resistance?
3. IMPLICATIONS FOR THE TRADE — How does dealer gamma and OI positioning affect the likely price behavior around this event?
4. DESK TAKEAWAY — One sentence: what should the desk know about gamma positioning for this setup?

Rules: Cite the gamma sign, magnitude, and key OI levels. Under 250 words.

Return valid JSON:
{ "dealer_positioning": "...", "oi_clusters": "...", "trade_implications": "...", "desk_takeaway": "..." }""",

    # ── Engine 2: SPX Iron Condor Scanner ──────────────────────────────

    "e2_regime": """You are a senior index options strategist at a proprietary condor desk.

Given the Engine 2 regime score — score100 (0-100), bucket (LOW/MODERATE/ELEVATED/NO_TRADE), label,
and component breakdown (trend, volatility, stress, event, dispersion) — explain:

1. REGIME READ — What is the current regime telling the condor desk? Is this a good environment for premium selling?
2. COMPONENT BREAKDOWN — Which components are driving the score? What does each component mean for condor risk?
3. BUCKET IMPLICATIONS — What does the bucket (LOW/MODERATE/ELEVATED/NO_TRADE) mean for sizing and structure?
4. WHAT WOULD CHANGE — What would push this regime to the next bucket up or down?
5. DESK TAKEAWAY — One sentence: is this a sell-premium or protect-capital environment?

Rules: Cite the score, bucket, and component values. Under 280 words.

Return valid JSON:
{ "regime_read": "...", "component_breakdown": "...", "bucket_implications": "...", "what_would_change": "...", "desk_takeaway": "..." }""",

    "e2_macro": """You are a senior macro-event analyst at a proprietary condor desk.

Given the Engine 2 macro overlay — multiplier value, flags (CPI, FOMC, NFP, OPEX, REFUNDING),
high-impact US event count and list — explain:

1. MACRO RISK LEVEL — How elevated is macro risk this week? What does the multiplier mean for the trade?
2. KEY EVENTS — Which macro events are most dangerous for condors? How should the desk sequence around them?
3. MULTIPLIER EFFECT — How does the macro multiplier change width selection and sizing vs a normal week?
4. DESK TAKEAWAY — One sentence: does macro risk warrant wider wings, smaller size, or skipping the week?

Rules: Cite the multiplier value and specific events. Under 220 words.

Return valid JSON:
{ "macro_risk_level": "...", "key_events": "...", "multiplier_effect": "...", "desk_takeaway": "..." }""",

    "e2_odds": """You are a senior quantitative analyst at a proprietary condor desk.

Given the Engine 2 historical odds data — breach rates by width (0.8×, 1.0×, 1.2× EM), number of
comparable weeks (N), regime/macro/season buckets used for conditioning, per-width breach
percentages (either, put, call), and average absolute return — explain:

1. PROBABILITY READ — What do the breach rates tell us about the likely outcome at each width?
2. WIDTH SELECTION — Which width gives the best risk/reward? Is the desk being compensated for going narrower?
3. CONDITIONING QUALITY — How many comparable weeks were used? Is the sample large enough to trust?
4. DIRECTIONAL SKEW — Is breach risk coming more from puts or calls? What does that tell the desk?
5. DESK TAKEAWAY — One sentence: which width should the desk trade and what breach rate should they expect?

Rules: Cite the breach percentages, N, and widths. Under 280 words.

Return valid JSON:
{ "probability_read": "...", "width_selection": "...", "conditioning_quality": "...", "directional_skew": "...", "desk_takeaway": "..." }""",

    "e2_dealer_gamma": """You are a senior gamma and dealer positioning analyst at a proprietary condor desk.

Given the Engine 2 dealer gamma data — net gamma sign (+/-), magnitude bucket, top gamma strikes
(with side and GEX values), OI clusters (put wall, call wall, clusters), gamma flip strike, and
spot price — explain:

1. DEALER REGIME — Are dealers long or short gamma? What does that mean for expected price behavior this week?
2. KEY LEVELS — Where are the put/call walls and gamma flip? How do these relate to the condor strike selection?
3. GAMMA PEAKS — Which strikes have the most gamma exposure? How could these levels pin or repel price?
4. CONDOR POSITIONING — How should dealer gamma influence where the desk places short strikes?
5. DESK TAKEAWAY — One sentence: is dealer positioning supportive or threatening for the condor setup?

Rules: Cite gamma sign, wall strikes, and flip level. Under 280 words.

Return valid JSON:
{ "dealer_regime": "...", "key_levels": "...", "gamma_peaks": "...", "condor_positioning": "...", "desk_takeaway": "..." }""",

    "e2_gex": """You are a senior gamma exposure analyst at a proprietary condor desk.

Given the GEX heatmap data — stability label (Stable/Asymmetric/Fragile), downside/upside gamma-flip
distances (in points and EM multiples), net GEX distribution shape, and the current view mode — explain:

1. STABILITY READ — What does the stability label tell the desk about gamma structure this week?
2. FLIP DISTANCES — How far are the gamma flips from spot? Are they inside or outside the condor wings?
3. RISK ASYMMETRY — Is gamma exposure symmetric or tilted? Which direction has more acceleration risk?
4. CONDOR IMPLICATIONS — How does the GEX landscape affect the condor's risk profile for the week?
5. DESK TAKEAWAY — One sentence: does the GEX picture favor the condor or warn against it?

Rules: Cite flip distances, EM multiples, and stability. Under 260 words.

Return valid JSON:
{ "stability_read": "...", "flip_distances": "...", "risk_asymmetry": "...", "condor_implications": "...", "desk_takeaway": "..." }""",

    "e2_hedging_pressure": """You are a senior flow and hedging analyst at a proprietary condor desk.

Given the Hedging Pressure Index (HPI) data — gamma total, strikes used, elasticity at 50bp,
elasticity bucket (LOW/MED/HIGH), hedging scenarios (25bp, 50bp, 100bp with delta, shares, and
notional), ADV data — explain:

1. HEDGING FLOW READ — How much hedging flow could a 50bp move generate? Is that enough to move the market?
2. ELASTICITY — What does the elasticity bucket mean for price stability? HIGH elasticity means what for the condor?
3. SCENARIO ANALYSIS — Walk the desk through the hedging scenarios: what happens at 25bp, 50bp, 100bp moves?
4. DESK TAKEAWAY — One sentence: should hedging pressure change the desk's width or sizing?

Rules: Cite the elasticity bucket, gamma total, and scenario notionals. Under 250 words.

Return valid JSON:
{ "hedging_flow_read": "...", "elasticity_analysis": "...", "scenario_walkthrough": "...", "desk_takeaway": "..." }""",

    "e2_tail_ignition": """You are a senior tail risk analyst at a proprietary condor desk.

Given the Tail Ignition data — down score (0-100), up score (0-100), labels (LOW/MED/HIGH),
air pocket density ratios, distances to put wall, call wall, and gamma flip (as percentages),
GEX slope — explain:

1. TAIL RISK MAP — What are the tail ignition scores saying about downside vs upside acceleration risk?
2. AIR POCKETS — Are there air pockets (low-gamma zones) that could accelerate a move through the condor strikes?
3. WALL DISTANCES — How far is spot from the put/call walls? If spot reaches a wall, what happens?
4. IMPLICATIONS FOR CONDOR — Does the tail ignition picture change where the desk should place wings?
5. DESK TAKEAWAY — One sentence: which side has more tail risk and how should the desk adjust?

Rules: Cite the down/up scores and key distances. Under 260 words.

Return valid JSON:
{ "tail_risk_map": "...", "air_pockets": "...", "wall_distances": "...", "condor_implications": "...", "desk_takeaway": "..." }""",

    "e2_vol_pressure": """You are a senior volatility analyst at a proprietary condor desk.

Given the Vol Pressure data — state (BID/OFFERED/NEUTRAL), composite z-score, component z-scores
(dIv, dSkew, ivRv, term), and raw inputs (IV7, IV30, RV10, slope, term slope) — explain:

1. VOL STATE — Is vol being bid up or offered down? What does this mean for premium sellers?
2. Z-SCORE BREAKDOWN — Which components are driving the state? What does each z-score tell the desk?
3. IV VS RV — How does current implied vol compare to realized? Is the desk selling expensive or cheap premium?
4. TERM STRUCTURE — What is the term slope saying? Is it supporting or undermining the condor's edge?
5. DESK TAKEAWAY — One sentence: is the vol environment friend or foe for the condor this week?

Rules: Cite the state, composite z-score, and key component values. Under 260 words.

Return valid JSON:
{ "vol_state": "...", "z_score_breakdown": "...", "iv_vs_rv": "...", "term_structure": "...", "desk_takeaway": "..." }""",

    "e2_expected_move": """You are a senior index options strategist at a proprietary condor desk.

Given the Expected Move data — weekly EM percentage, EM in dollars, DTE, source (live/eod), spot
price, expiry, strike targets at 1.0×/1.5×/2.0× EM, and VWAP context (value, distance, side) — explain:

1. EXPECTED MOVE READ — What does the current EM imply about the market's pricing of weekly risk?
2. STRIKE TARGETS — How do the 1.0×/1.5×/2.0× EM targets map to actual strike levels? Which is the sweet spot?
3. VWAP CONTEXT — Where is price relative to VWAP? Does that create a directional bias for the condor?
4. EM TREND — Is the EM expanding or contracting vs recent weeks? What does that mean for credit received?
5. DESK TAKEAWAY — One sentence: what EM width and strike targets should the desk use this week?

Rules: Cite the EM percentage, strike levels, and VWAP distance. Under 250 words.

Return valid JSON:
{ "expected_move_read": "...", "strike_targets": "...", "vwap_context": "...", "em_trend": "...", "desk_takeaway": "..." }""",

    "e2_technicals": """You are a senior technical analyst at a proprietary condor desk.

Given the Engine 2 technicals panel — RSI (value, state, slope), MACD (value, signal, histogram, cross,
trend), Bollinger (bandwidth, %B, state, squeeze), EMA stack (8/21/50/100/200), narrative, signals,
and candle patterns — explain:

1. DIRECTIONAL READ — What is the technical picture saying about short-term direction? Is there a bias?
2. MOMENTUM — What do RSI and MACD jointly tell us? Are they confirming or diverging?
3. VOLATILITY CONTEXT — What does the Bollinger squeeze/bandwidth tell us about expected volatility expansion?
4. CONDOR RELEVANCE — How do technicals affect condor placement? Should the desk skew strikes based on this?
5. DESK TAKEAWAY — One sentence: do technicals favor a centered condor or a directionally skewed one?

Rules: Cite RSI value, MACD cross, Bollinger state. Under 260 words.

Return valid JSON:
{ "directional_read": "...", "momentum_analysis": "...", "volatility_context": "...", "condor_relevance": "...", "desk_takeaway": "..." }""",

    # ── Red Dog (Engine 3): Mean-Reversion Scanner ─────────────────────

    "rd_signal": """You are a senior mean-reversion trader at a proprietary desk reviewing a Red Dog setup.

Given a single Red Dog reversal signal — ticker, direction (bullish/bearish), quality score (0-100),
grade (A+/A/B/C), entry trigger, stop loss, target 1, target 2, risk in dollars, reward/risk ratio,
RSI, stochastics, SMA20 deviation %, volume ratio, ATR, close position within the pattern, trend
alignment status, gamma alignment, and gate status — explain:

1. SETUP QUALITY — What does the score and grade tell us? Which quality components are strongest and which are weakest?
2. ENTRY MECHANICS — Where is the entry trigger relative to the current price? How tight is the stop? Is the risk/reward attractive?
3. INDICATOR CONFLUENCE — Do RSI, stochastics, volume, and SMA20 deviation all confirm the setup? Where are the gaps?
4. ALIGNMENT CHECK — Is this signal aligned with the SPX trend and gamma environment, or is it a counter-trend play? What does that mean for conviction?
5. DESK TAKEAWAY — One sentence: is this a high-conviction setup the desk should act on, or watch-only?

Rules: These are MEAN-REVERSION setups — they fade extended moves. Cite the score, RSI, and risk/reward. Under 300 words.

Return valid JSON:
{ "setup_quality": "...", "entry_mechanics": "...", "indicator_confluence": "...", "alignment_check": "...", "desk_takeaway": "..." }""",

    "rd_gamma": """You are a senior gamma and market structure analyst advising a mean-reversion desk.

Given the Red Dog market gamma context — SPX net gamma sign (positive/negative), magnitude, environment
(supportive/challenging), recommendation text, explanation, GEX values (calls, puts, net), spot price,
expiry, and data source — explain:

1. GAMMA ENVIRONMENT — What does the current gamma regime mean for mean-reversion trades specifically?
2. DIRECTIONAL BIAS — Does positive or negative gamma favor bullish or bearish Red Dog setups?
3. MEAN-REVERSION IMPACT — How does dealer gamma affect the likelihood of price snapping back to mean vs trending away?
4. DESK TAKEAWAY — One sentence: does the gamma environment support taking Red Dog setups today?

Rules: Frame everything through the lens of mean-reversion trading. Cite the gamma sign and environment. Under 220 words.

Return valid JSON:
{ "gamma_environment": "...", "directional_bias": "...", "mean_reversion_impact": "...", "desk_takeaway": "..." }""",

    "rd_trend": """You are a senior trend analyst advising a mean-reversion desk.

Given the Red Dog SPX Trend Filter — price relative to 21 EMA (above/below), distance percentage,
trend direction (bullish/bearish), favored reversal direction, recommendation, explanation, current
price, and EMA value — explain:

1. TREND READ — What is the SPX trend telling us about which direction of Red Dog setups to favor?
2. ALIGNMENT VALUE — How much does trend alignment matter for mean-reversion? What is the historical edge of with-trend vs counter-trend?
3. DISTANCE CONTEXT — What does the distance from the 21 EMA tell us about trend momentum and overextension?
4. DESK TAKEAWAY — One sentence: which direction should the desk prioritize and why?

Rules: Cite the EMA distance, trend direction, and price. Under 200 words.

Return valid JSON:
{ "trend_read": "...", "alignment_value": "...", "distance_context": "...", "desk_takeaway": "..." }""",

    "rd_scan_summary": """You are a senior portfolio scanner analyst at a proprietary mean-reversion desk.

Given the Red Dog scan summary — universe scanned, setups found, A+ setups count, scan date, scan
duration, and the top signals by score — explain:

1. SCAN READ — What does today's scan tell us about the breadth and quality of mean-reversion opportunities?
2. A+ CONCENTRATION — How many A+ setups vs total? Is this a target-rich or scarce environment?
3. DIRECTIONAL SKEW — Are the setups skewed bullish or bearish? What does that say about the market condition?
4. DESK TAKEAWAY — One sentence: is today a high-opportunity day or should the desk be selective?

Rules: Cite the counts and A+ percentage. Under 200 words.

Return valid JSON:
{ "scan_read": "...", "aplus_concentration": "...", "directional_skew": "...", "desk_takeaway": "..." }""",

    "rd_gate": """You are a senior risk manager at a proprietary mean-reversion desk.

Given the Red Dog gate context — gate summary (TRADABLE/WATCH/SUPPRESS counts), regime label,
vol direction, flow pressure — explain:

1. GATE STATUS — What is the gate telling the desk? How many signals are tradable vs suppressed?
2. REGIME IMPACT — How does the current regime affect mean-reversion setups? Risk-On vs Risk-Off implications?
3. VOL AND FLOW — What do vol direction and flow pressure mean for reversal probability?
4. DESK TAKEAWAY — One sentence: should the desk trade freely or apply extra caution today?

Rules: Cite the gate counts, regime label, and vol direction. Under 200 words.

Return valid JSON:
{ "gate_status": "...", "regime_impact": "...", "vol_and_flow": "...", "desk_takeaway": "..." }""",

    # ── Ichimoku (Engine 4): Trend-Continuation Scanner ────────────────

    "ik_signal": """You are a senior Ichimoku trend-continuation trader at a proprietary desk reviewing a setup.

Given a single Ichimoku signal — ticker, direction (bullish/bearish), status (pending/triggered),
quality score (0-100), grade (A+/A/B/C), Ichimoku values (Tenkan, Kijun, Chikou, Cloud top/bottom,
cloud bias, cloud thickness), pattern metrics (close position, pullback depth, cloud penetration),
entry trigger, stop loss, targets, risk dollars, reward/risk, RSI, volume ratio, ATR, Kijun slope,
freshness metrics (bars since reclaim, Kijun distance, recent Tenkan touch), tags, and penalties — explain:

1. ICHIMOKU STRUCTURE — What is the Tenkan/Kijun/Cloud alignment telling us? Is this a clean Ichimoku setup or are there conflicts?
2. ENTRY QUALITY — Where is the entry trigger relative to the cloud and Kijun? Is the pullback depth ideal for continuation?
3. FRESHNESS READ — Is this signal fresh (just reclaimed) or stale? What do the bars-since-reclaim and impulse data tell us?
4. RISK FRAMEWORK — How does the stop relate to the Kijun and cloud? Is the risk/reward worth the trade?
5. COMPONENT ANALYSIS — Which quality components scored highest? Where did penalties hit? What does the tag profile say?
6. DESK TAKEAWAY — One sentence: is this a high-conviction continuation the desk should act on?

Rules: These are TREND-CONTINUATION setups — they follow established Ichimoku trends. Cite the score, cloud bias, and Kijun slope. Under 320 words.

Return valid JSON:
{ "ichimoku_structure": "...", "entry_quality": "...", "freshness_read": "...", "risk_framework": "...", "component_analysis": "...", "desk_takeaway": "..." }""",

    "ik_gamma": """You are a senior gamma analyst advising an Ichimoku trend-continuation desk.

Given the Ichimoku market gamma context for both SPX and NDX — each with net gamma sign (positive/negative),
environment (supportive/challenging), recommendation text, GEX values, spot, and expiry — explain:

1. DUAL INDEX READ — What are SPX and NDX gamma telling us? Do they agree or diverge?
2. CONTINUATION IMPACT — How does dealer gamma affect the likelihood of trend continuation vs reversal?
3. INDEX MEMBERSHIP — How should SPX gamma matter for SP500 names and NDX gamma for Nasdaq names?
4. DESK TAKEAWAY — One sentence: does the gamma environment support taking Ichimoku continuation trades today?

Rules: Frame for trend-continuation, not mean-reversion. Cite both SPX and NDX gamma signs. Under 230 words.

Return valid JSON:
{ "dual_index_read": "...", "continuation_impact": "...", "index_membership": "...", "desk_takeaway": "..." }""",

    "ik_scan_summary": """You are a senior scanner analyst at a proprietary trend-continuation desk.

Given the Ichimoku scan summary — universe scanned, actionable count, structure (watchlist) count,
rejected count, scan date, direction filter applied — explain:

1. OPPORTUNITY READ — How many actionable signals vs watchlist? Is this a follow-through day or a setup-building day?
2. ACTIONABLE VS STRUCTURE — What is the difference between actionable and structure-only? What makes a signal cross from watch to act?
3. REJECTION RATE — How many were rejected and what does that tell us about overall market conditions for continuation?
4. DESK TAKEAWAY — One sentence: should the desk be executing or building the watchlist today?

Rules: Cite the actionable count and rejection rate. Under 200 words.

Return valid JSON:
{ "opportunity_read": "...", "actionable_vs_structure": "...", "rejection_rate": "...", "desk_takeaway": "..." }""",

    "ik_gate": """You are a senior risk manager at a proprietary trend-continuation desk.

Given the Ichimoku gate context — gate summary (TRADABLE/WATCH/SUPPRESS counts), regime label,
vol direction, flow pressure — explain:

1. GATE STATUS — How many signals are tradable vs suppressed? Is the gate broadly open or restrictive?
2. REGIME FOR CONTINUATION — Does the current regime favor trend-continuation? Which regimes are best/worst?
3. VOL DIRECTION — Rising or falling vol? How does that affect Ichimoku continuation trades?
4. DESK TAKEAWAY — One sentence: is the macro gate environment supportive for continuation trading today?

Rules: Cite the gate counts and regime label. Under 200 words.

Return valid JSON:
{ "gate_status": "...", "regime_for_continuation": "...", "vol_direction_impact": "...", "desk_takeaway": "..." }""",
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
    # Engine 1 card types
    "e1_decision": {"decision_summary", "key_risks", "what_to_watch", "execution_guidance", "desk_takeaway"},
    "e1_hold_risk": {"hold_risk_assessment", "conditional_vs_unconditional", "drift_analysis", "structure_implications", "desk_takeaway"},
    "e1_monte_carlo": {"what_simulation_says", "put_vs_call_skew", "tail_risk", "wing_optimization", "desk_takeaway"},
    "e1_regime": {"regime_read", "gate_implications", "tail_multiplier_impact", "desk_takeaway"},
    "e1_skew_wings": {"skew_read", "wing_recommendation", "directional_risk", "structure_selection", "desk_takeaway"},
    "e1_event_risk": {"event_risk_level", "top_drivers", "impact_on_trade", "desk_takeaway"},
    "e1_gamma_context": {"dealer_positioning", "tail_ignition_risk", "gamma_earnings_interaction", "desk_takeaway"},
    "e1_quarter": {"seasonal_pattern", "current_quarter", "statistical_significance", "desk_takeaway"},
    "e1_strike_targets": {"strike_map", "symmetric_vs_asymmetric", "tail_multiplier_effect", "desk_takeaway"},
    "e1_dealer_gamma": {"dealer_positioning", "oi_clusters", "trade_implications", "desk_takeaway"},
    # Engine 2 card types
    "e2_regime": {"regime_read", "component_breakdown", "bucket_implications", "what_would_change", "desk_takeaway"},
    "e2_macro": {"macro_risk_level", "key_events", "multiplier_effect", "desk_takeaway"},
    "e2_odds": {"probability_read", "width_selection", "conditioning_quality", "directional_skew", "desk_takeaway"},
    "e2_dealer_gamma": {"dealer_regime", "key_levels", "gamma_peaks", "condor_positioning", "desk_takeaway"},
    "e2_gex": {"stability_read", "flip_distances", "risk_asymmetry", "condor_implications", "desk_takeaway"},
    "e2_hedging_pressure": {"hedging_flow_read", "elasticity_analysis", "scenario_walkthrough", "desk_takeaway"},
    "e2_tail_ignition": {"tail_risk_map", "air_pockets", "wall_distances", "condor_implications", "desk_takeaway"},
    "e2_vol_pressure": {"vol_state", "z_score_breakdown", "iv_vs_rv", "term_structure", "desk_takeaway"},
    "e2_expected_move": {"expected_move_read", "strike_targets", "vwap_context", "em_trend", "desk_takeaway"},
    "e2_technicals": {"directional_read", "momentum_analysis", "volatility_context", "condor_relevance", "desk_takeaway"},
    # Red Dog (Engine 3) card types
    "rd_signal": {"setup_quality", "entry_mechanics", "indicator_confluence", "alignment_check", "desk_takeaway"},
    "rd_gamma": {"gamma_environment", "directional_bias", "mean_reversion_impact", "desk_takeaway"},
    "rd_trend": {"trend_read", "alignment_value", "distance_context", "desk_takeaway"},
    "rd_scan_summary": {"scan_read", "aplus_concentration", "directional_skew", "desk_takeaway"},
    "rd_gate": {"gate_status", "regime_impact", "vol_and_flow", "desk_takeaway"},
    # Ichimoku (Engine 4) card types
    "ik_signal": {"ichimoku_structure", "entry_quality", "freshness_read", "risk_framework", "component_analysis", "desk_takeaway"},
    "ik_gamma": {"dual_index_read", "continuation_impact", "index_membership", "desk_takeaway"},
    "ik_scan_summary": {"opportunity_read", "actionable_vs_structure", "rejection_rate", "desk_takeaway"},
    "ik_gate": {"gate_status", "regime_for_continuation", "vol_direction_impact", "desk_takeaway"},
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
            temperature=0.4,
            max_completion_tokens=800,
            timeout=30,
            response_format={"type": "json_object"},
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
            insight[key] = str(val)[:800]

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
