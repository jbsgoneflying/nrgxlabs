"""
Engine 9 — Credit Stress Drift: Signal Computation

Eight signals, weighted composite score, phase detection, and trigger layer.

Signal weights (sum to 100%):
  1  BDC Price-to-Book Divergence   25 %
  2  Credit Spread Acceleration     25 %
  3  NLP Delta-of-Language           5 %
  4  Insider Selling Acceleration   10 %
  5  Correlation Breakdown          20 %
  6  ETF Price vs NAV Deviation      0 % (confirmation overlay)
  7  Funding Stress Proxy           15 %
  8  Time Compression                0 % (meta-signal, phase escalation)

Trigger layer:
  A  Early Entry    — Spread z > 1.5 AND BDC divergence > 40
  B  Scale          — Correlation < 60 score AND insider > 50
  C  Aggressive     — Funding stress > 60 AND HYG 20d MA break
"""
from __future__ import annotations

import logging
import math
import statistics
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Signal result container
# ---------------------------------------------------------------------------

@dataclass
class SignalResult:
    key: str
    label: str
    score: float          # 0-100
    weight: float         # 0.0 – 1.0 (0 for unscored)
    detail: str
    triggered: bool = False
    data: Dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Weights
# ---------------------------------------------------------------------------

SIGNAL_WEIGHTS: Dict[str, float] = {
    "bdc_divergence":       0.25,
    "spread_accel":         0.25,
    "correlation_break":    0.20,
    "funding_stress":       0.15,
    "insider_selling":      0.10,
    "nlp_language":         0.05,
    "etf_nav_deviation":    0.00,
    "time_compression":     0.00,
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _z_score(values: List[float], current: float) -> float:
    if len(values) < 5:
        return 0.0
    mu = statistics.mean(values)
    sigma = statistics.stdev(values)
    if sigma < 1e-9:
        return 0.0
    return (current - mu) / sigma


def _pct_rank(values: List[float], current: float) -> float:
    if not values:
        return 50.0
    below = sum(1 for v in values if v <= current)
    return 100.0 * below / len(values)


def _rolling_corr(xs: List[float], ys: List[float], window: int = 20) -> float:
    """Pearson correlation of last `window` observations."""
    xs = xs[-window:]
    ys = ys[-window:]
    n = min(len(xs), len(ys))
    if n < 10:
        return 0.0
    xs, ys = xs[:n], ys[:n]
    mx, my = statistics.mean(xs), statistics.mean(ys)
    num = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    dx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    dy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if dx < 1e-12 or dy < 1e-12:
        return 0.0
    return num / (dx * dy)


def _roc(values: List[float], period: int) -> float:
    """Rate of change over `period` observations."""
    if len(values) < period + 1:
        return 0.0
    prev = values[-(period + 1)]
    if abs(prev) < 1e-9:
        return 0.0
    return (values[-1] - prev) / abs(prev) * 100.0


def _sma(values: List[float], window: int) -> Optional[float]:
    if len(values) < window:
        return None
    return statistics.mean(values[-window:])


# ---------------------------------------------------------------------------
# Signal 1: BDC Price-to-Book Divergence
# ---------------------------------------------------------------------------

def compute_bdc_divergence(
    prices_30d: List[float],
    prices_60d: List[float],
    prices_90d: List[float],
    last_book_value: Optional[float],
    current_price: Optional[float],
) -> SignalResult:
    """
    Score = how much market price is pulling away from reported book.
    A declining P/B while NAV stays flat = market calling BS.
    """
    key = "bdc_divergence"
    if not current_price or not last_book_value or last_book_value <= 0:
        return SignalResult(key=key, label="BDC Divergence", score=0, weight=0.25,
                           detail="Insufficient data", data={})

    pb_now = current_price / last_book_value
    pb_30d_avg = (statistics.mean(prices_30d) / last_book_value) if prices_30d else pb_now
    pb_60d_avg = (statistics.mean(prices_60d) / last_book_value) if prices_60d else pb_now

    drift_30 = (pb_30d_avg - pb_now) / pb_30d_avg * 100 if pb_30d_avg else 0
    drift_60 = (pb_60d_avg - pb_now) / pb_60d_avg * 100 if pb_60d_avg else 0
    max_drift = max(drift_30, drift_60, 0)

    score = _clamp(max_drift * 10)  # 10% drift = 100

    triggered = score > 40
    detail = f"P/B {pb_now:.2f} (30d avg {pb_30d_avg:.2f}, 60d avg {pb_60d_avg:.2f})"
    return SignalResult(
        key=key, label="BDC Divergence", score=round(score, 1), weight=0.25,
        detail=detail, triggered=triggered,
        data={"pb_now": round(pb_now, 3), "drift_30": round(drift_30, 2), "drift_60": round(drift_60, 2)},
    )


# ---------------------------------------------------------------------------
# Signal 2: Credit Spread Acceleration
# ---------------------------------------------------------------------------

def compute_spread_signal(
    hy_oas_series: List[float],
    vix_series: List[float],
) -> SignalResult:
    """
    HY OAS 20d z-score vs VIX z-score.
    When spreads widen faster than equity vol rises, credit is leading.
    """
    key = "spread_accel"
    if len(hy_oas_series) < 25:
        return SignalResult(key=key, label="Spread Acceleration", score=0, weight=0.25,
                           detail="Insufficient HY OAS data", data={})

    hy_z = _z_score(hy_oas_series[-60:], hy_oas_series[-1])
    vix_z = _z_score(vix_series[-60:], vix_series[-1]) if len(vix_series) >= 25 else 0.0

    roc_5d = _roc(hy_oas_series, 5)
    pct_60 = _pct_rank(hy_oas_series[-60:], hy_oas_series[-1])

    spread_lead = max(hy_z - vix_z, 0)
    raw = (hy_z * 20) + (spread_lead * 15) + (pct_60 * 0.3) + max(roc_5d, 0) * 2
    score = _clamp(raw)

    triggered = hy_z > 1.5 and vix_z < 1.0
    detail = f"HY z={hy_z:+.2f}, VIX z={vix_z:+.2f}, 5d RoC={roc_5d:+.1f}%, 60d rank={pct_60:.0f}%"
    return SignalResult(
        key=key, label="Spread Acceleration", score=round(score, 1), weight=0.25,
        detail=detail, triggered=triggered,
        data={"hy_z": round(hy_z, 3), "vix_z": round(vix_z, 3),
              "roc_5d": round(roc_5d, 2), "pct_60": round(pct_60, 1)},
    )


# ---------------------------------------------------------------------------
# Signal 3: NLP Delta-of-Language (LLM-powered with keyword fallback)
# ---------------------------------------------------------------------------

HEDGING_PHRASES = [
    "prudent", "disciplined", "selective", "monitoring conditions",
    "cautious", "challenging environment", "headwinds", "conservative",
    "measured approach", "careful", "watchful",
]

CONFIDENCE_PHRASES = [
    "strong demand", "robust pipeline", "accelerating growth",
    "record performance", "strong momentum", "excellent results",
    "outperformed", "exceeded expectations", "confident",
]

_TRANSCRIPT_ANALYSIS_SYSTEM = """You are a senior credit analyst specializing in private credit, BDCs, and alternative asset managers. Analyze the following earnings call transcript excerpt and return a JSON object with exactly these keys:

{
  "hedging_score": <0-10, higher = more hedging/cautious language>,
  "confidence_score": <0-10, higher = more forward confidence>,
  "stress_indicators": [<list of specific phrases or themes indicating stress>],
  "tone_shift_vs_prior": "<brief narrative: is management becoming more defensive, cautious, or confident compared to what you'd expect?>",
  "forward_guidance_sentiment": <-1.0 to +1.0, negative = guarded/reducing, positive = expanding/optimistic>,
  "key_risks_mentioned": [<list of specific risks management flagged>],
  "liquidity_language_detected": <true/false, whether gating/redemption/liquidity restriction language appears>
}

Focus on:
- Shifts toward hedging language ("prudent", "disciplined", "selective", "monitoring conditions")
- Reduction in forward confidence (fewer "strong demand", "robust pipeline" phrases)
- Mentions of gating, redemptions, covenant modifications, non-accrual increases
- Changes in how management describes their portfolio quality and funding access

Be precise. This feeds a quantitative signal system."""


def _count_phrases(text: str, phrases: List[str]) -> int:
    text_lower = text.lower()
    return sum(text_lower.count(p) for p in phrases)


def analyze_transcript_llm(
    ticker: str, year: int, quarter: int,
    transcript_text: str, api_key: str,
) -> Optional[dict]:
    """Send transcript to GPT for structured tone analysis. Result is cached in Redis."""
    import json as _json
    from backend.engine9_store import store_transcript_analysis, load_transcript_analysis

    cached = load_transcript_analysis(ticker, year, quarter)
    if cached:
        return cached

    if not api_key or not transcript_text or len(transcript_text.strip()) < 200:
        return None

    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        trimmed = transcript_text[:15000]
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": _TRANSCRIPT_ANALYSIS_SYSTEM},
                {"role": "user", "content": f"Ticker: {ticker} | {year} Q{quarter}\n\n{trimmed}"},
            ],
            temperature=0.2,
            max_tokens=800,
            response_format={"type": "json_object"},
        )
        text = (resp.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3].strip()
        analysis = _json.loads(text)
        analysis["_ticker"] = ticker
        analysis["_year"] = year
        analysis["_quarter"] = quarter
        store_transcript_analysis(ticker, year, quarter, analysis)
        return analysis
    except Exception as e:
        LOG.warning("LLM transcript analysis failed for %s %dQ%d: %s", ticker, year, quarter, e)
        return None


