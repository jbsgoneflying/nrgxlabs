"""Engine 7 – Thematic Relative Value (Pairs) Engine: theme classification.

INV-1  Determinism guarantee
------
Layer 1 – ``classify_themes_deterministic`` is a **pure function** (keyword
rules, no external calls, no randomness).  This is the sole gating truth used
for eligibility and scoring decisions.

Layer 2 – ``annotate_themes_llm`` is an **optional enrichment** layer.  It
calls OpenAI with temperature=1, a fixed prompt, and sorted input.  Results
are persisted to Redis keyed by ``date + sha256(sorted_headlines + model)``.
Re-runs and backtests always replay from storage.  If OpenAI is unavailable
the annotation is ``None``; scoring and eligibility are unaffected.

INV-2  Theme-pair eligibility
------
``THEME_PAIR_ELIGIBILITY`` maps each theme to the pair_ids it validates and
the expected directional bias.  ``score_theme_alignment`` returns 0 when no
active theme covers a pair, which causes the screener to mark the signal
``NOT_ELIGIBLE``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Tuple

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class ThemeClassification:
    """One theme's deterministic classification for a given day."""
    theme: str = ""
    label: str = ""
    active: bool = False
    intensity: float = 0.0       # 0-100
    keyword_hits: int = 0
    sample_keywords: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ThemeResult:
    """Aggregation of all theme classifications for one scan."""
    date: str = ""
    themes: List[ThemeClassification] = field(default_factory=list)
    active_themes: List[str] = field(default_factory=list)
    headline_count: int = 0
    llm_annotation: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        d["themes"] = [t.to_dict() if isinstance(t, ThemeClassification) else t for t in self.themes]
        return d


# ---------------------------------------------------------------------------
# Theme keyword definitions
# ---------------------------------------------------------------------------

