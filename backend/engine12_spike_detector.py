"""Engine 12 — VIX Spike Detection and Regime Classification.

Detects whether the current VIX move qualifies as a geopolitical shock,
classifies severity, and estimates scenario probabilities dynamically
adjusted by dealer gamma state and cross-asset stress.
"""

from __future__ import annotations

import json
import logging
import math
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

LOG = logging.getLogger(__name__)

_SHOCK_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "universe", "geopolitical_shocks.json")


@dataclass
class SpikeSignal:
    detected: bool = False
    vix_current: float = 0.0
    vix_20d_ma: float = 0.0
    vix_20d_std: float = 0.0
    spike_pct_above_ma: float = 0.0
    z_score: float = 0.0
    pre_event_regime: str = "unknown"   # low_vol | normal | elevated | high_vol
    pre_event_mean: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "detected": self.detected,
            "vixCurrent": round(self.vix_current, 2),
            "vix20dMA": round(self.vix_20d_ma, 2),
            "vix20dStd": round(self.vix_20d_std, 2),
            "spikePctAboveMA": round(self.spike_pct_above_ma, 1),
            "zScore": round(self.z_score, 2),
            "preEventRegime": self.pre_event_regime,
            "preEventMean": round(self.pre_event_mean, 2),
        }


@dataclass
class SeverityScore:
    score: float = 0.0          # 0-100
    vix_spike_pct: float = 0.0
    spx_gap_pct: float = 0.0
    oil_gap_pct: float = 0.0
    dealer_gamma_sign: str = "unknown"
    cross_asset_stress: float = 50.0
    components: Dict[str, float] = None

    def __post_init__(self):
        if self.components is None:
            self.components = {}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "score": round(self.score, 1),
            "vixSpikePct": round(self.vix_spike_pct, 1),
            "spxGapPct": round(self.spx_gap_pct, 2),
            "oilGapPct": round(self.oil_gap_pct, 2),
            "dealerGammaSign": self.dealer_gamma_sign,
            "crossAssetStress": round(self.cross_asset_stress, 1),
            "components": {k: round(v, 2) for k, v in (self.components or {}).items()},
        }


@dataclass
class ScenarioProbabilities:
    p_contained: float = 0.60
    p_disruption: float = 0.25
    p_escalation: float = 0.15
    adjustments: List[str] = None

    def __post_init__(self):
        if self.adjustments is None:
            self.adjustments = []

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pContained": round(self.p_contained, 3),
            "pDisruption": round(self.p_disruption, 3),
            "pEscalation": round(self.p_escalation, 3),
            "adjustments": self.adjustments,
        }


def _clamp(lo: float, hi: float, x: float) -> float:
    return max(lo, min(hi, x))


def detect_vix_spike(closes: List[float]) -> SpikeSignal:
    """Detect if current VIX move qualifies as a geopolitical shock.

    Criteria:
    (a) VIX prior 20d mean was in low-vol regime (< 20)
    (b) Current VIX is > 20% above 20d MA
    (c) Move is > 2 standard deviations from recent history
    """
    if len(closes) < 21:
        return SpikeSignal()

    current = closes[-1]
    lookback = closes[-21:-1]
    ma_20 = sum(lookback) / len(lookback)
    var_20 = sum((v - ma_20) ** 2 for v in lookback) / max(1, len(lookback) - 1)
    std_20 = math.sqrt(var_20) if var_20 > 0 else 1.0

    spike_pct = ((current - ma_20) / ma_20 * 100.0) if ma_20 > 0 else 0.0
    z = (current - ma_20) / std_20 if std_20 > 0.1 else 0.0

    if ma_20 < 16:
        regime = "low_vol"
    elif ma_20 < 20:
        regime = "normal"
    elif ma_20 < 25:
        regime = "elevated"
    else:
        regime = "high_vol"

    detected = spike_pct > 15.0 and z > 1.5

    return SpikeSignal(
        detected=detected,
        vix_current=current,
        vix_20d_ma=ma_20,
        vix_20d_std=std_20,
        spike_pct_above_ma=spike_pct,
        z_score=z,
        pre_event_regime=regime,
        pre_event_mean=ma_20,
    )