def compute_nlp_from_llm_analyses(analyses_by_ticker: Dict[str, List[dict]]) -> SignalResult:
    """
    Compute NLP signal from cached LLM transcript analyses.
    
    For each ticker, compare most recent quarter to prior quarters to find
    delta-of-language: hedging acceleration, confidence erosion, stress emergence.
    """
    key = "nlp_language"
    if not analyses_by_ticker:
        return SignalResult(key=key, label="NLP Language Drift", score=0, weight=0.05,
                           detail="No transcript analyses available", data={})

    ticker_deltas = []
    per_ticker = {}

    for ticker, analyses in analyses_by_ticker.items():
        if len(analyses) < 2:
            continue
        analyses_sorted = sorted(analyses, key=lambda a: (a.get("_year", 0), a.get("_quarter", 0)), reverse=True)
        latest = analyses_sorted[0]
        priors = analyses_sorted[1:]

        latest_hedge = latest.get("hedging_score", 5)
        latest_conf = latest.get("confidence_score", 5)
        latest_fwd = latest.get("forward_guidance_sentiment", 0)
        latest_liq = latest.get("liquidity_language_detected", False)

        avg_prior_hedge = statistics.mean([a.get("hedging_score", 5) for a in priors])
        avg_prior_conf = statistics.mean([a.get("confidence_score", 5) for a in priors])
        avg_prior_fwd = statistics.mean([a.get("forward_guidance_sentiment", 0) for a in priors])

        hedge_delta = (latest_hedge - avg_prior_hedge) / max(avg_prior_hedge, 1) * 100
        conf_delta = (avg_prior_conf - latest_conf) / max(avg_prior_conf, 1) * 100
        fwd_delta = avg_prior_fwd - latest_fwd

        ticker_score = (
            _clamp(hedge_delta * 3, 0, 100) * 0.35
            + _clamp(conf_delta * 3, 0, 100) * 0.30
            + _clamp(fwd_delta * 50, 0, 100) * 0.20
            + (100 if latest_liq else 0) * 0.15
        )
        ticker_deltas.append(ticker_score)
        per_ticker[ticker] = {
            "hedge_delta": round(hedge_delta, 1),
            "conf_delta": round(conf_delta, 1),
            "fwd_delta": round(fwd_delta, 3),
            "liquidity_flag": latest_liq,
            "score": round(ticker_score, 1),
            "quarters": len(analyses),
        }

    if not ticker_deltas:
        return SignalResult(key=key, label="NLP Language Drift", score=0, weight=0.05,
                           detail="Insufficient cross-quarter data", data={})

    score = _clamp(statistics.mean(ticker_deltas))
    detail = f"LLM-analyzed {len(ticker_deltas)} tickers, avg drift={score:.0f}"
    return SignalResult(
        key=key, label="NLP Language Drift", score=round(score, 1), weight=0.05,
        detail=detail, triggered=score > 40,
        data={"per_ticker": per_ticker, "method": "llm", "tickers_analyzed": len(ticker_deltas)},
    )