THEME_KEYWORD_MAP: Dict[str, dict] = {
    "ai_expansion": {
        "label": "AI Expansion",
        "keywords": [
            "ai revenue", "ai growth", "ai investment", "ai spending",
            "ai demand", "ai adoption", "ai infrastructure", "gpu demand",
            "data center", "artificial intelligence", "ai capex",
            "generative ai", "ai chip", "nvidia", "cloud ai", "ai enterprise",
            "openai", "chatgpt", "copilot", "machine learning",
            "semiconductor", "gpu", "ai server", "ai model",
        ],
        "activation_threshold": 0.01,
    },
    "ai_deceleration": {
        "label": "AI Deceleration",
        "keywords": [
            "ai slowdown", "ai spending cut", "ai pullback", "ai hype",
            "ai bubble", "ai overinvest", "ai disappoint", "ai fatigue",
            "ai layoff", "ai cost cut", "ai scaling back",
        ],
        "activation_threshold": 0.01,
    },
    "rates_rising": {
        "label": "Rates Rising",
        "keywords": [
            "rate hike", "rates higher", "yield rise", "bond sell",
            "treasury sell", "hawkish", "inflation surprise", "cpi hot",
            "sticky inflation", "tightening", "higher for longer",
            "fed hawk", "rate increase", "yield surge",
            "treasury yield", "10-year", "bond yield", "inflation",
            "interest rate", "fed rate",
        ],
        "activation_threshold": 0.01,
    },
    "rates_falling": {
        "label": "Rates Falling",
        "keywords": [
            "rate cut", "rates lower", "yield fall", "bond rally",
            "treasury rally", "dovish", "inflation cool", "cpi soft",
            "disinflation", "easing", "fed dove", "rate decrease",
            "pivot", "yield drop", "fed cut", "rate reduction",
        ],
        "activation_threshold": 0.01,
    },
    "risk_off": {
        "label": "Risk-Off",
        "keywords": [
            "risk off", "flight to safety", "market fear", "vix spike",
            "sell off", "selloff", "panic", "recession", "credit stress",
            "bank risk", "contagion", "safe haven", "volatility spike",
            "crash", "correction", "bear market", "market drop",
            "market decline", "stock fall", "downturn", "slump",
            "tariff", "trade war", "geopolitical",
        ],
        "activation_threshold": 0.01,
    },
    "risk_on": {
        "label": "Risk-On",
        "keywords": [
            "risk on", "risk appetite", "market rally", "bull market",
            "optimism", "all time high", "all-time high", "record high",
            "breakout", "melt up", "euphoria", "fomo", "equity inflow",
            "market surge", "stock rally", "s&p 500 rise", "nasdaq rise",
            "market gain", "rally", "market climb",
        ],
        "activation_threshold": 0.01,
    },
    "liquidity_expansion": {
        "label": "Liquidity Expansion",
        "keywords": [
            "liquidity inject", "qe", "quantitative easing", "fed balance sheet",
            "reverse repo decline", "tga drawdown", "money supply grow",
            "liquidity flush", "stimulus", "fiscal spend", "fiscal stimulus",
        ],
        "activation_threshold": 0.01,
    },
    "liquidity_tightening": {
        "label": "Liquidity Tightening",
        "keywords": [
            "qt", "quantitative tightening", "liquidity drain", "fed shrink",
            "reverse repo rise", "money supply shrink", "credit tighten",
            "lending standard", "dollar shortage", "funding stress",
        ],
        "activation_threshold": 0.01,
    },
    "energy_shock": {
        "label": "Energy Shock",
        "keywords": [
            "oil spike", "oil surge", "energy crisis", "opec cut",
            "gasoline price", "natural gas surge", "energy shock",
            "crude spike", "oil embargo", "refinery", "oil supply",
            "energy shortage", "brent surge", "oil price",
            "crude oil", "opec", "energy price",
        ],
        "activation_threshold": 0.01,
    },
    "sector_rotation": {
        "label": "Sector Rotation",
        "keywords": [
            "sector rotation", "rotation into", "rotation out of",
            "value over growth", "growth over value", "small cap rotation",
            "defensive rotation", "cyclical rotation", "style rotation",
            "broadening", "market breadth", "small cap", "value stock",
            "growth stock", "defensive", "cyclical",
        ],
        "activation_threshold": 0.01,
    },
}


# ---------------------------------------------------------------------------
# Theme → Pair eligibility mapping
# ---------------------------------------------------------------------------