def classify_event_severity(
    *,
    vix_spike_pct: float,
    spx_gap_pct: float,
    oil_gap_pct: float,
    cross_asset_stress: float = 50.0,
    dealer_gamma_sign: str = "unknown",
    dealer_gamma_bucket: str = "low",
) -> SeverityScore:
    """Composite severity score (0-100) from multi-factor inputs.

    Weights: VIX spike 35%, SPX gap 25%, Oil gap 20%, Cross-asset stress 20%.
    Dealer gamma modifies the final score (short gamma amplifies severity).
    """
    # Normalize each factor to 0-100 scale
    vix_component = _clamp(0, 100, abs(vix_spike_pct) * 2.5)
    spx_component = _clamp(0, 100, abs(spx_gap_pct) * 15.0)
    oil_component = _clamp(0, 100, abs(oil_gap_pct) * 5.0)
    stress_component = _clamp(0, 100, cross_asset_stress)

    raw = (
        vix_component * 0.35
        + spx_component * 0.25
        + oil_component * 0.20
        + stress_component * 0.20
    )

    # Dealer gamma modifier
    gamma_adj = 0.0
    if dealer_gamma_sign == "negative":
        amp = {"low": 5.0, "medium": 10.0, "high": 15.0}.get(dealer_gamma_bucket, 5.0)
        gamma_adj = amp
    elif dealer_gamma_sign == "positive":
        damp = {"low": -3.0, "medium": -6.0, "high": -10.0}.get(dealer_gamma_bucket, -3.0)
        gamma_adj = damp

    score = _clamp(0, 100, raw + gamma_adj)

    return SeverityScore(
        score=score,
        vix_spike_pct=vix_spike_pct,
        spx_gap_pct=spx_gap_pct,
        oil_gap_pct=oil_gap_pct,
        dealer_gamma_sign=dealer_gamma_sign,
        cross_asset_stress=cross_asset_stress,
        components={
            "vix": vix_component,
            "spx": spx_component,
            "oil": oil_component,
            "cross_asset": stress_component,
            "gamma_adj": gamma_adj,
        },
    )