def compute_nlp_delta_of_language(
    transcripts: List[Dict[str, Any]],
) -> SignalResult:
    """Keyword-based fallback when LLM analyses aren't available."""
    key = "nlp_language"
    if len(transcripts) < 2:
        return SignalResult(key=key, label="NLP Language Drift", score=0, weight=0.05,
                           detail="Need 2+ quarters of transcripts", data={})

    hedging_counts: List[float] = []
    confidence_counts: List[float] = []
    sentiment_scores: List[float] = []

    for t in transcripts:
        text = t.get("transcript") or ""
        word_count = max(len(text.split()), 1)
        h = _count_phrases(text, HEDGING_PHRASES)
        c = _count_phrases(text, CONFIDENCE_PHRASES)
        hedging_counts.append(h / word_count * 10000)
        confidence_counts.append(c / word_count * 10000)
        sent = t.get("overall_sentiment")
        if sent is not None:
            try:
                sentiment_scores.append(float(sent))
            except (ValueError, TypeError):
                pass

    sentiment_delta_score = 0.0
    if len(sentiment_scores) >= 2:
        avg_prior = statistics.mean(sentiment_scores[1:])
        current = sentiment_scores[0]
        delta = avg_prior - current
        sentiment_delta_score = _clamp(delta * 100, 0, 100)

    hedging_accel_score = 0.0
    if len(hedging_counts) >= 2:
        latest = hedging_counts[0]
        avg_prior = statistics.mean(hedging_counts[1:])
        if avg_prior > 0:
            accel = (latest - avg_prior) / avg_prior * 100
            hedging_accel_score = _clamp(accel * 2, 0, 100)

    confidence_reduction_score = 0.0
    if len(confidence_counts) >= 2:
        latest = confidence_counts[0]
        avg_prior = statistics.mean(confidence_counts[1:])
        if avg_prior > 0:
            reduction = (avg_prior - latest) / avg_prior * 100
            confidence_reduction_score = _clamp(reduction * 2, 0, 100)

    score = (
        sentiment_delta_score * 0.40
        + hedging_accel_score * 0.35
        + confidence_reduction_score * 0.25
    )
    score = _clamp(score)

    detail = (
        f"Keyword fallback: Sentiment Δ={sentiment_delta_score:.0f}, "
        f"Hedging accel={hedging_accel_score:.0f}, "
        f"Confidence drop={confidence_reduction_score:.0f}"
    )
    return SignalResult(
        key=key, label="NLP Language Drift", score=round(score, 1), weight=0.05,
        detail=detail, triggered=score > 50,
        data={
            "sentiment_delta": round(sentiment_delta_score, 1),
            "hedging_accel": round(hedging_accel_score, 1),
            "confidence_reduction": round(confidence_reduction_score, 1),
            "quarters_analyzed": len(transcripts),
            "method": "keyword_fallback",
        },
    )


