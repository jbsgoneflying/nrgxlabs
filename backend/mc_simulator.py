from __future__ import annotations

import datetime as dt
import hashlib
import json
import math
import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

from cachetools import TTLCache

from backend.config import FeatureFlags
from backend.wing_recommendation import compute_wing_recommendation


# Match /api/breach caching cadence; MC runs are deterministic but can still be cached for speed.
_mc_cache: TTLCache = TTLCache(maxsize=2048, ttl=6 * 60 * 60)
_mc_cache_lock = threading.Lock()


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _stable_json_dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _to_float(v: Any) -> Optional[float]:
    try:
        if v is None:
            return None
        f = float(v)
        if not math.isfinite(f):
            return None
        return f
    except (TypeError, ValueError):
        return None


def _quarter_key(date_str: str) -> Optional[str]:
    try:
        d = dt.date.fromisoformat(str(date_str)[:10])
    except Exception:
        return None
    q = ((d.month - 1) // 3) + 1
    return f"Q{q}"


@dataclass(frozen=True)
class ShockRow:
    earnDate: str
    pricingDateUsed: str
    impliedMovePct: float
    signedMovePct: float
    quarterKey: str
    regimeLabel: str
    tradeGate: str

    @property
    def s_signed(self) -> float:
        return float(self.signedMovePct) / float(self.impliedMovePct)

    def to_minimal_tuple(self) -> tuple:
        return (
            str(self.earnDate)[:10],
            str(self.pricingDateUsed)[:10],
            round(float(self.impliedMovePct), 6),
            round(float(self.signedMovePct), 6),
            str(self.quarterKey),
            str(self.regimeLabel),
            str(self.tradeGate),
        )


def build_shock_pool(
    *,
    events: List[Dict[str, Any]],
    min_implied_move_pct: float,
) -> Tuple[List[ShockRow], Dict[str, int]]:
    """
    Build the empirical earnings shock pool from payload events.

    Hard rules:
    - no lookahead: use only per-event values already computed at that event time
    - implied floor: exclude impliedMovePct < min_implied_move_pct to avoid S blow-ups
    """
    pool: List[ShockRow] = []
    excluded = {
        "missing_pricingDateUsed": 0,
        "missing_earnDate": 0,
        "missing_impliedMovePct": 0,
        "missing_signedMovePct": 0,
        "implied_below_floor": 0,
        "missing_regimeAtEvent": 0,
        "missing_quarterKey": 0,
    }

    floor = float(min_implied_move_pct)
    for e in events or []:
        earn_date = str(e.get("earnDate") or "")[:10]
        if not earn_date:
            excluded["missing_earnDate"] += 1
            continue
        pricing = str(e.get("pricingDateUsed") or "")[:10]
        if not pricing:
            excluded["missing_pricingDateUsed"] += 1
            continue
        implied = _to_float(e.get("impliedMovePct"))
        if implied is None:
            excluded["missing_impliedMovePct"] += 1
            continue
        if implied < floor:
            excluded["implied_below_floor"] += 1
            continue
        signed = _to_float(e.get("signedMovePct"))
        if signed is None:
            excluded["missing_signedMovePct"] += 1
            continue
        qk = _quarter_key(earn_date)
        if not qk:
            excluded["missing_quarterKey"] += 1
            continue
        rge = e.get("regimeAtEvent") if isinstance(e.get("regimeAtEvent"), dict) else None
        if not rge:
            excluded["missing_regimeAtEvent"] += 1
            continue
        label = str(rge.get("label") or "")
        gate = str(rge.get("tradeGate") or "OK")
        if not label:
            excluded["missing_regimeAtEvent"] += 1
            continue

        pool.append(
            ShockRow(
                earnDate=earn_date,
                pricingDateUsed=pricing,
                impliedMovePct=float(implied),
                signedMovePct=float(signed),
                quarterKey=qk,
                regimeLabel=label,
                tradeGate=gate if gate else "OK",
            )
        )

    # Stable ordering: newest events first; ties broken by pricingDateUsed then signed value.
    pool.sort(key=lambda r: (r.earnDate, r.pricingDateUsed, r.signedMovePct), reverse=True)
    return pool, excluded


def shock_pool_key(
    *,
    ticker: str,
    n: int,
    years: int,
    k: float,
    flags_fingerprint: tuple,
    pool: List[ShockRow],
) -> str:
    minimal = [r.to_minimal_tuple() for r in pool]
    payload = {
        "ticker": str(ticker).upper(),
        "params": {"n": int(n), "years": int(years), "k": float(k)},
        "flags": list(flags_fingerprint),
        "rows": minimal,
    }
    return _sha256_hex(_stable_json_dumps(payload))


def _conditioning_requested(flags: FeatureFlags) -> List[str]:
    out: List[str] = []
    if flags.MC_ENABLE_CONDITION_ON_QUARTER:
        out.append("quarter")
    if flags.MC_ENABLE_CONDITION_ON_REGIME:
        out.append("regime")
    if flags.MC_ENABLE_CONDITION_ON_TRADE_GATE:
        out.append("gate")
    return out


def _apply_conditioning_hierarchy(
    *,
    pool: List[ShockRow],
    want_quarter: Optional[str],
    want_regime: Optional[str],
    want_gate: Optional[str],
    min_pool: int,
) -> Tuple[List[ShockRow], str]:
    """
    Conditioning hierarchy (strict):
      quarter+regime+gate -> quarter+regime -> regime -> unconditioned
    """

    def _filt(pred) -> List[ShockRow]:
        return [r for r in pool if pred(r)]

    # 1) quarter+regime+gate
    if want_quarter and want_regime and want_gate:
        xs = _filt(lambda r: r.quarterKey == want_quarter and r.regimeLabel == want_regime and r.tradeGate == want_gate)
        if len(xs) >= min_pool:
            return xs, "quarter+regime+gate"

    # 2) quarter+regime
    if want_quarter and want_regime:
        xs = _filt(lambda r: r.quarterKey == want_quarter and r.regimeLabel == want_regime)
        if len(xs) >= min_pool:
            return xs, "quarter+regime"

    # 3) regime
    if want_regime:
        xs = _filt(lambda r: r.regimeLabel == want_regime)
        if len(xs) >= min_pool:
            return xs, "regime"

    # 4) unconditioned
    return pool, "unconditioned"


def _seed_from_key(mc_key: Any, global_seed: int) -> int:
    digest = hashlib.sha256(repr(mc_key).encode("utf-8")).digest()
    # 64-bit seed, stable across runs.
    seed64 = int.from_bytes(digest[:8], "big", signed=False)
    return int(seed64 ^ int(global_seed))


def _weighted_indices(n: int, half_life: int) -> List[float]:
    # age=0 newest has weight=1.0; weight halves every `half_life` events.
    hl = max(1, int(half_life))
    ln2 = math.log(2.0)
    return [math.exp(-(ln2 * i) / hl) for i in range(n)]


def _intrinsic_put_spread(*, s: float, k_short: float, k_long: float) -> float:
    # value >=0 at open (ignoring credit); defined risk if k_long < k_short
    return max(0.0, k_short - s) - max(0.0, k_long - s)


def _intrinsic_call_spread(*, s: float, k_short: float, k_long: float) -> float:
    return max(0.0, s - k_short) - max(0.0, s - k_long)


def _cvar95(losses: List[float]) -> Optional[float]:
    if not losses:
        return None
    xs = sorted(float(x) for x in losses)
    if not xs:
        return None
    n = len(xs)
    # worst 5% tail; at least 1 element
    tail_n = max(1, int(math.ceil(0.05 * n)))
    tail = xs[-tail_n:]
    return float(sum(tail) / len(tail))


def _pctiles(xs: List[float], ps: List[float]) -> Dict[str, float]:
    if not xs:
        return {}
    ys = sorted(float(x) for x in xs)
    n = len(ys)
    out: Dict[str, float] = {}
    for p in ps:
        pp = float(p)
        if pp <= 0:
            v = ys[0]
        elif pp >= 100:
            v = ys[-1]
        else:
            # linear interpolation
            pos = (pp / 100.0) * (n - 1)
            lo = int(math.floor(pos))
            hi = int(math.ceil(pos))
            if lo == hi:
                v = ys[lo]
            else:
                w = pos - lo
                v = (1.0 - w) * ys[lo] + w * ys[hi]
        out[str(int(pp))] = float(v)
    return out


def _structure_from_trade_builder(tb: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        put = tb.get("put") if isinstance(tb.get("put"), dict) else {}
        call = tb.get("call") if isinstance(tb.get("call"), dict) else {}
        ps = _to_float(put.get("shortStrike"))
        pl = _to_float(put.get("longStrike"))
        cs = _to_float(call.get("shortStrike"))
        cl = _to_float(call.get("longStrike"))
        if ps is None or pl is None or cs is None or cl is None:
            return None
        return {
            "kind": "STRIKES",
            "putShort": float(ps),
            "putLong": float(pl),
            "callShort": float(cs),
            "callLong": float(cl),
            "totalCredit": _to_float(tb.get("totalCredit")),
        }
    except Exception:
        return None


def _structure_from_distances(
    *,
    spot: float,
    implied_move_pct: float,
    put_mult: float,
    call_mult: float,
    wing_width_dollars: float,
) -> Optional[Dict[str, Any]]:
    if spot <= 0 or implied_move_pct <= 0:
        return None
    em = spot * (implied_move_pct / 100.0)
    put_short = spot - em * put_mult
    call_short = spot + em * call_mult
    w = float(wing_width_dollars)
    return {
        "kind": "DISTANCES_EST",
        "putShort": float(put_short),
        "putLong": float(put_short - w),
        "callShort": float(call_short),
        "callLong": float(call_short + w),
        "totalCredit": None,
    }


def run_monte_carlo(
    *,
    ticker: str,
    params: Dict[str, Any],
    flags: FeatureFlags,
    current: Dict[str, Any],
    next_event: Dict[str, Any],
    regime: Dict[str, Any],
    wing_recommendation: Dict[str, Any],
    events: List[Dict[str, Any]],
    trade_builder: Optional[Dict[str, Any]],
    event_risk: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Compute additive MC fields. Caller must ensure flags.ENABLE_MONTE_CARLO_EARNINGS is True.
    """
    notes: List[str] = []

    spot = _to_float(current.get("stockPrice"))
    imp_planned = _to_float(next_event.get("impliedMovePctPlanned"))
    if spot is None or spot <= 0:
        return {"notes": ["MC unavailable: missing current stockPrice."], "nSims": 0}
    if imp_planned is None or imp_planned <= 0:
        return {"notes": ["MC unavailable: missing nextEvent impliedMovePctPlanned."], "nSims": 0}

    pool, excluded = build_shock_pool(events=events, min_implied_move_pct=float(flags.MC_MIN_IMPLIED_MOVE_PCT))
    if not pool:
        return {
            "nSims": 0,
            "pool": {"sizeUsed": 0, "excludedCounts": excluded},
            "notes": ["MC unavailable: shock pool empty after exclusions."],
        }

    req = _conditioning_requested(flags)
    want_q = _quarter_key(str(current.get("asOfDate") or "")[:10]) if flags.MC_ENABLE_CONDITION_ON_QUARTER else None
    want_regime = str(regime.get("label") or "") if flags.MC_ENABLE_CONDITION_ON_REGIME else None
    want_gate = str((regime.get("guidance") or {}).get("tradeGate") or regime.get("tradeGate") or "") if flags.MC_ENABLE_CONDITION_ON_TRADE_GATE else None
    min_pool = max(1, int(flags.MC_MIN_POOL))

    used_pool, conditioning_used = _apply_conditioning_hierarchy(
        pool=pool,
        want_quarter=want_q,
        want_regime=want_regime,
        want_gate=want_gate,
        min_pool=min_pool,
    )
    if conditioning_used != "unconditioned" and len(used_pool) < min_pool:
        notes.append("Conditioned pool too small; falling back to unconditioned.")
        used_pool = pool
        conditioning_used = "unconditioned"

    # Determine structure.
    structure = _structure_from_trade_builder(trade_builder or {}) if trade_builder else None
    if structure is None:
        put_mult = _to_float(wing_recommendation.get("putWingMultiple") or wing_recommendation.get("baseWingMultiple"))
        call_mult = _to_float(wing_recommendation.get("callWingMultiple") or wing_recommendation.get("baseWingMultiple"))
        if put_mult is None or call_mult is None:
            return {
                "nSims": 0,
                "pool": {"sizeUsed": len(used_pool), "excludedCounts": excluded},
                "notes": ["MC unavailable: missing wing multipliers and no strike-based structure."],
            }
        # Optional: event-risk widening (risk-only). Does not change ORATS EOD implied move anchoring.
        bump_pct = 0.0
        if bool(flags.BENZINGA_EVENT_RISK_AFFECTS_MC) and isinstance(event_risk, dict) and event_risk.get("enabled") is True:
            try:
                s = float(event_risk.get("score01"))
                hi = float(flags.BENZINGA_EVENT_RISK_HIGH_THRESHOLD)
                ca = float(flags.BENZINGA_EVENT_RISK_CAUTION_THRESHOLD)
                max_bump = max(0.0, float(flags.BENZINGA_EVENT_RISK_MC_WING_BUMP_MAX_PCT))
                if s > ca and ca < 1.0:
                    bump_pct = ((s - ca) / (1.0 - ca)) * max_bump
                    bump_pct = max(0.0, min(max_bump, bump_pct))
                # Only apply when risk is meaningfully elevated
                if s < hi:
                    bump_pct = min(bump_pct, max_bump * 0.50)
            except Exception:
                bump_pct = 0.0

        if bump_pct > 0:
            notes.append(f"Event-risk widening applied: +{bump_pct:.1f}% to wing distances (risk-only).")
            put_mult = float(put_mult) * (1.0 + bump_pct / 100.0)
            call_mult = float(call_mult) * (1.0 + bump_pct / 100.0)

        structure = _structure_from_distances(
            spot=float(spot),
            implied_move_pct=float(imp_planned),
            put_mult=float(put_mult),
            call_mult=float(call_mult),
            wing_width_dollars=float(flags.MC_DEFAULT_WING_WIDTH_DOLLARS),
        )
        notes.append("Structure estimated from distances (no chain strikes).")

    if structure is None:
        return {
            "nSims": 0,
            "pool": {"sizeUsed": len(used_pool), "excludedCounts": excluded},
            "notes": ["MC unavailable: unable to build IC structure."],
        }

    # Build deterministic cache key.
    n_sims = max(0, int(flags.MC_N_SIMS))
    if n_sims <= 0:
        return {"nSims": 0, "notes": ["MC disabled: MC_N_SIMS<=0."]}

    sp_key = shock_pool_key(
        ticker=str(ticker).upper(),
        n=int(params.get("n") or 0),
        years=int(params.get("years") or 0),
        k=float(params.get("k") or 0.0),
        flags_fingerprint=flags.cache_fingerprint(),
        pool=pool,
    )
    structure_key = (
        structure.get("kind"),
        round(float(structure["putShort"]), 6),
        round(float(structure["putLong"]), 6),
        round(float(structure["callShort"]), 6),
        round(float(structure["callLong"]), 6),
        None if structure.get("totalCredit") is None else round(float(structure["totalCredit"]), 6),
    )
    conditioning_key = {
        "requested": req,
        "used": conditioning_used,
        "want": {"quarter": want_q, "regime": want_regime, "gate": want_gate},
    }
    conditioning_key_s = _stable_json_dumps(conditioning_key)
    mc_key = (
        "mc",
        str(ticker).upper(),
        str(current.get("asOfDate") or "")[:10],
        round(float(spot), 6),
        round(float(imp_planned), 6),
        structure_key,
        conditioning_key_s,
        int(n_sims),
        int(flags.MC_GLOBAL_SEED),
        sp_key,
        flags.cache_fingerprint(),
    )

    with _mc_cache_lock:
        cached = _mc_cache.get(mc_key)
    if cached is not None:
        return cached

    seed = _seed_from_key(mc_key, int(flags.MC_GLOBAL_SEED))

    # Deterministic RNG: use stdlib Random (fast, stable).
    import random

    rng = random.Random(seed)

    # Sampling weights (optional).
    weights = None
    if flags.MC_ENABLE_RECENCY_WEIGHTING:
        weights = _weighted_indices(len(used_pool), int(flags.MC_RECENCY_HALFLIFE_EVENTS))

    # Simulation loop (gap-at-open only).
    put_short = float(structure["putShort"])
    put_long = float(structure["putLong"])
    call_short = float(structure["callShort"])
    call_long = float(structure["callLong"])

    breaches_put = 0
    breaches_call = 0
    breaches_either = 0

    loss_put: List[float] = []
    loss_call: List[float] = []
    loss_total: List[float] = []
    p_open_sims: List[float] = []

    # Pre-extract S to reduce attribute access overhead.
    s_vals = [r.s_signed for r in used_pool]
    s_min = min(s_vals) if s_vals else None
    s_max = max(s_vals) if s_vals else None

    # Required standardized shock (S) to breach each side at open:
    # P_open = spot * (1 + (S * imp_planned)/100) => S_req = ((K/spot - 1) * 100) / imp_planned
    def _s_req(k_level: float) -> Optional[float]:
        if spot is None or spot <= 0 or imp_planned is None or imp_planned <= 0:
            return None
        return ((float(k_level) / float(spot) - 1.0) * 100.0) / float(imp_planned)

    s_req_put = _s_req(put_short)
    s_req_call = _s_req(call_short)
    for _ in range(n_sims):
        if weights is None:
            idx = rng.randrange(len(s_vals))
        else:
            idx = rng.choices(range(len(s_vals)), weights=weights, k=1)[0]
        s_signed = float(s_vals[idx])
        move_sim_pct = s_signed * float(imp_planned)
        p_open = float(spot) * (1.0 + move_sim_pct / 100.0)
        p_open_sims.append(p_open)

        b_put = p_open <= put_short
        b_call = p_open >= call_short
        if b_put:
            breaches_put += 1
        if b_call:
            breaches_call += 1
        if b_put or b_call:
            breaches_either += 1

        lp = _intrinsic_put_spread(s=p_open, k_short=put_short, k_long=put_long)
        lc = _intrinsic_call_spread(s=p_open, k_short=call_short, k_long=call_long)
        lt = lp + lc
        loss_put.append(lp)
        loss_call.append(lc)
        loss_total.append(lt)

    def _prob(x: int) -> float:
        return float(x) / float(n_sims) if n_sims > 0 else 0.0

    breach_prob_upper_bound_pct: Optional[float] = None
    # Explainable notes when simulated breach count is zero.
    if breaches_either == 0 and s_min is not None and s_max is not None and s_req_put is not None and s_req_call is not None:
        notes.append(
            "No simulated breaches observed. "
            f"Historical S range [{s_min:.2f}, {s_max:.2f}] vs required S<= {s_req_put:.2f} (put) or S>= {s_req_call:.2f} (call)."
        )
        # Simple empirical upper bound: if 0 breaches in N sims, p < ~3/N (rule-of-three) at ~95% confidence.
        breach_prob_upper_bound_pct = (3.0 / float(n_sims)) * 100.0
        notes.append(f"Empirical breach probability is very small (0/{n_sims}); upper bound ~{breach_prob_upper_bound_pct:.2f}%.")

    out = {
        "nSims": int(n_sims),
        "seed": int(seed),
        "notes": [
            "Simulates close→open earnings gap only (no intraday path).",
            *notes,
        ],
        "pool": {
            "sizeUsed": int(len(used_pool)),
            "sizeUnconditioned": int(len(pool)),
            "conditioningRequested": req,
            "conditioningUsed": conditioning_used,
            "minPool": int(min_pool),
            "recencyWeighting": bool(flags.MC_ENABLE_RECENCY_WEIGHTING),
            "excludedCounts": excluded,
            "shockPoolKey": sp_key,
        },
        "breachProb": {
            "put": round(_prob(breaches_put), 6),
            "call": round(_prob(breaches_call), 6),
            "either": round(_prob(breaches_either), 6),
        },
        "expectedLoss": {
            "put": round(float(sum(loss_put) / n_sims), 6),
            "call": round(float(sum(loss_call) / n_sims), 6),
            "total": round(float(sum(loss_total) / n_sims), 6),
        },
        "cvar95": {
            "put": None if _cvar95(loss_put) is None else round(float(_cvar95(loss_put) or 0.0), 6),
            "call": None if _cvar95(loss_call) is None else round(float(_cvar95(loss_call) or 0.0), 6),
            "total": None if _cvar95(loss_total) is None else round(float(_cvar95(loss_total) or 0.0), 6),
        },
        "structure": {
            "kind": structure.get("kind"),
            "putShort": round(put_short, 4),
            "putLong": round(put_long, 4),
            "callShort": round(call_short, 4),
            "callLong": round(call_long, 4),
        },
        "diagnostics": {
            "sRange": None if (s_min is None or s_max is None) else {"min": round(float(s_min), 4), "max": round(float(s_max), 4)},
            "sRequiredForBreach": {
                "put": None if s_req_put is None else round(float(s_req_put), 4),
                "call": None if s_req_call is None else round(float(s_req_call), 4),
            },
            "openPricePctiles": _pctiles(p_open_sims, [1, 5, 50, 95, 99]) if p_open_sims else None,
            "breachProbUpperBoundPct": None if breach_prob_upper_bound_pct is None else round(float(breach_prob_upper_bound_pct), 4),
        },
    }

    with _mc_cache_lock:
        _mc_cache[mc_key] = out
    return out


def optimize_wings_risk_only(
    *,
    ticker: str,
    params: Dict[str, Any],
    flags: FeatureFlags,
    current: Dict[str, Any],
    next_event: Dict[str, Any],
    regime: Dict[str, Any],
    wing_recommendation: Dict[str, Any],
    events: List[Dict[str, Any]],
    stability: Optional[Dict[str, Any]] = None,
    event_risk: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Risk-only wing optimization around the heuristic EM-multiples.

    This intentionally does NOT optimize on credit unless real chain pricing exists (future phase).
    """
    spot = _to_float(current.get("stockPrice"))
    imp_planned = _to_float(next_event.get("impliedMovePctPlanned"))
    if spot is None or spot <= 0 or imp_planned is None or imp_planned <= 0:
        return {"mode": "RISK_ONLY", "notes": ["Optimization unavailable: missing spot or impliedMovePctPlanned."]}

    pool, excluded = build_shock_pool(events=events, min_implied_move_pct=float(flags.MC_MIN_IMPLIED_MOVE_PCT))
    if not pool:
        return {"mode": "RISK_ONLY", "notes": ["Optimization unavailable: shock pool empty."], "pool": {"excludedCounts": excluded}}

    req = _conditioning_requested(flags)
    want_q = _quarter_key(str(current.get("asOfDate") or "")[:10]) if flags.MC_ENABLE_CONDITION_ON_QUARTER else None
    want_regime = str(regime.get("label") or "") if flags.MC_ENABLE_CONDITION_ON_REGIME else None
    want_gate = str((regime.get("guidance") or {}).get("tradeGate") or regime.get("tradeGate") or "") if flags.MC_ENABLE_CONDITION_ON_TRADE_GATE else None
    min_pool = max(1, int(flags.MC_MIN_POOL))
    used_pool, conditioning_used = _apply_conditioning_hierarchy(pool=pool, want_quarter=want_q, want_regime=want_regime, want_gate=want_gate, min_pool=min_pool)

    base_put = _to_float(wing_recommendation.get("putWingMultiple") or wing_recommendation.get("baseWingMultiple"))
    base_call = _to_float(wing_recommendation.get("callWingMultiple") or wing_recommendation.get("baseWingMultiple"))
    if base_put is None or base_call is None:
        return {"mode": "RISK_ONLY", "notes": ["Optimization unavailable: missing heuristic wing multiples."]}

    # Optional: event-risk widening on the heuristic center (risk-only).
    bump_pct = 0.0
    if bool(flags.BENZINGA_EVENT_RISK_AFFECTS_MC) and isinstance(event_risk, dict) and event_risk.get("enabled") is True:
        try:
            s = float(event_risk.get("score01"))
            ca = float(flags.BENZINGA_EVENT_RISK_CAUTION_THRESHOLD)
            max_bump = max(0.0, float(flags.BENZINGA_EVENT_RISK_MC_WING_BUMP_MAX_PCT))
            if s > ca and ca < 1.0:
                bump_pct = ((s - ca) / (1.0 - ca)) * max_bump
                bump_pct = max(0.0, min(max_bump, bump_pct))
        except Exception:
            bump_pct = 0.0
    if bump_pct > 0:
        base_put = float(base_put) * (1.0 + bump_pct / 100.0)
        base_call = float(base_call) * (1.0 + bump_pct / 100.0)

    # Optional stability-based cap.
    cap_mode = None
    cap_adj = None
    if stability and isinstance(stability.get("asymmetryCap"), dict):
        cap_mode = stability["asymmetryCap"].get("mode")
        cap_adj = _to_float(stability["asymmetryCap"].get("maxAdj"))

    max_delta = max(0.0, float(flags.MC_OPT_MAX_MULT_DELTA))
    step = max(0.01, float(flags.MC_OPT_STEP))
    n_sims = max(200, int(flags.MC_N_SIMS))
    # Guardrail: keep runtime bounded if user pushes sims too high.
    # We cap optimization sims while keeping main MC sims unchanged.
    opt_sims = min(n_sims, 2000)
    notes: List[str] = []
    if opt_sims < n_sims:
        notes.append(f"Optimization uses {opt_sims} sims for speed (main MC uses {n_sims}).")

    # Sampling weights (optional).
    weights = None
    if flags.MC_ENABLE_RECENCY_WEIGHTING:
        weights = _weighted_indices(len(used_pool), int(flags.MC_RECENCY_HALFLIFE_EVENTS))
    s_vals = [r.s_signed for r in used_pool]

    # Base seed (deterministic); candidate seeds are derived from this.
    sp_key = shock_pool_key(
        ticker=str(ticker).upper(),
        n=int(params.get("n") or 0),
        years=int(params.get("years") or 0),
        k=float(params.get("k") or 0.0),
        flags_fingerprint=flags.cache_fingerprint(),
        pool=pool,
    )
    base_key = (
        "mcopt",
        str(ticker).upper(),
        str(current.get("asOfDate") or "")[:10],
        round(float(spot), 6),
        round(float(imp_planned), 6),
        conditioning_used,
        int(opt_sims),
        int(flags.MC_GLOBAL_SEED),
        sp_key,
        flags.cache_fingerprint(),
    )
    base_seed = _seed_from_key(base_key, int(flags.MC_GLOBAL_SEED))

    em = float(spot) * (float(imp_planned) / 100.0)
    wing_w = float(flags.MC_DEFAULT_WING_WIDTH_DOLLARS)

    max_breach_either = float(flags.MC_MAX_BREACH_EITHER_PCT) / 100.0
    max_cvar = float(flags.MC_MAX_CVAR95_TOTAL)
    use_cvar_budget = max_cvar > 0.0

    def _eval_candidate(put_mult: float, call_mult: float) -> Dict[str, float]:
        # Deterministic candidate seed
        cand_key = (base_seed, round(put_mult, 6), round(call_mult, 6))
        seed = _seed_from_key(cand_key, int(flags.MC_GLOBAL_SEED))

        import random

        rng = random.Random(seed)
        breaches_either = 0
        losses: List[float] = []
        put_short = float(spot) - em * float(put_mult)
        put_long = put_short - wing_w
        call_short = float(spot) + em * float(call_mult)
        call_long = call_short + wing_w

        for _ in range(int(opt_sims)):
            if weights is None:
                idx = rng.randrange(len(s_vals))
            else:
                idx = rng.choices(range(len(s_vals)), weights=weights, k=1)[0]
            move_sim_pct = float(s_vals[idx]) * float(imp_planned)
            p_open = float(spot) * (1.0 + move_sim_pct / 100.0)
            b_put = p_open <= put_short
            b_call = p_open >= call_short
            if b_put or b_call:
                breaches_either += 1
            lp = _intrinsic_put_spread(s=p_open, k_short=put_short, k_long=put_long)
            lc = _intrinsic_call_spread(s=p_open, k_short=call_short, k_long=call_long)
            losses.append(lp + lc)

        prob_either = float(breaches_either) / float(opt_sims)
        cvar = _cvar95(losses) or 0.0
        return {"breachProbEither": prob_either, "cvar95Total": float(cvar)}

    def _passes_caps(put_mult: float, call_mult: float) -> bool:
        if cap_mode == "FORCE_SYMMETRIC":
            return abs(float(put_mult) - float(call_mult)) <= 1e-9
        if cap_mode == "CAP_ASYMMETRY" and cap_adj is not None and cap_adj >= 0:
            mid = 0.5 * (float(put_mult) + float(call_mult))
            if mid <= 0:
                return False
            return (abs(float(put_mult) - mid) / mid <= float(cap_adj) + 1e-12) and (abs(float(call_mult) - mid) / mid <= float(cap_adj) + 1e-12)
        return True

    # Build candidate grid.
    def _grid(center: float) -> List[float]:
        lo = max(0.05, float(center) - max_delta)
        hi = float(center) + max_delta
        xs = []
        x = lo
        while x <= hi + 1e-12:
            xs.append(round(x, 4))
            x += step
        return xs

    put_grid = _grid(float(base_put))
    call_grid = _grid(float(base_call))

    best = None
    best_stats = None
    best_feasible = False

    # Baseline (heuristic) stats for comparison.
    base_stats = _eval_candidate(float(base_put), float(base_call))

    for pm in put_grid:
        for cm in call_grid:
            if not _passes_caps(pm, cm):
                continue
            stats = _eval_candidate(float(pm), float(cm))
            feasible = (stats["breachProbEither"] <= max_breach_either + 1e-12) and ((not use_cvar_budget) or (stats["cvar95Total"] <= max_cvar + 1e-12))

            if best is None:
                best = (pm, cm)
                best_stats = stats
                best_feasible = feasible
                continue

            # Prefer feasible solutions. Within feasibility, minimize CVaR then breach prob.
            if feasible and not best_feasible:
                best = (pm, cm)
                best_stats = stats
                best_feasible = True
                continue
            if feasible == best_feasible:
                assert best_stats is not None
                if stats["cvar95Total"] < best_stats["cvar95Total"] - 1e-12:
                    best = (pm, cm)
                    best_stats = stats
                    best_feasible = feasible
                elif abs(stats["cvar95Total"] - best_stats["cvar95Total"]) <= 1e-12 and stats["breachProbEither"] < best_stats["breachProbEither"] - 1e-12:
                    best = (pm, cm)
                    best_stats = stats
                    best_feasible = feasible

    if best is None or best_stats is None:
        return {"mode": "RISK_ONLY", "notes": ["Optimization failed: no candidates evaluated."], "pool": {"excludedCounts": excluded}}

    constraint_summary = {
        "maxBreachProbEither": float(max_breach_either),
        "maxCvar95Total": (float(max_cvar) if use_cvar_budget else None),
        "feasibleFound": bool(best_feasible),
        "conditioningRequested": req,
        "conditioningUsed": conditioning_used,
    }

    return {
        "mode": "RISK_ONLY",
        "optimalPutMultiple": float(best[0]),
        "optimalCallMultiple": float(best[1]),
        "constraintSummary": constraint_summary,
        "improvementVsHeuristic": {
            "heuristic": {"put": float(base_put), "call": float(base_call), **base_stats},
            "optimal": {"put": float(best[0]), "call": float(best[1]), **best_stats},
            "delta": {
                "breachProbEither": float(best_stats["breachProbEither"] - base_stats["breachProbEither"]),
                "cvar95Total": float(best_stats["cvar95Total"] - base_stats["cvar95Total"]),
            },
        },
        "pool": {"sizeUsed": int(len(used_pool)), "excludedCounts": excluded, "shockPoolKey": sp_key},
        "notes": ["Risk-only optimization: minimizes tail risk subject to constraints (no credit model).", *notes],
    }


def bootstrap_tas_stability(
    *,
    flags: FeatureFlags,
    summary: Dict[str, Any],
    regime: Dict[str, Any],
    events: List[Dict[str, Any]],
    n_boot: int,
) -> Dict[str, Any]:
    """
    Bootstrap stability for TAS sign using event-level directional stats already computed.
    Returns additive stability fields. Does not mutate existing wingRecommendation.
    """
    if n_boot <= 0:
        return {"notes": ["stability disabled (MC_BOOTSTRAP_N<=0)."]}

    # Filter usable directional events: need up/down breach flags and overshoot numbers to recompute means.
    usable = []
    for e in events or []:
        if e.get("upBreach") is None or e.get("downBreach") is None:
            continue
        # implied/signed existence is already implicit via directional fields but keep conservative
        if e.get("impliedMovePct") is None or e.get("signedMovePct") is None:
            continue
        usable.append(e)

    if len(usable) < 3:
        return {"notes": ["stability unavailable: insufficient usable events (<3)."], "eventsUsed": int(len(usable))}

    import random

    rng = random.Random(int(flags.MC_GLOBAL_SEED))

    base_wr = compute_wing_recommendation(
        summary=summary,
        quarters={},
        regime=regime,
        current_quarter_key=None,
        skew_component=None,
    )
    base_tas = _to_float(base_wr.get("tas")) or 0.0
    base_sign = -1 if base_tas < 0 else 1 if base_tas > 0 else 0

    tas_vals: List[float] = []
    sign_agree = 0

    for _ in range(int(n_boot)):
        sample = [usable[rng.randrange(len(usable))] for _ in range(len(usable))]
        n = len(sample)
        up = sum(1 for e in sample if e.get("upBreach") is True)
        dn = sum(1 for e in sample if e.get("downBreach") is True)
        up_rate = (up / n) * 100.0
        dn_rate = (dn / n) * 100.0
        up_os = [float(e.get("upOvershootPct")) for e in sample if e.get("upBreach") is True and e.get("upOvershootPct") is not None]
        dn_os = [float(e.get("downOvershootPct")) for e in sample if e.get("downBreach") is True and e.get("downOvershootPct") is not None]
        avg_up_os = (sum(up_os) / len(up_os)) if up_os else 0.0
        avg_dn_os = (sum(dn_os) / len(dn_os)) if dn_os else 0.0

        wr_i = compute_wing_recommendation(
            summary={
                "upBreachRatePct": up_rate,
                "downBreachRatePct": dn_rate,
                "avgUpOvershootPct": avg_up_os,
                "avgDownOvershootPct": avg_dn_os,
                "events_used": n,
            },
            quarters={},
            regime=regime,
            current_quarter_key=None,
            skew_component=None,
        )
        tas_i = _to_float(wr_i.get("tas")) or 0.0
        tas_vals.append(float(tas_i))
        sign_i = -1 if tas_i < 0 else 1 if tas_i > 0 else 0
        if base_sign == 0:
            # If baseline is symmetric, treat agreement as “also near symmetric”.
            if sign_i == 0:
                sign_agree += 1
        else:
            if sign_i == base_sign:
                sign_agree += 1

    agree_pct = (sign_agree / len(tas_vals)) * 100.0 if tas_vals else 0.0
    mean = sum(tas_vals) / len(tas_vals) if tas_vals else 0.0
    var = sum((x - mean) ** 2 for x in tas_vals) / max(1, (len(tas_vals) - 1)) if len(tas_vals) > 1 else 0.0
    std = math.sqrt(var)

    # Concrete caps per spec.
    cap_mode = "FULL"
    cap_adj = None
    if agree_pct < 65.0:
        cap_mode = "FORCE_SYMMETRIC"
        cap_adj = 0.0
    elif agree_pct < 80.0:
        cap_mode = "CAP_ASYMMETRY"
        cap_adj = 0.12  # 12% default within 10–15%

    conf = "LOW"
    if agree_pct >= 80.0:
        conf = "HIGH" if std <= 0.15 else "MED"
    elif agree_pct >= 65.0:
        conf = "MED"

    return {
        "eventsUsed": int(len(usable)),
        "tasSignAgreementPct": round(float(agree_pct), 2),
        "tasStd": round(float(std), 4),
        "confidenceDerived": conf,
        "asymmetryCap": {"mode": cap_mode, "maxAdj": cap_adj},
        "notes": ["Bootstrap stability over TAS sign (history+regime only; no skew)."],
    }