def estimate_scenario_probabilities(
    severity_score: float,
    *,
    dealer_gamma_sign: str = "unknown",
    dealer_gamma_bucket: str = "low",
    cross_asset_stress: float = 50.0,
    vix_spike_pct: float = 0.0,
    spx_gap_pct: float = 0.0,
    oil_gap_pct: float = 0.0,
    edge_score: float = 50.0,
    pre_event_regime: str = "normal",
    shock_db: Optional[List[Dict[str, Any]]] = None,
) -> ScenarioProbabilities:
    """Estimate P(contained), P(disruption), P(escalation) from all available data.

    Multi-factor model combining:
    1. Empirical base rates from historical shock DB (conditioned on similar events)
    2. Current VIX spike magnitude and regime
    3. Cross-asset stress composite
    4. Dealer gamma state
    5. Edge decomposition score
    """
    adjustments: List[str] = []

    # ── Factor 1: Empirical base rates from shock DB ──
    events = shock_db if shock_db is not None else load_shock_db()
    n_contained = n_disruption = n_escalation = 0
    similar_outcomes: List[str] = []

    for evt in events:
        outcome = evt.get("outcome_class", "contained")
        if outcome == "contained":
            n_contained += 1
        elif outcome == "disruption":
            n_disruption += 1
        elif outcome == "escalation":
            n_escalation += 1

    n_total = n_contained + n_disruption + n_escalation
    if n_total > 0:
        base_c = n_contained / n_total
        base_d = n_disruption / n_total
        base_e = n_escalation / n_total
        adjustments.append(
            f"Historical base rates ({n_total} events): "
            f"contained {base_c:.0%}, disruption {base_d:.0%}, escalation {base_e:.0%}"
        )
    else:
        base_c, base_d, base_e = 0.55, 0.27, 0.18

    # ── Factor 2: Condition on similar events (nearest-neighbor) ──
    if events and vix_spike_pct > 0:
        scored_events = []
        for evt in events:
            vix_pre = evt.get("vix_pre_close", 0)
            vix_open = evt.get("vix_event_open", 0)
            if vix_pre <= 0:
                continue
            evt_spike = (vix_open - vix_pre) / vix_pre * 100.0
            evt_oil = abs(evt.get("oil_gap_pct", 0))

            # Similarity weight: inverse distance, exponential decay
            dist = math.sqrt(
                ((vix_spike_pct - evt_spike) / 10.0) ** 2
                + ((abs(oil_gap_pct) - evt_oil) / 5.0) ** 2
            )
            weight = math.exp(-dist)
            scored_events.append((weight, evt.get("outcome_class", "contained")))

        if scored_events:
            w_c = sum(w for w, o in scored_events if o == "contained")
            w_d = sum(w for w, o in scored_events if o == "disruption")
            w_e = sum(w for w, o in scored_events if o == "escalation")
            w_total = w_c + w_d + w_e
            if w_total > 0:
                sim_c = w_c / w_total
                sim_d = w_d / w_total
                sim_e = w_e / w_total
                # Blend: 40% empirical base, 60% similarity-weighted
                base_c = base_c * 0.40 + sim_c * 0.60
                base_d = base_d * 0.40 + sim_d * 0.60
                base_e = base_e * 0.40 + sim_e * 0.60
                adjustments.append(
                    f"Similarity-conditioned: nearest events favor "
                    f"contained {sim_c:.0%}, disruption {sim_d:.0%}, escalation {sim_e:.0%}"
                )

    p_c, p_d, p_e = base_c, base_d, base_e

    # ── Factor 3: VIX spike magnitude adjustment ──
    if vix_spike_pct > 40:
        shift = min(0.15, (vix_spike_pct - 40) / 200.0)
        p_c -= shift
        p_e += shift * 0.6
        p_d += shift * 0.4
        adjustments.append(f"VIX spike extreme (+{vix_spike_pct:.0f}%): escalation risk elevated")
    elif vix_spike_pct > 25:
        shift = min(0.08, (vix_spike_pct - 25) / 200.0)
        p_c -= shift * 0.5
        p_d += shift * 0.5
        adjustments.append(f"VIX spike significant (+{vix_spike_pct:.0f}%): disruption probability higher")

    # ── Factor 4: Pre-event regime ──
    if pre_event_regime == "low_vol":
        p_c += 0.04
        p_e -= 0.03
        adjustments.append("Pre-event low-vol regime: contained probability +4% (shocks from low base tend to revert)")
    elif pre_event_regime == "high_vol":
        p_c -= 0.05
        p_e += 0.04
        adjustments.append("Pre-event high-vol regime: escalation risk +4% (already stressed, amplification likely)")

    # ── Factor 5: Oil gap (energy disruption signal) ──
    if abs(oil_gap_pct) > 10:
        shift = min(0.10, abs(oil_gap_pct) / 100.0)
        p_c -= shift
        p_d += shift * 0.6
        p_e += shift * 0.4
        adjustments.append(f"Oil gap {oil_gap_pct:+.1f}%: energy disruption signal, away from contained")
    elif abs(oil_gap_pct) > 5:
        shift = min(0.04, abs(oil_gap_pct) / 150.0)
        p_d += shift
        p_c -= shift * 0.5

    # ── Factor 6: Dealer gamma ──
    if dealer_gamma_sign == "negative":
        shift = {"low": 0.03, "medium": 0.06, "high": 0.10}.get(dealer_gamma_bucket, 0.03)
        p_c -= shift
        p_e += shift * 0.6
        p_d += shift * 0.4
        adjustments.append(
            f"Dealers short gamma ({dealer_gamma_bucket}): hedging flow amplifies moves, "
            f"escalation +{shift * 0.6:.0%}"
        )
    elif dealer_gamma_sign == "positive":
        shift = {"low": 0.02, "medium": 0.04, "high": 0.07}.get(dealer_gamma_bucket, 0.02)
        p_c += shift
        p_e -= shift * 0.6
        p_d -= shift * 0.4
        adjustments.append(
            f"Dealers long gamma ({dealer_gamma_bucket}): hedging flow dampens moves, "
            f"contained +{shift:.0%}"
        )

    # ── Factor 7: Cross-asset stress ──
    if cross_asset_stress > 70:
        shift = min(0.12, (cross_asset_stress - 70) / 200.0)
        p_c -= shift
        p_d += shift * 0.5
        p_e += shift * 0.5
        adjustments.append(f"Cross-asset stress elevated ({cross_asset_stress:.0f}/100): multi-market confirmation of risk")
    elif cross_asset_stress > 60:
        shift = min(0.05, (cross_asset_stress - 60) / 250.0)
        p_c -= shift * 0.5
        p_d += shift * 0.5
        adjustments.append(f"Cross-asset stress moderate ({cross_asset_stress:.0f}/100): some confirmation")
    elif cross_asset_stress < 35:
        shift = min(0.08, (35 - cross_asset_stress) / 250.0)
        p_c += shift
        p_e -= shift * 0.6
        p_d -= shift * 0.4
        adjustments.append(f"Cross-asset stress low ({cross_asset_stress:.0f}/100): other markets not confirming panic")

    # ── Factor 8: Edge score (high edge = market overpricing, favors contained) ──
    if edge_score > 65:
        shift = min(0.06, (edge_score - 65) / 500.0)
        p_c += shift
        p_e -= shift * 0.6
        adjustments.append(f"Edge score {edge_score:.0f}/100: market overpricing the shock, contained more likely")
    elif edge_score < 35:
        shift = min(0.04, (35 - edge_score) / 500.0)
        p_c -= shift * 0.5
        p_d += shift * 0.5
        adjustments.append(f"Edge score {edge_score:.0f}/100: market may be under-pricing risk")

    # ── Normalize ──
    p_c = max(0.03, p_c)
    p_d = max(0.03, p_d)
    p_e = max(0.02, p_e)
    total = p_c + p_d + p_e
    p_c /= total
    p_d /= total
    p_e /= total

    return ScenarioProbabilities(
        p_contained=p_c,
        p_disruption=p_d,
        p_escalation=p_e,
        adjustments=adjustments,
    )