# ---------------------------------------------------------------------------
# Signal 4: Insider Selling Acceleration
# ---------------------------------------------------------------------------

def compute_insider_signal(
    net_selling_30d: float,
    net_selling_60d: float,
    net_selling_90d: float,
    transaction_count: int,
) -> SignalResult:
    """
    Score based on net insider selling acceleration.
    Alert when net selling exceeds 2x the 90d average.
    """
    key = "insider_selling"
    if transaction_count < 1:
        return SignalResult(key=key, label="Insider Selling", score=0, weight=0.10,
                           detail="No insider transactions found in window",
                           data={"status": "no_data"})

    avg_90d = net_selling_90d / 3 if net_selling_90d else 0
    ratio = (net_selling_30d / avg_90d) if avg_90d > 0 else 0

    score = _clamp(ratio * 25)
    triggered = ratio > 2.0

    detail = (
        f"30d net=${net_selling_30d:,.0f}, "
        f"90d avg/mo=${avg_90d:,.0f}, "
        f"accel={ratio:.1f}x"
    )
    return SignalResult(
        key=key, label="Insider Selling", score=round(score, 1), weight=0.10,
        detail=detail, triggered=triggered,
        data={
            "net_30d": round(net_selling_30d, 2),
            "net_60d": round(net_selling_60d, 2),
            "net_90d": round(net_selling_90d, 2),
            "ratio": round(ratio, 2),
            "txn_count": transaction_count,
        },
    )