# Maps theme_id -> list of (pair_id, directional_bias) tuples.
# directional_bias: "long_ratio" means the long leg outperforms (ratio up),
# "short_ratio" means the short leg outperforms (ratio down).
THEME_PAIR_ELIGIBILITY: Dict[str, List[dict]] = {
    "ai_expansion": [
        {"pair_id": "QQQ_IWM", "bias": "long_ratio"},
        {"pair_id": "QQQ_SPY", "bias": "long_ratio"},
        {"pair_id": "SOXX_IGV", "bias": "long_ratio"},
        {"pair_id": "XLF_XLK", "bias": "short_ratio"},
        {"pair_id": "IBIT_QQQ", "bias": "long_ratio"},
    ],
    "ai_deceleration": [
        {"pair_id": "QQQ_IWM", "bias": "short_ratio"},
        {"pair_id": "QQQ_SPY", "bias": "short_ratio"},
        {"pair_id": "SOXX_IGV", "bias": "short_ratio"},
        {"pair_id": "XLF_XLK", "bias": "long_ratio"},
        {"pair_id": "RSP_SPY", "bias": "long_ratio"},
    ],
    "rates_rising": [
        {"pair_id": "GLD_TLT", "bias": "long_ratio"},
        {"pair_id": "XLF_XLK", "bias": "long_ratio"},
        {"pair_id": "HYG_TLT", "bias": "short_ratio"},
        {"pair_id": "XHB_SPY", "bias": "short_ratio"},
        {"pair_id": "UUP_SPY", "bias": "long_ratio"},
    ],
    "rates_falling": [
        {"pair_id": "GLD_TLT", "bias": "short_ratio"},
        {"pair_id": "XLF_XLK", "bias": "short_ratio"},
        {"pair_id": "HYG_TLT", "bias": "long_ratio"},
        {"pair_id": "XHB_SPY", "bias": "long_ratio"},
        {"pair_id": "UUP_SPY", "bias": "short_ratio"},
    ],
    "risk_off": [
        {"pair_id": "VIXY_SPY", "bias": "long_ratio"},
        {"pair_id": "SPHB_SPLV", "bias": "short_ratio"},
        {"pair_id": "XLY_XLP", "bias": "short_ratio"},
        {"pair_id": "SPY_EFA", "bias": "long_ratio"},
        {"pair_id": "HYG_TLT", "bias": "short_ratio"},
        {"pair_id": "XLI_XLU", "bias": "short_ratio"},
    ],
    "risk_on": [
        {"pair_id": "VIXY_SPY", "bias": "short_ratio"},
        {"pair_id": "SPHB_SPLV", "bias": "long_ratio"},
        {"pair_id": "XLY_XLP", "bias": "long_ratio"},
        {"pair_id": "QQQ_IWM", "bias": "long_ratio"},
        {"pair_id": "HYG_TLT", "bias": "long_ratio"},
        {"pair_id": "XLI_XLU", "bias": "long_ratio"},
    ],
    "liquidity_expansion": [
        {"pair_id": "SPHB_SPLV", "bias": "long_ratio"},
        {"pair_id": "QQQ_SPY", "bias": "long_ratio"},
        {"pair_id": "IBIT_QQQ", "bias": "long_ratio"},
        {"pair_id": "SPY_IWM", "bias": "short_ratio"},
    ],
    "liquidity_tightening": [
        {"pair_id": "SPHB_SPLV", "bias": "short_ratio"},
        {"pair_id": "QQQ_SPY", "bias": "short_ratio"},
        {"pair_id": "HYG_TLT", "bias": "short_ratio"},
        {"pair_id": "XBI_XLV", "bias": "short_ratio"},
    ],
    "energy_shock": [
        {"pair_id": "XLE_SPY", "bias": "long_ratio"},
        {"pair_id": "USO_SPY", "bias": "long_ratio"},
        {"pair_id": "XLI_XLU", "bias": "short_ratio"},
        {"pair_id": "XLY_XLP", "bias": "short_ratio"},
    ],
    "sector_rotation": [
        {"pair_id": "RSP_SPY", "bias": "long_ratio"},
        {"pair_id": "SPY_IWM", "bias": "short_ratio"},
        {"pair_id": "XLB_XLI", "bias": "long_ratio"},
        {"pair_id": "XBI_XLV", "bias": "long_ratio"},
    ],
}


# ---------------------------------------------------------------------------
# Layer 1: Deterministic keyword classifier (GATING TRUTH)
# ---------------------------------------------------------------------------


