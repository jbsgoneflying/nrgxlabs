"""Engine 10 — Multi-Ticker Portfolio Advisor.

Transforms Engine 10's ranked ticker list into an actionable allocation plan.
Two layers:
  1. Deterministic: correlation bucketing, Kelly-lite sizing, regime cap,
     concentration limits, timing conflict detection.
  2. LLM: head-of-book review that can override or adjust the quant allocation.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import math
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.config import FeatureFlags, get_flags
from backend.daily_market_state import load_dms
from backend.redis_store import get_store_optional

LOG = logging.getLogger(__name__)

_PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"

# ---------------------------------------------------------------------------
# GICS-like sector mapping (covers large-/mega-cap earnings names)
# ---------------------------------------------------------------------------

_SECTOR_MAP: Dict[str, str] = {
    "AAPL": "Tech", "MSFT": "Tech", "GOOGL": "Tech", "GOOG": "Tech",
    "META": "Tech", "NVDA": "Tech", "AMD": "Tech", "INTC": "Tech",
    "AVGO": "Tech", "QCOM": "Tech", "CRM": "Tech", "ORCL": "Tech",
    "ADBE": "Tech", "CSCO": "Tech", "IBM": "Tech", "NOW": "Tech",
    "INTU": "Tech", "MU": "Tech", "AMAT": "Tech", "LRCX": "Tech",
    "KLAC": "Tech", "MRVL": "Tech", "SNPS": "Tech", "CDNS": "Tech",
    "PANW": "Tech", "CRWD": "Tech", "FTNT": "Tech", "ZS": "Tech",
    "NET": "Tech", "DDOG": "Tech", "SNOW": "Tech", "PLTR": "Tech",
    "UBER": "Tech", "ABNB": "Tech", "DASH": "Tech", "SQ": "Tech",
    "SHOP": "Tech", "MELI": "Tech", "SE": "Tech",
    "AMZN": "ConsDisc", "TSLA": "ConsDisc", "NKE": "ConsDisc",
    "SBUX": "ConsDisc", "MCD": "ConsDisc", "HD": "ConsDisc",
    "LOW": "ConsDisc", "TGT": "ConsDisc", "COST": "ConsStap",
    "WMT": "ConsStap", "PG": "ConsStap", "KO": "ConsStap",
    "PEP": "ConsStap", "PM": "ConsStap", "MO": "ConsStap",
    "CL": "ConsStap", "EL": "ConsStap",
    "JPM": "Financials", "BAC": "Financials", "WFC": "Financials",
    "GS": "Financials", "MS": "Financials", "C": "Financials",
    "BLK": "Financials", "SCHW": "Financials", "AXP": "Financials",
    "V": "Financials", "MA": "Financials", "PYPL": "Financials",
    "JNJ": "Health", "UNH": "Health", "PFE": "Health", "MRK": "Health",
    "ABBV": "Health", "LLY": "Health", "TMO": "Health", "ABT": "Health",
    "BMY": "Health", "AMGN": "Health", "GILD": "Health", "ISRG": "Health",
    "REGN": "Health", "VRTX": "Health", "BIIB": "Health",
    "XOM": "Energy", "CVX": "Energy", "COP": "Energy", "SLB": "Energy",
    "EOG": "Energy", "MPC": "Energy", "VLO": "Energy", "OXY": "Energy",
    "LIN": "Materials", "APD": "Materials", "SHW": "Materials",
    "NEM": "Materials", "FCX": "Materials", "DOW": "Materials",
    "BA": "Industrials", "CAT": "Industrials", "GE": "Industrials",
    "HON": "Industrials", "UPS": "Industrials", "UNP": "Industrials",
    "DE": "Industrials", "RTX": "Industrials", "LMT": "Industrials",
    "NEE": "Utilities", "DUK": "Utilities", "SO": "Utilities",
    "AMT": "RealEstate", "PLD": "RealEstate", "CCI": "RealEstate",
    "T": "Comm", "VZ": "Comm", "CMCSA": "Comm", "DIS": "Comm",
    "NFLX": "Comm", "TMUS": "Comm", "CHTR": "Comm",
}


def _get_sector(ticker: str) -> str:
    return _SECTOR_MAP.get(ticker.upper(), "Other")


# ---------------------------------------------------------------------------
# 1. Correlation Bucketing
# ---------------------------------------------------------------------------

def _build_sector_buckets(tickers: List[str]) -> Dict[str, List[str]]:
    """Group tickers by sector."""
    buckets: Dict[str, List[str]] = {}
    for t in tickers:
        s = _get_sector(t)
        buckets.setdefault(s, []).append(t)
    return buckets


# ---------------------------------------------------------------------------
# 2. Kelly-Lite Sizing
# ---------------------------------------------------------------------------

def _kelly_fraction(breach_pct: float, credit_proxy: float, max_loss: float) -> float:
    """Compute a fractional Kelly weight for a single earnings IC.

    f* = (p * b - q) / b  where p = survival prob, b = credit/maxloss ratio, q = breach prob.
    Clamped to [0, 1] and half-Kelly'd for conservatism.
    """
    if max_loss <= 0 or credit_proxy <= 0:
        return 0.0
    p = max(0.0, min(1.0, 1.0 - breach_pct / 100.0))
    q = 1.0 - p
    b = credit_proxy / (max_loss - credit_proxy) if max_loss > credit_proxy else 0.01
    if b <= 0:
        return 0.0
    f_star = (p * b - q) / b
    return max(0.0, min(1.0, f_star * 0.5))


def _extract_best_row(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Find the best width-comparison row for the preferred EM."""
    dc = payload.get("deskConsensus") or {}
    emp = payload.get("emPreference") or {}
    preferred_em = emp.get("preferredEm") or dc.get("preferredEm") or 1.5

    wc = payload.get("widthComparison") or []
    best: Optional[Dict[str, Any]] = None
    for row in wc:
        if abs(row.get("emMult", 0) - preferred_em) < 0.01 and row.get("wingWidthPts") == 5.0:
            best = row
            break
    if best is None:
        for row in wc:
            if abs(row.get("emMult", 0) - preferred_em) < 0.01:
                best = row
                break
    if best is None and wc:
        best = wc[0]
    return best or {}