# ---------------------------------------------------------------------------
# Signal 5: Correlation Breakdown
# ---------------------------------------------------------------------------

def compute_correlation_breakdown(
    spy_returns: List[float],
    hyg_returns: List[float],
    hyg_prices: List[float],
) -> SignalResult:
    """
    Rolling 20d correlation between SPY and HYG.
    Stress = corr < 0.4 while HYG declining.
    """
    key = "correlation_break"
    if len(spy_returns) < 15 or len(hyg_returns) < 15:
        return SignalResult(key=key, label="Correlation Breakdown", score=0, weight=0.20,
                           detail="Insufficient return data", data={})

    corr_20 = _rolling_corr(spy_returns, hyg_returns, 20)
    hyg_declining = False
    if len(hyg_prices) >= 20:
        sma_20 = _sma(hyg_prices, 20)
        if sma_20 and hyg_prices[-1] < sma_20:
            hyg_declining = True

    raw = 0.0
    if corr_20 < 0.7:
        decorrelation = (0.7 - corr_20) / 0.7 * 100
        raw = decorrelation
        if hyg_declining:
            raw *= 1.5

    score = _clamp(raw)
    triggered = corr_20 < 0.4 and hyg_declining

    detail = f"SPY-HYG corr={corr_20:.2f}, HYG trend={'declining' if hyg_declining else 'stable'}"
    return SignalResult(
        key=key, label="Correlation Breakdown", score=round(score, 1), weight=0.20,
        detail=detail, triggered=triggered,
        data={"corr_20": round(corr_20, 3), "hyg_declining": hyg_declining},
    )


# ---------------------------------------------------------------------------
# Signal 6: ETF Price vs NAV Deviation (confirmation overlay)
# ---------------------------------------------------------------------------

def compute_etf_nav_deviation(
    etf_prices: List[float],
    etf_nav: Optional[float],
) -> SignalResult:
    """
    Track market price vs reported NAV for credit ETFs.
    Persistent discount = forced-selling pressure.
    """
    key = "etf_nav_deviation"
    if not etf_nav or etf_nav <= 0 or not etf_prices:
        return SignalResult(key=key, label="ETF Price/NAV", score=0, weight=0.0,
                           detail="No NAV data", data={})

    current = etf_prices[-1]
    discount = (etf_nav - current) / etf_nav * 100

    avg_5d = statistics.mean(etf_prices[-5:]) if len(etf_prices) >= 5 else current
    discount_5d = (etf_nav - avg_5d) / etf_nav * 100

    widening = discount > discount_5d

    score = _clamp(max(discount, 0) * 20)

    detail = f"Discount={discount:+.2f}% (5d avg={discount_5d:+.2f}%), {'widening' if widening else 'stable'}"
    return SignalResult(
        key=key, label="ETF Price/NAV", score=round(score, 1), weight=0.0,
        detail=detail, triggered=discount > 1.0,
        data={"discount_pct": round(discount, 3), "discount_5d": round(discount_5d, 3),
              "widening": widening},
    )


# ---------------------------------------------------------------------------
# Signal 7: Funding Stress Proxy
# ---------------------------------------------------------------------------