def classify_themes_deterministic(
    headlines: List[str],
    macro_data: Optional[dict] = None,
) -> ThemeResult:
    """Pure-function theme classifier.  No external calls.  No randomness.

    Each headline is lower-cased and matched against keyword sets.
    A theme activates when hit_ratio >= activation_threshold.

    Static themes from THEME_KEYWORD_MAP are the immutable bedrock.
    Active dynamic themes from the LLM nightly review are merged in
    additively — they can never overwrite or remove static themes.
    """
    if not headlines:
        _LOG.warning("Engine7 theme classifier: 0 headlines supplied — all themes will be inactive")
        return ThemeResult(themes=[], active_themes=[], headline_count=0)

    # Merge static + dynamic keyword maps (dynamic never overwrites static)
    merged_map: Dict[str, dict] = dict(THEME_KEYWORD_MAP)
    try:
        from backend.engine7_llm_review import load_dynamic_themes
        dynamic = load_dynamic_themes()
        dynamic_ids = set()
        for tid, tdef in dynamic.items():
            if tid not in merged_map:
                merged_map[tid] = tdef
                dynamic_ids.add(tid)
        if dynamic_ids:
            _LOG.info("Engine7 theme classifier: merged %d dynamic themes: %s",
                      len(dynamic_ids), sorted(dynamic_ids))
    except Exception as exc:
        _LOG.debug("Engine7 theme classifier: dynamic theme load skipped: %s", exc)
        dynamic_ids = set()

    n = len(headlines)
    lower_headlines = [h.lower() for h in headlines]
    results: List[ThemeClassification] = []

    # Per-headline match log (top 25 headlines + their matches)
    headline_matches: List[Dict[str, Any]] = []

    for idx, (orig, low) in enumerate(zip(headlines[:25], lower_headlines[:25])):
        matched_themes_for_hl: List[str] = []
        matched_kw_for_hl: List[str] = []
        for theme_id, tdef in merged_map.items():
            for kw in tdef["keywords"]:
                if kw in low:
                    matched_themes_for_hl.append(theme_id)
                    matched_kw_for_hl.append(kw)
                    break
        headline_matches.append({
            "idx": idx,
            "title": orig[:120],
            "themes": matched_themes_for_hl,
            "keywords": matched_kw_for_hl,
        })

    matched_count = sum(1 for hm in headline_matches if hm["themes"])
    _LOG.info(
        "Engine7 theme classifier: %d headlines, %d/%d of top-25 matched at least one theme",
        n, matched_count, min(25, n),
    )
    for hm in headline_matches:
        if hm["themes"]:
            _LOG.info(
                "  [%d] MATCH themes=%s kw=%s | %s",
                hm["idx"], hm["themes"], hm["keywords"], hm["title"],
            )

    # Recency decay: EODHD returns newest-first.  The first 60% of
    # headlines are treated as "recent" (weight 1.0) and the remaining
    # 40% as "older" (weight 0.5).  This biases activation toward themes
    # that are driving the current tape without losing weekend coverage.
    _RECENT_CUTOFF = 0.60
    _RECENT_WEIGHT = 1.0
    _OLDER_WEIGHT = 0.5
    recent_boundary = int(n * _RECENT_CUTOFF)
    headline_weights = [_RECENT_WEIGHT if i < recent_boundary else _OLDER_WEIGHT for i in range(n)]
    total_weight = sum(headline_weights)

    for theme_id, tdef in merged_map.items():
        kws = tdef["keywords"]
        threshold = tdef.get("activation_threshold", 0.02)
        raw_hits = 0
        weighted_hits = 0.0
        matched_kws: List[str] = []

        for idx_hl, hl in enumerate(lower_headlines):
            for kw in kws:
                if kw in hl:
                    raw_hits += 1
                    weighted_hits += headline_weights[idx_hl]
                    if kw not in matched_kws:
                        matched_kws.append(kw)
                    break  # one hit per headline per theme

        hit_ratio = weighted_hits / total_weight if total_weight > 0 else 0.0
        intensity = min(100.0, hit_ratio * 500.0)  # 20% weighted ratio -> 100
        active = hit_ratio >= threshold and raw_hits >= 1

        is_dynamic = theme_id in dynamic_ids
        tag = " [DYNAMIC]" if is_dynamic else ""
        _LOG.info(
            "  THEME %-22s | raw=%3d weighted=%.1f ratio=%.4f threshold=%.4f intensity=%6.2f → %s%s",
            theme_id, raw_hits, weighted_hits, hit_ratio, threshold, intensity,
            "ACTIVE" if active else "inactive", tag,
        )

        results.append(ThemeClassification(
            theme=theme_id,
            label=tdef["label"],
            active=active,
            intensity=round(intensity, 2),
            keyword_hits=raw_hits,
            sample_keywords=matched_kws[:5],
        ))

    active_themes = [t.theme for t in results if t.active]
    _LOG.info(
        "Engine7 theme classifier RESULT: %d active themes out of %d candidates → %s",
        len(active_themes), len(results), active_themes or "NONE",
    )
    return ThemeResult(
        themes=results,
        active_themes=active_themes,
        headline_count=n,
    )