# ---------------------------------------------------------------------------
# 3. Regime Cap
# ---------------------------------------------------------------------------

_REGIME_CAPS = {
    "calm": 1.0,
    "low": 1.0,
    "risk-on": 1.0,
    "transitional": 0.85,
    "moderate": 0.85,
    "elevated": 0.75,
    "risk-off": 0.65,
    "stressed": 0.50,
    "stress": 0.50,
}


def _regime_budget_cap(regime_label: str) -> float:
    return _REGIME_CAPS.get(regime_label.lower().strip(), 0.85)


# ---------------------------------------------------------------------------
# 4. Concentration Limit
# ---------------------------------------------------------------------------

_MAX_SINGLE_NAME_PCT = 0.40
_SECTOR_CONCENTRATION_PENALTY = 0.15


# ---------------------------------------------------------------------------
# 5. Conflict Detection
# ---------------------------------------------------------------------------

def _detect_timing_conflicts(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Flag tickers sharing the same earnings session (AMC or BMO on same date)."""
    by_session: Dict[str, List[str]] = {}
    for e in entries:
        date_str = e.get("earningsDate") or ""
        timing = (e.get("earningsTiming") or "").upper()
        if date_str and timing:
            key = f"{date_str}_{timing}"
            by_session.setdefault(key, []).append(e.get("ticker", "?"))

    conflicts = []
    for key, tickers in by_session.items():
        if len(tickers) > 1:
            date_str, timing = key.rsplit("_", 1)
            conflicts.append({
                "date": date_str,
                "session": timing,
                "tickers": tickers,
                "note": f"{len(tickers)} names exit same {timing} session — manage sequentially",
            })
    return conflicts


# ---------------------------------------------------------------------------
# Deterministic Portfolio Allocation
# ---------------------------------------------------------------------------

def compute_portfolio_allocation(
    rankings: List[Dict[str, Any]],
    *,
    market_regime_label: str = "moderate",
) -> Dict[str, Any]:
    """Produce a deterministic capital allocation plan from enriched Engine 10 rankings.

    Returns allocation percentages, sector buckets, conflicts, and regime adjustment.
    """
    tradeable: List[Dict[str, Any]] = []

    for r in rankings:
        fp = r.get("fullPayload") or {}
        dc = fp.get("deskConsensus") or {}
        verdict = dc.get("verdict", "PASS")
        if verdict in ("TRADE", "LEAN_PASS"):
            tradeable.append(r)

    if not tradeable:
        return {
            "allocationCount": 0,
            "allocations": [],
            "regimeCap": _regime_budget_cap(market_regime_label),
            "regimeLabel": market_regime_label,
            "sectorBuckets": _build_sector_buckets([r.get("ticker", "") for r in rankings]),
            "conflicts": [],
            "skippedTickers": [
                {"ticker": r.get("ticker"), "reason": (r.get("fullPayload") or {}).get("deskConsensus", {}).get("verdict", "PASS")}
                for r in rankings
            ],
        }

    sector_buckets = _build_sector_buckets([r.get("ticker", "") for r in rankings])
    regime_cap = _regime_budget_cap(market_regime_label)

    raw_weights: Dict[str, float] = {}
    entry_details: Dict[str, Dict[str, Any]] = {}

    for r in tradeable:
        ticker = r.get("ticker", "")
        fp = r.get("fullPayload") or {}
        dc = fp.get("deskConsensus") or {}
        vrp = fp.get("vrpAnalysis") or {}
        emp = fp.get("emPreference") or {}
        best_row = _extract_best_row(fp)

        breach_pct = best_row.get("breachPct", 20.0)
        credit_proxy = best_row.get("creditProxy", 0.0)
        max_loss = best_row.get("maxLoss", 500.0)

        kf = _kelly_fraction(breach_pct, credit_proxy, max_loss)
        if dc.get("verdict") == "LEAN_PASS":
            kf *= 0.5

        raw_weights[ticker] = max(kf, 0.01)

        next_event = fp.get("nextEvent") or {}
        entry_details[ticker] = {
            "ticker": ticker,
            "verdict": dc.get("verdict"),
            "vrpScore": vrp.get("vrpScore"),
            "entryQuality": (fp.get("entryQuality") or {}).get("entryQuality"),
            "compositeScore": r.get("compositeScore"),
            "preferredEm": emp.get("preferredEm", dc.get("preferredEm")),
            "emLabel": emp.get("label", "standard"),
            "suggestedWing": best_row.get("wingWidthPts", 5.0),
            "breachPct": breach_pct,
            "creditProxy": credit_proxy,
            "maxLoss": max_loss,
            "sector": _get_sector(ticker),
            "earningsDate": next_event.get("earnDateNext"),
            "earningsTiming": next_event.get("timingPlanned"),
        }

    total_weight = sum(raw_weights.values())
    if total_weight <= 0:
        total_weight = 1.0

    normalized: Dict[str, float] = {
        t: w / total_weight for t, w in raw_weights.items()
    }

    # Concentration clamp
    clamped = True
    for _ in range(5):
        clamped = True
        excess = 0.0
        under_cap = []
        for t, w in normalized.items():
            if w > _MAX_SINGLE_NAME_PCT:
                excess += w - _MAX_SINGLE_NAME_PCT
                normalized[t] = _MAX_SINGLE_NAME_PCT
                clamped = False
            else:
                under_cap.append(t)
        if excess > 0 and under_cap:
            share = excess / len(under_cap)
            for t in under_cap:
                normalized[t] += share
        if clamped:
            break

    # Sector concentration penalty
    for sector, tickers in sector_buckets.items():
        sector_total = sum(normalized.get(t, 0) for t in tickers if t in normalized)
        if sector_total > 0.60 and len(tickers) > 1:
            penalty = _SECTOR_CONCENTRATION_PENALTY
            for t in tickers:
                if t in normalized:
                    normalized[t] *= (1.0 - penalty)
            re_total = sum(normalized.values())
            if re_total > 0:
                normalized = {t: w / re_total for t, w in normalized.items()}

    # Regime cap: scale all allocations
    allocations = []
    for t, w in normalized.items():
        pct = round(w * regime_cap * 100.0, 1)
        det = entry_details.get(t, {})
        det["allocationPct"] = pct
        det["rawKelly"] = round(raw_weights.get(t, 0), 4)
        allocations.append(det)

    allocations.sort(key=lambda x: -(x.get("allocationPct") or 0))

    total_deployed = round(sum(a["allocationPct"] for a in allocations), 1)
    cash_reserve = round(100.0 - total_deployed, 1)

    conflict_entries = [
        {"ticker": a["ticker"], "earningsDate": a.get("earningsDate"), "earningsTiming": a.get("earningsTiming")}
        for a in allocations
    ]
    conflicts = _detect_timing_conflicts(conflict_entries)

    skipped = []
    tradeable_tickers = {r.get("ticker") for r in tradeable}
    for r in rankings:
        t = r.get("ticker", "")
        if t not in tradeable_tickers:
            fp = r.get("fullPayload") or {}
            dc = fp.get("deskConsensus") or {}
            skipped.append({"ticker": t, "reason": dc.get("verdict", "PASS")})

    return {
        "allocationCount": len(allocations),
        "totalDeployed": total_deployed,
        "cashReserve": cash_reserve,
        "allocations": allocations,
        "regimeCap": regime_cap,
        "regimeLabel": market_regime_label,
        "sectorBuckets": sector_buckets,
        "conflicts": conflicts,
        "skippedTickers": skipped,
    }


# ---------------------------------------------------------------------------
# LLM integration
# ---------------------------------------------------------------------------

class _PortfolioAdvisorRateLimiter:
    def __init__(self, max_calls_per_minute: int = 3):
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


_rate_limiter = _PortfolioAdvisorRateLimiter()


def _get_openai_client():
    try:
        import openai
    except Exception:
        return None
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None
    try:
        return openai.OpenAI(api_key=api_key)
    except Exception:
        return None


def _load_prompt(filename: str) -> Optional[str]:
    path = _PROMPTS_DIR / filename
    if not path.exists():
        LOG.warning("Prompt file not found: %s", path)
        return None
    return path.read_text(encoding="utf-8").strip()


def _parse_llm_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        start = cleaned.find("{")
        if start < 0:
            return None
        depth = 0
        for i in range(start, len(cleaned)):
            if cleaned[i] == "{":
                depth += 1
            elif cleaned[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(cleaned[start : i + 1])
                    except json.JSONDecodeError:
                        return None
    return None


def _load_todays_dms() -> Optional[Dict[str, Any]]:
    store = get_store_optional()
    if store is None:
        return None
    today_str = dt.date.today().strftime("%Y-%m-%d")
    dms = load_dms(today_str, store)
    if dms is None:
        return None
    return dms.to_dict()


def _extract_dms_context(dms_dict: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not dms_dict:
        return {}
    return {
        "regime": dms_dict.get("regime", {}),
        "vol_state": dms_dict.get("vol_state", {}),
        "composite_stress": (dms_dict.get("cross_asset_stress") or {}).get("composite_score"),
        "composite_label": (dms_dict.get("cross_asset_stress") or {}).get("composite_label"),
        "active_themes": dms_dict.get("news_themes", []),
    }


def _build_ticker_summary(alloc: Dict[str, Any], full_payload: Dict[str, Any]) -> Dict[str, Any]:
    """Compact per-ticker summary for the LLM context window."""
    summary_data = full_payload.get("summary") or {}
    current = full_payload.get("current") or {}
    vrp = full_payload.get("vrpAnalysis") or {}
    dc = full_payload.get("deskConsensus") or {}
    eq = full_payload.get("entryQuality") or {}
    next_ev = full_payload.get("nextEvent") or {}

    return {
        "ticker": alloc.get("ticker"),
        "allocationPct": alloc.get("allocationPct"),
        "verdict": alloc.get("verdict"),
        "vrpScore": vrp.get("vrpScore"),
        "vrpConfidence": vrp.get("confidence"),
        "meanRatio": vrp.get("meanRatio"),
        "ivElevation": vrp.get("ivElevation"),
        "entryQuality": eq.get("entryQuality"),
        "compositeScore": alloc.get("compositeScore"),
        "preferredEm": alloc.get("preferredEm"),
        "emLabel": alloc.get("emLabel"),
        "suggestedWing": alloc.get("suggestedWing"),
        "breachPct": alloc.get("breachPct"),
        "creditProxy": alloc.get("creditProxy"),
        "maxLoss": alloc.get("maxLoss"),
        "sector": alloc.get("sector"),
        "earningsDate": alloc.get("earningsDate") or next_ev.get("earnDateNext"),
        "earningsTiming": alloc.get("earningsTiming") or next_ev.get("timingPlanned"),
        "stockPrice": current.get("stockPrice"),
        "impliedMovePct": current.get("impliedMovePct"),
        "breachRateOverall": summary_data.get("breachRate"),
        "riskLevel": dc.get("riskLevel"),
        "reasons": dc.get("reasons", []),
    }


_PORTFOLIO_REQUIRED_KEYS = {
    "allocationPlan", "portfolioRationale", "correlationNote",
    "regimeAdjustment", "deskNote",
}


def generate_portfolio_advisor(
    *,
    rankings: List[Dict[str, Any]],
    deterministic_allocation: Dict[str, Any],
    flags: Optional[FeatureFlags] = None,
) -> Dict[str, Any]:
    """Run the E10 Portfolio Advisor LLM: review deterministic allocation and produce game plan."""
    f = flags or get_flags()

    fallback: Dict[str, Any] = {
        "_source": "fallback",
        "allocationPlan": deterministic_allocation.get("allocations", []),
        "portfolioRationale": "Deterministic allocation (LLM unavailable).",
        "correlationNote": None,
        "regimeAdjustment": None,
        "conflictResolution": None,
        "deskNote": "Using quant model output directly.",
    }

    if not getattr(f, "E10_ADVISOR_ENABLED", True):
        fallback["_fallback_reason"] = "E10 Advisor disabled"
        return fallback

    system_prompt = _load_prompt("e10_portfolio_advisor.txt")
    if not system_prompt:
        fallback["_fallback_reason"] = "Prompt file missing"
        return fallback

    if not _rate_limiter.acquire():
        fallback["_fallback_reason"] = "Rate limited. Wait a moment and try again."
        return fallback

    client = _get_openai_client()
    if client is None:
        fallback["_fallback_reason"] = "OpenAI client unavailable"
        return fallback

    dms_dict = _load_todays_dms()

    trade_journal = None
    try:
        from backend.e1_earnings_trades import compute_e1_trade_performance_digest
        perf_digest = compute_e1_trade_performance_digest()
        if perf_digest.get("hasData"):
            trade_journal = {
                "totalClosed": perf_digest.get("totalClosed"),
                "winRate": perf_digest.get("winRate"),
                "avgPnl": perf_digest.get("avgPnl"),
                "riskTendency": perf_digest.get("riskTendency"),
                "byVrpBucket": perf_digest.get("byVrpBucket"),
                "byEm": perf_digest.get("byEm"),
                "byRegime": perf_digest.get("byRegime"),
                "verdictCalibration": perf_digest.get("verdictCalibration"),
                "vrpCalibration": perf_digest.get("vrpCalibration"),
                "patternInsights": perf_digest.get("patternInsights"),
            }
    except Exception as e:
        LOG.debug("E10 trade journal unavailable: %s", e)

    portfolio_journal = None
    try:
        from backend.e10_portfolio_sessions import compute_e10_portfolio_digest
        pj = compute_e10_portfolio_digest()
        if pj and pj.get("hasData"):
            portfolio_journal = pj
    except Exception as e:
        LOG.debug("E10 portfolio journal unavailable: %s", e)

    payload_map: Dict[str, Dict[str, Any]] = {}
    for r in rankings:
        payload_map[r.get("ticker", "")] = r.get("fullPayload") or {}

    ticker_summaries = []
    for alloc in deterministic_allocation.get("allocations", []):
        fp = payload_map.get(alloc.get("ticker", ""), {})
        ticker_summaries.append(_build_ticker_summary(alloc, fp))

    context: Dict[str, Any] = {
        "deterministicAllocation": {
            "totalDeployed": deterministic_allocation.get("totalDeployed"),
            "cashReserve": deterministic_allocation.get("cashReserve"),
            "regimeCap": deterministic_allocation.get("regimeCap"),
            "regimeLabel": deterministic_allocation.get("regimeLabel"),
            "sectorBuckets": deterministic_allocation.get("sectorBuckets"),
            "conflicts": deterministic_allocation.get("conflicts"),
            "skippedTickers": deterministic_allocation.get("skippedTickers"),
        },
        "tickerSummaries": ticker_summaries,
        "market": _extract_dms_context(dms_dict),
    }
    if trade_journal:
        context["tradeJournal"] = trade_journal
    if portfolio_journal:
        context["portfolioJournal"] = portfolio_journal

    payload_str = json.dumps(context, default=str)
    if len(payload_str) > 30000:
        payload_str = payload_str[:30000]

    model = str(getattr(f, "E10_ADVISOR_MODEL", None) or f.E1_ADVISOR_MODEL or "gpt-5.5").strip()

    try:
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": payload_str},
            ],
            temperature=1,
            max_completion_tokens=2500,
            timeout=60,
            response_format={"type": "json_object"},
        )

        content = response.choices[0].message.content.strip()
        result = _parse_llm_json(content)

        if result is None or not _PORTFOLIO_REQUIRED_KEYS.issubset(set(result.keys())):
            LOG.warning("E10 portfolio advisor: LLM response missing required keys")
            fallback["_fallback_reason"] = "LLM returned invalid JSON"
            return fallback

        result["_source"] = "llm"
        result["_model"] = model
        result["_generatedAt"] = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return result

    except Exception as e:
        reason = f"{type(e).__name__}: {e}"
        LOG.warning("E10 portfolio advisor LLM call failed: %s", reason)
        fallback["_fallback_reason"] = reason
        return fallback