def compute_funding_stress(
    bkln_prices: List[float],
    hyg_prices: List[float],
    dgs2_series: List[float],
    dgs10_series: List[float],
) -> SignalResult:
    """
    BKLN vs HYG divergence + 2s10s steepening after inversion.
    When funding layer cracks, forced selling begins.
    """
    key = "funding_stress"
    score_parts: List[float] = []

    # Sub-signal A: BKLN vs HYG divergence
    bkln_hyg_score = 0.0
    if len(bkln_prices) >= 20 and len(hyg_prices) >= 20:
        bkln_ret_20 = (bkln_prices[-1] / bkln_prices[-20] - 1) * 100
        hyg_ret_20 = (hyg_prices[-1] / hyg_prices[-20] - 1) * 100
        divergence = hyg_ret_20 - bkln_ret_20
        if bkln_ret_20 < 0 and divergence > 0:
            bkln_hyg_score = _clamp(abs(bkln_ret_20) * 15 + divergence * 10)
        score_parts.append(bkln_hyg_score)

    # Sub-signal B: 2s10s steepening after inversion
    curve_score = 0.0
    if len(dgs2_series) >= 60 and len(dgs10_series) >= 60:
        spread_now = dgs10_series[-1] - dgs2_series[-1]
        spread_30d = dgs10_series[-30] - dgs2_series[-30] if len(dgs10_series) >= 30 else spread_now
        spread_60d = dgs10_series[-60] - dgs2_series[-60]

        was_inverted = spread_60d < 0 or spread_30d < 0
        steepening = spread_now > spread_30d and spread_now > spread_60d

        if was_inverted and steepening:
            curve_score = _clamp((spread_now - spread_60d) * 30 + 30)
        score_parts.append(curve_score)

    score = statistics.mean(score_parts) if score_parts else 0.0
    score = _clamp(score)

    triggered = score > 60
    detail_parts = []
    if len(bkln_prices) >= 20:
        detail_parts.append(f"BKLN/HYG div={bkln_hyg_score:.0f}")
    if len(dgs2_series) >= 60:
        detail_parts.append(f"Curve signal={curve_score:.0f}")
    detail = ", ".join(detail_parts) or "Insufficient data"

    return SignalResult(
        key=key, label="Funding Stress", score=round(score, 1), weight=0.15,
        detail=detail, triggered=triggered,
        data={"bkln_hyg_score": round(bkln_hyg_score, 1), "curve_score": round(curve_score, 1)},
    )


# ---------------------------------------------------------------------------
# Signal 8: Time Compression (meta-signal)
# ---------------------------------------------------------------------------

def compute_time_compression(
    signal_results: List[SignalResult],
    recent_trigger_dates: Dict[str, List[str]],
    window_days: int = 5,
) -> SignalResult:
    """
    Meta-signal: when 3+ scored signals cross threshold within 5 trading days,
    events are accelerating. Escalates phase by 1.
    """
    key = "time_compression"
    scored_keys = [k for k, w in SIGNAL_WEIGHTS.items() if w > 0]

    today = date.today()
    cutoff = today - timedelta(days=window_days * 2)

    recent_fires = 0
    for sig_key in scored_keys:
        dates = recent_trigger_dates.get(sig_key, [])
        recent = [d for d in dates if d >= cutoff.isoformat()]
        if recent:
            recent_fires += 1

    currently_triggered = sum(1 for s in signal_results if s.weight > 0 and s.triggered)

    clustering = max(recent_fires, currently_triggered)
    active = clustering >= 3

    score = _clamp(clustering / 6 * 100) if active else _clamp(clustering / 6 * 50)

    detail = f"{clustering} signals active in {window_days}d window" + (" — CLUSTERING" if active else "")
    return SignalResult(
        key=key, label="Time Compression", score=round(score, 1), weight=0.0,
        detail=detail, triggered=active,
        data={"clustering_count": clustering, "window_days": window_days, "active": active},
    )


# ---------------------------------------------------------------------------
# Weighted Composite & Phase Detection
# ---------------------------------------------------------------------------

PHASE_THRESHOLDS = [
    (25, 1, "Gates up, early stress"),
    (50, 2, "Markdowns beginning"),
    (75, 3, "Defaults rising, CLO stress"),
    (100, 4, "Crowded trade"),
]