# ---------------------------------------------------------------------------
# Theme alignment scoring
# ---------------------------------------------------------------------------


def _get_merged_eligibility() -> Dict[str, List[dict]]:
    """Merge static THEME_PAIR_ELIGIBILITY with dynamic theme pair mappings."""
    merged = dict(THEME_PAIR_ELIGIBILITY)
    try:
        from backend.engine7_llm_review import load_dynamic_themes
        for tid, tdef in load_dynamic_themes().items():
            if tid not in merged:
                merged[tid] = tdef.get("pair_mappings", [])
    except Exception:
        pass
    return merged


def score_theme_alignment(pair_id: str, theme_result: ThemeResult) -> Tuple[float, List[str]]:
    """Return (score 0-100, matching_theme_tags) for a pair given active themes.

    Score is 0 when no active theme covers the pair (-> NOT_ELIGIBLE under INV-2).
    Checks both static and dynamic theme pair eligibility maps.
    """
    if not theme_result.active_themes:
        return 0.0, []

    eligibility = _get_merged_eligibility()
    matching_tags: List[str] = []
    total_intensity = 0.0

    for theme_id in theme_result.active_themes:
        eligible_pairs = eligibility.get(theme_id, [])
        for ep in eligible_pairs:
            if ep.get("pair_id") == pair_id:
                matching_tags.append(theme_id)
                theme_cls = next((t for t in theme_result.themes if t.theme == theme_id), None)
                if theme_cls:
                    total_intensity += theme_cls.intensity
                break

    if not matching_tags:
        return 0.0, []

    score = min(100.0, total_intensity / len(matching_tags) if matching_tags else 0.0)
    score = max(score, 20.0) if matching_tags else 0.0
    return round(score, 2), matching_tags


def get_theme_bias(pair_id: str, theme_result: ThemeResult) -> Optional[str]:
    """Return the expected directional bias for a pair from active themes.

    Returns "long_ratio", "short_ratio", or None if no theme applies / mixed.
    Checks both static and dynamic theme pair eligibility maps.
    """
    eligibility = _get_merged_eligibility()
    biases: List[str] = []
    for theme_id in theme_result.active_themes:
        eligible_pairs = eligibility.get(theme_id, [])
        for ep in eligible_pairs:
            if ep.get("pair_id") == pair_id:
                biases.append(ep.get("bias", ""))
                break

    if not biases:
        return None
    if all(b == biases[0] for b in biases):
        return biases[0]
    return None  # mixed signals


# ---------------------------------------------------------------------------
# Layer 2: LLM annotation (OPTIONAL ENRICHMENT – never affects scoring)
# ---------------------------------------------------------------------------

_LLM_ANNOTATION_PROMPT = """You are a macro strategist. Given the following market headlines,
identify the dominant macro themes currently in play and briefly describe how
each theme might affect relative performance between paired assets.

Headlines:
{headlines}

Respond with valid JSON only:
{{
  "dominant_themes": [
    {{"theme": "<theme_name>", "description": "<one sentence>", "confidence": <0.0-1.0>}}
  ],
  "macro_summary": "<two sentences summarising the macro environment>"
}}"""

_ANNOTATION_REDIS_PREFIX = "engine7:theme_annotation"


def _annotation_cache_key(date_str: str, headlines: List[str], model: str) -> str:
    """Build a deterministic Redis key for a given input set."""
    sorted_hl = sorted(headlines)
    payload = json.dumps(sorted_hl, sort_keys=True) + "|" + model
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{_ANNOTATION_REDIS_PREFIX}:{date_str}:{digest}"