def load_shock_db() -> List[Dict[str, Any]]:
    """Load the curated geopolitical shock database."""
    try:
        path = os.path.normpath(_SHOCK_DB_PATH)
        with open(path, "r") as f:
            data = json.load(f)
        return data.get("events", [])
    except Exception as exc:
        LOG.warning("Failed to load geopolitical shock DB: %s", exc)
        return []


def find_similar_events(
    *,
    vix_spike_pct: float,
    spx_gap_pct: float,
    oil_gap_pct: float,
    shock_db: Optional[List[Dict[str, Any]]] = None,
    top_n: int = 10,
) -> List[Dict[str, Any]]:
    """Find historical events most similar to current conditions.

    Similarity = inverse Euclidean distance in normalized feature space.
    """
    events = shock_db if shock_db is not None else load_shock_db()
    if not events:
        return []

    scored = []
    for evt in events:
        vix_pre = evt.get("vix_pre_close", 0)
        vix_open = evt.get("vix_event_open", 0)
        if vix_pre <= 0:
            continue
        evt_spike = (vix_open - vix_pre) / vix_pre * 100.0
        evt_spx = evt.get("spx_gap_pct", 0)
        evt_oil = evt.get("oil_gap_pct", 0)
        peak = evt.get("peak_vix", vix_open)
        jump_ratio = peak / vix_open if vix_open > 0 else 1.0

        dist = math.sqrt(
            ((vix_spike_pct - evt_spike) / 10.0) ** 2
            + ((spx_gap_pct - evt_spx) / 3.0) ** 2
            + ((oil_gap_pct - evt_oil) / 5.0) ** 2
        )

        enriched = dict(evt)
        enriched["computed_spike_pct"] = round(evt_spike, 1)
        enriched["jump_ratio"] = round(jump_ratio, 3)
        enriched["similarity_distance"] = round(dist, 3)
        scored.append((dist, enriched))

    scored.sort(key=lambda x: x[0])
    return [evt for _, evt in scored[:top_n]]