def compute_weighted_composite(
    signals: List[SignalResult],
    time_compression_active: bool = False,
) -> Dict[str, Any]:
    """
    Weighted composite score and phase detection.

    Returns dict with composite, phase (1-4), phase_label, phase_action,
    and individual signal contributions.
    """
    weighted_sum = 0.0
    total_weight = 0.0
    contributions: Dict[str, float] = {}

    for sig in signals:
        if sig.weight > 0:
            contrib = sig.score * sig.weight
            weighted_sum += contrib
            total_weight += sig.weight
            contributions[sig.key] = round(contrib, 2)

    composite = round(weighted_sum, 1) if total_weight > 0 else 0.0

    phase = 1
    phase_label = PHASE_THRESHOLDS[0][2]
    for threshold, p, label in PHASE_THRESHOLDS:
        if composite <= threshold:
            phase = p
            phase_label = label
            break

    if time_compression_active and phase < 4:
        phase += 1
        phase_label = PHASE_THRESHOLDS[phase - 1][2] if phase <= 4 else phase_label

    phase_actions = {
        1: "Small starter positions, BDCs + weakest alt manager",
        2: "Add size, put spreads on OWL/APO/ARES, short BDC basket",
        3: "HYG puts, VIX convexity",
        4: "Take profits into volatility, do NOT chase",
    }

    return {
        "composite": composite,
        "phase": phase,
        "phase_label": phase_label,
        "phase_action": phase_actions.get(phase, ""),
        "contributions": contributions,
        "time_compression_escalated": time_compression_active and phase > 1,
    }


# ---------------------------------------------------------------------------
# Trigger Layer
# ---------------------------------------------------------------------------

@dataclass
class TriggerResult:
    name: str
    level: str        # "A", "B", "C"
    active: bool
    condition: str
    action: str
    sizing: str


def evaluate_triggers(
    signals: Dict[str, SignalResult],
    hyg_prices: Optional[List[float]] = None,
) -> List[TriggerResult]:
    """
    Evaluate Trigger A/B/C conditions.
    """
    triggers: List[TriggerResult] = []

    spread = signals.get("spread_accel")
    bdc = signals.get("bdc_divergence")
    corr = signals.get("correlation_break")
    insider = signals.get("insider_selling")
    funding = signals.get("funding_stress")

    spread_z = (spread.data.get("hy_z", 0) if spread else 0)

    # Trigger A: Early Entry
    trigger_a_active = (
        spread_z > 1.5
        and bdc is not None and bdc.score > 40
    )
    triggers.append(TriggerResult(
        name="Early Entry", level="A", active=trigger_a_active,
        condition="Spread z-score > 1.5 AND BDC divergence active",
        action="Open starter short positions in Tier 1 BDCs + weakest Tier 2 name",
        sizing="0.25-0.5% of book per position",
    ))

    # Trigger B: Scale
    trigger_b_active = (
        corr is not None and corr.score > 60
        and insider is not None and insider.score > 50
    )
    triggers.append(TriggerResult(
        name="Scale", level="B", active=trigger_b_active,
        condition="Correlation breakdown confirmed AND insider selling elevated",
        action="Add size to existing positions, put spreads on Tier 2 alt managers",
        sizing="Scale to 1-2% of book per position",
    ))

    # Trigger C: Aggressive
    hyg_trend_break = False
    if hyg_prices and len(hyg_prices) >= 20:
        sma_20 = _sma(hyg_prices, 20)
        if sma_20 and hyg_prices[-1] < sma_20:
            hyg_trend_break = True

    trigger_c_active = (
        funding is not None and funding.score > 60
        and hyg_trend_break
    )
    triggers.append(TriggerResult(
        name="Aggressive", level="C", active=trigger_c_active,
        condition="Funding stress fires AND HYG 20d MA crossover down",
        action="Full positioning — HYG puts, VIX convexity, short BDC basket",
        sizing="Full allocation per risk budget",
    ))

    return triggers


# ---------------------------------------------------------------------------
# Thesis Invalidation
# ---------------------------------------------------------------------------