def annotate_themes_llm(
    headlines: List[str],
    date_str: str,
    *,
    store: Any = None,
    model: str = "gpt-5.5",
    ttl_s: int = 7 * 86400,
) -> Optional[Dict[str, Any]]:
    """Optional LLM enrichment.  Replays from Redis when available.

    INV-1: this output is NEVER used for scoring or eligibility.
    """
    if not headlines:
        return None

    cache_key = _annotation_cache_key(date_str, headlines, model)

    # Try Redis replay
    if store is not None:
        try:
            cached = store.get_json(cache_key)
            if cached is not None:
                _LOG.debug("Engine7 theme LLM annotation replayed from Redis: %s", cache_key)
                return cached
        except Exception:
            pass

    # Call LLM
    try:
        from backend.llm_client import _get_openai_client, _rate_limiter, _parse_desk_brief_json
    except ImportError:
        _LOG.debug("LLM client not available for Engine7 annotation")
        return None

    if not _rate_limiter.acquire():
        _LOG.debug("Engine7 theme LLM rate-limited; skipping annotation")
        return None

    client = _get_openai_client()
    if client is None:
        return None

    sorted_hl = sorted(headlines)
    prompt = _LLM_ANNOTATION_PROMPT.format(headlines="\n".join(f"- {h}" for h in sorted_hl[:50]))

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=1,
            max_tokens=500,
            timeout=15,
        )
        content = resp.choices[0].message.content or ""
        parsed = _parse_desk_brief_json(content)
        if parsed is None:
            _LOG.warning("Engine7 theme LLM annotation parse failed")
            return None
    except Exception as exc:
        _LOG.warning("Engine7 theme LLM annotation failed: %s", exc)
        return None

    # Persist to Redis for replay
    if store is not None:
        try:
            store.set_json(cache_key, parsed, ttl_s=ttl_s)
            _LOG.debug("Engine7 theme LLM annotation persisted: %s", cache_key)
        except Exception:
            pass

    return parsed


# ---------------------------------------------------------------------------
# Layer 3: LLM-enhanced theme scoring (upgrades keyword classification)
# ---------------------------------------------------------------------------

_LLM_SCORING_PROMPT = """You are a quantitative macro theme classifier. Given a list of market
headlines and a set of predefined themes, score how strongly each theme is
supported by the headlines.

Themes to evaluate:
{theme_list}

Headlines (most recent first):
{headlines}

For each theme, return a confidence score from 0.0 to 1.0 indicating how
strongly the headlines support that theme being active right now. A score of
0.0 means no evidence; 1.0 means overwhelming evidence.

Also flag up to 2 themes that are ABSENT from the predefined list but clearly
present in the headlines (novel themes).

Respond with valid JSON only:
{{
  "scores": {{
    "<theme_id>": <0.0-1.0>,
    ...
  }},
  "novel_themes": [
    {{"theme_id": "<snake_case>", "label": "<short label>", "confidence": <0.0-1.0>, "evidence": "<one sentence>"}}
  ]
}}"""

_LLM_SCORING_REDIS_PREFIX = "engine7:theme_llm_scores"


def _scoring_cache_key(date_str: str, headlines: List[str], model: str) -> str:
    sorted_hl = sorted(headlines)
    payload = json.dumps(sorted_hl, sort_keys=True) + "|scoring|" + model
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    return f"{_LLM_SCORING_REDIS_PREFIX}:{date_str}:{digest}"


def enhance_themes_with_llm(
    keyword_result: "ThemeResult",
    headlines: List[str],
    date_str: str,
    *,
    store: Any = None,
    model: str = "gpt-5.5",
    ttl_s: int = 7 * 86400,
    activation_threshold: float = 0.3,
) -> "ThemeResult":
    """Enhance keyword-based ThemeResult with LLM confidence scores.

    INV-1 guarantee maintained: if LLM fails, returns the original
    keyword_result unchanged. Results are cached in Redis for deterministic
    replay.

    The LLM scores each theme 0.0-1.0. Themes with LLM confidence >= threshold
    that were inactive in keyword classification are activated. Themes with
    LLM confidence < 0.1 that were keyword-activated are demoted (likely
    false positives from substring matching).
    """
    if not headlines:
        return keyword_result

    cache_key = _scoring_cache_key(date_str, headlines, model)

    # Try Redis replay
    llm_scores: Optional[dict] = None
    if store is not None:
        try:
            cached = store.get_json(cache_key)
            if cached is not None:
                llm_scores = cached
                _LOG.debug("Engine7 LLM theme scores replayed from Redis: %s", cache_key)
        except Exception:
            pass

    if llm_scores is None:
        try:
            from backend.llm_client import _get_openai_client, _rate_limiter, _parse_desk_brief_json
        except ImportError:
            _LOG.debug("LLM client not available for Engine7 theme scoring")
            return keyword_result

        if not _rate_limiter.acquire():
            _LOG.debug("Engine7 LLM theme scoring rate-limited; using keyword-only")
            return keyword_result

        client = _get_openai_client()
        if client is None:
            return keyword_result

        theme_list = "\n".join(
            f"- {tid}: {tdef.get('label', tid)}"
            for tid, tdef in THEME_KEYWORD_MAP.items()
        )
        sorted_hl = sorted(headlines)
        prompt = _LLM_SCORING_PROMPT.format(
            theme_list=theme_list,
            headlines="\n".join(f"- {h}" for h in sorted_hl[:60]),
        )

        try:
            resp = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=1,
                max_tokens=800,
                timeout=20,
            )
            content = resp.choices[0].message.content or ""
            llm_scores = _parse_desk_brief_json(content)
            if llm_scores is None:
                _LOG.warning("Engine7 LLM theme scoring parse failed")
                return keyword_result
        except Exception as exc:
            _LOG.warning("Engine7 LLM theme scoring failed: %s", exc)
            return keyword_result

        if store is not None:
            try:
                store.set_json(cache_key, llm_scores, ttl_s=ttl_s)
            except Exception:
                pass

    scores_map = llm_scores.get("scores", {}) if isinstance(llm_scores, dict) else {}
    if not scores_map:
        return keyword_result

    # Merge LLM scores into keyword result
    enhanced_themes: List[ThemeClassification] = []
    enhanced_active: List[str] = []

    for tc in keyword_result.themes:
        llm_conf = scores_map.get(tc.theme)
        if llm_conf is None:
            enhanced_themes.append(tc)
            if tc.active:
                enhanced_active.append(tc.theme)
            continue

        llm_conf = float(llm_conf)

        if tc.active and llm_conf < 0.1:
            _LOG.info(
                "Engine7 LLM demoting false-positive theme %s (keyword=%d hits, llm=%.2f)",
                tc.theme, tc.keyword_hits, llm_conf,
            )
            enhanced_themes.append(ThemeClassification(
                theme=tc.theme, label=tc.label, active=False,
                intensity=round(llm_conf * 100, 1),
                keyword_hits=tc.keyword_hits,
                sample_keywords=tc.sample_keywords,
            ))
        elif not tc.active and llm_conf >= activation_threshold:
            _LOG.info(
                "Engine7 LLM activating missed theme %s (keyword=%d hits, llm=%.2f)",
                tc.theme, tc.keyword_hits, llm_conf,
            )
            new_intensity = round(max(llm_conf * 100, 25.0), 1)
            enhanced_themes.append(ThemeClassification(
                theme=tc.theme, label=tc.label, active=True,
                intensity=new_intensity,
                keyword_hits=tc.keyword_hits,
                sample_keywords=tc.sample_keywords,
            ))
            enhanced_active.append(tc.theme)
        else:
            # Blend: keyword intensity weighted 60%, LLM 40%
            blended = tc.intensity * 0.6 + (llm_conf * 100) * 0.4
            enhanced_themes.append(ThemeClassification(
                theme=tc.theme, label=tc.label,
                active=tc.active,
                intensity=round(blended, 1),
                keyword_hits=tc.keyword_hits,
                sample_keywords=tc.sample_keywords,
            ))
            if tc.active:
                enhanced_active.append(tc.theme)

    return ThemeResult(
        date=keyword_result.date,
        themes=enhanced_themes,
        active_themes=enhanced_active,
        headline_count=keyword_result.headline_count,
        llm_annotation=keyword_result.llm_annotation,
    )