def evaluate_thesis_health(
    fed_funds_latest: Optional[float],
    fed_funds_30d_ago: Optional[float],
    hy_oas_latest: Optional[float],
    hy_oas_20d_ma: Optional[float],
    bdc_price_stable: bool = False,
) -> List[Dict[str, Any]]:
    """
    Check thesis invalidation conditions.
    Returns list of indicator dicts.
    """
    indicators: List[Dict[str, Any]] = []

    rate_cutting = False
    if fed_funds_latest is not None and fed_funds_30d_ago is not None:
        rate_cutting = fed_funds_latest < fed_funds_30d_ago - 0.10
    indicators.append({
        "name": "Rate Cuts",
        "healthy": not rate_cutting,
        "detail": f"Fed funds {'declining' if rate_cutting else 'stable/rising'}",
    })

    spread_compressing = False
    if hy_oas_latest is not None and hy_oas_20d_ma is not None:
        spread_compressing = hy_oas_latest < hy_oas_20d_ma
    indicators.append({
        "name": "Spread Compression",
        "healthy": not spread_compressing,
        "detail": f"HY OAS {'below' if spread_compressing else 'above'} 20d MA",
    })

    indicators.append({
        "name": "BDC Price Stability",
        "healthy": not bdc_price_stable,
        "detail": f"BDC prices {'stabilized (thesis risk)' if bdc_price_stable else 'still moving'}",
    })

    return indicators


# ---------------------------------------------------------------------------
# News Cycle Signal (context overlay, not scored in composite)
# ---------------------------------------------------------------------------

CREDIT_STRESS_KEYWORDS = [
    "gating", "redemption", "default", "downgrade", "covenant",
    "leverage", "liquidity", "writedown", "non-accrual", "credit stress",
    "forced selling", "margin call", "distressed", "impairment",
    "restructuring", "bankruptcy", "CLO", "private credit",
    "warehouse line", "funding pressure",
]


def filter_credit_news(articles: List[dict]) -> List[dict]:
    """Filter news articles for credit-stress relevant headlines."""
    relevant = []
    for article in articles:
        title = (article.get("title") or "").lower()
        content = (article.get("content") or article.get("text") or "").lower()[:500]
        combined = f"{title} {content}"
        matched = [kw for kw in CREDIT_STRESS_KEYWORDS if kw in combined]
        if matched:
            relevant.append({
                "title": article.get("title", ""),
                "date": article.get("date", ""),
                "link": article.get("link", ""),
                "source": article.get("source", ""),
                "matched_keywords": matched,
            })
    return relevant


def score_news_with_llm(headlines: List[dict], api_key: str) -> dict:
    """Send top credit headlines to GPT for relevance scoring."""
    import json as _json

    if not api_key or not headlines:
        return {"articles": headlines, "llm_scored": False, "avg_relevance": 0}

    try:
        import openai
        client = openai.OpenAI(api_key=api_key)
        headline_text = "\n".join(
            f"{i+1}. [{a.get('date','')}] {a.get('title','')}"
            for i, a in enumerate(headlines[:15])
        )
        resp = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": (
                    "You are a credit research analyst. Rate each headline for relevance to "
                    "private credit stress. Return JSON: {\"scores\": [{\"index\": 1, \"relevance\": 0-10, "
                    "\"why\": \"brief reason\"},...], \"summary\": \"1-2 sentence overall assessment\"}"
                )},
                {"role": "user", "content": headline_text},
            ],
            temperature=0.2,
            max_tokens=1000,
            response_format={"type": "json_object"},
        )
        text = (resp.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3].strip()
        parsed = _json.loads(text)
        scores = parsed.get("scores", [])
        for s in scores:
            idx = s.get("index", 0) - 1
            if 0 <= idx < len(headlines):
                headlines[idx]["llm_relevance"] = s.get("relevance", 0)
                headlines[idx]["llm_reason"] = s.get("why", "")

        avg_rel = (
            statistics.mean([s.get("relevance", 0) for s in scores])
            if scores else 0
        )
        return {
            "articles": headlines,
            "llm_scored": True,
            "avg_relevance": round(avg_rel, 1),
            "summary": parsed.get("summary", ""),
        }
    except Exception as e:
        LOG.warning("News LLM scoring failed: %s", e)
        return {"articles": headlines, "llm_scored": False, "avg_relevance": 0}