# ---------------------------------------------------------------------------
# Headline fetching helper
# ---------------------------------------------------------------------------


_HEADLINE_FETCH_LIMIT = 500
_HEADLINE_TOPICS = ("market", "earnings")


def fetch_headlines(date_str: str, lookback_days: int = 7) -> List[str]:
    """Fetch recent headlines from EODHD (primary) with Benzinga fallback.

    Fetches across multiple EODHD topics (market + earnings) for denser
    keyword surface area, then deduplicates.  Default lookback is 7 days
    to ensure full coverage across weekends and holidays.
    """
    import os as _os

    end = dt.date.fromisoformat(date_str)
    start = end - dt.timedelta(days=lookback_days)
    seen: set = set()
    titles: List[str] = []

    # --- Primary source: EODHD (multi-topic) ---
    try:
        from backend.eodhd_client import EodhdClient
        token = _os.getenv("EODHD_API_TOKEN", "")
        if not token:
            _LOG.warning("Engine7 headline fetch: EODHD_API_TOKEN not set — skipping EODHD")
        else:
            client = EodhdClient(token=token)
            for topic in _HEADLINE_TOPICS:
                try:
                    resp = client.get_news(
                        topic=topic,
                        from_date=start.isoformat(),
                        to_date=end.isoformat(),
                        limit=_HEADLINE_FETCH_LIMIT,
                    )
                    topic_count = 0
                    for row in (resp.rows or []):
                        title = (row.get("title") or "").strip()
                        if title and title not in seen:
                            seen.add(title)
                            titles.append(title)
                            topic_count += 1
                    _LOG.info(
                        "Engine7 headline fetch [EODHD/%s]: %d unique headlines, window=%s→%s",
                        topic, topic_count, start.isoformat(), end.isoformat(),
                    )
                except Exception as topic_exc:
                    _LOG.warning("Engine7 headline fetch [EODHD/%s] failed: %s", topic, topic_exc)
            _LOG.info(
                "Engine7 headline fetch [EODHD total]: %d unique headlines across topics %s",
                len(titles), _HEADLINE_TOPICS,
            )
    except Exception as exc:
        _LOG.warning("Engine7 headline fetch [EODHD] failed: %s", exc)

    # --- Fallback: Benzinga news if EODHD returned nothing ---
    if not titles:
        try:
            from backend.benzinga_client import BenzingaClient
            bz_key = _os.getenv("BENZINGA_API_KEY", "")
            if bz_key:
                bz = BenzingaClient(api_key=bz_key)
                bz_resp = bz.get_news(
                    from_date=start.isoformat(),
                    to_date=end.isoformat(),
                    page_size=_HEADLINE_FETCH_LIMIT,
                )
                for row in (bz_resp if isinstance(bz_resp, list) else (bz_resp.rows if hasattr(bz_resp, "rows") else [])):
                    title = (row.get("title") or row.get("headline") or "").strip() if isinstance(row, dict) else ""
                    if title and title not in seen:
                        seen.add(title)
                        titles.append(title)
                _LOG.info(
                    "Engine7 headline fetch [Benzinga fallback]: %d headlines, window=%s→%s",
                    len(titles), start.isoformat(), end.isoformat(),
                )
        except Exception as bz_exc:
            _LOG.warning("Engine7 headline fetch [Benzinga fallback] failed: %s", bz_exc)

    if not titles:
        _LOG.warning(
            "Engine7 headline fetch: 0 headlines from ALL sources (window=%s→%s). "
            "Theme classifier will produce no active themes.",
            start.isoformat(), end.isoformat(),
        )

    return titles
