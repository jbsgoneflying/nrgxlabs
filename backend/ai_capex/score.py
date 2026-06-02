"""Deterministic scoring for the AI Capex Reality Engine.

Pure functions, no I/O, no LLM. Given a ticker's ``CapexEvidence`` list and its
market context, produce a ``TickerVerdict`` with:

- **Reality Score** (0..100): strength of *corroborated, hard, near-term* capex
  evidence, net of delay and hype penalties.
- **Consensus Gap** (-100..100): Reality Score minus a market-positioning score
  (price momentum + valuation richness + analyst-rating drift). A large positive
  gap = reality is strong but the market hasn't repriced ("consensus has not
  updated yet"); a large negative gap = priced as a winner without evidence.
- **Label**: one of the six desk labels, derived by fixed rules from
  (reality, gap, hype_ratio, delay-dominance, category role).

Second-order winners/losers are assigned in ``score_universe`` once driver-
category reality is known, propagated through the taxonomy's linkage edges.

Because this is fully deterministic, every label + conviction is reproducible
from the evidence table — the platform's "LLM never drives sizing" invariant.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

from backend.ai_capex import models
from backend.ai_capex.models import CapexEvidence, TickerVerdict

# Substrings that mark a web source as the issuer's OWN material (IR decks, SEC
# filings) rather than an independent third party. These corroborate the call
# but are the same "voice" as the transcript, so they don't add independence.
_ISSUER_HOST_HINTS = ("q4cdn", "q4inc", "sec.gov", "edgar")

# Evidence weighting knobs.
_TIMING_WEIGHT = {models.TIMING_NEAR: 1.0, models.TIMING_MID: 0.6, models.TIMING_FAR: 0.3}
_SOURCE_WEIGHT = {
    models.SOURCE_TRANSCRIPT: 1.0,
    models.SOURCE_FUNDAMENTAL: 1.0,
    models.SOURCE_WEB: 0.9,
    models.SOURCE_NEWS: 0.7,
}
# Saturation constant: evidence "mass" of this size maps to a 50/100 score.
_SAT = 2.5

# Overhyped (positioning-driven): the market must be genuinely bid for the name
# before a low reality reads as "priced ahead of evidence" rather than just
# "no evidence". Positioning is 0..100 (50 = neutral); >= this = clearly bullish.
_OVERHYPED_POS_MIN = 65.0


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _sigmoid(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def _saturate(mass: float, sat: float = _SAT) -> float:
    """Map non-negative evidence mass to 0..100 (saturating)."""
    mass = max(0.0, mass)
    return 100.0 * mass / (mass + sat)


def _weight(e: CapexEvidence) -> float:
    return (
        _clamp(e.magnitude, 0.0, 1.0)
        * _clamp(e.confidence, 0.0, 1.0)
        * _TIMING_WEIGHT.get(e.timing, 0.6)
        * _SOURCE_WEIGHT.get(e.source_type, 0.7)
    )


def _independence_key(e: CapexEvidence) -> str:
    """A stable key identifying the *independent voice* behind an evidence item.

    Corroboration should reward independent confirmation, not how much one
    source said. So all transcripts (and the issuer's own IR decks / SEC
    filings) collapse to a single ``"issuer"`` voice; the fundamentals feed is
    its own voice; and each distinct news outlet / third-party web domain
    (ISO, FERC, county permits, media) counts separately.
    """
    st = e.source_type
    if st == models.SOURCE_TRANSCRIPT:
        return "issuer"
    if st == models.SOURCE_FUNDAMENTAL:
        return "fundamental"
    url = (e.source_url or "").strip().lower()
    if url and any(h in url for h in _ISSUER_HOST_HINTS):
        return "issuer"
    host = ""
    if url:
        try:
            host = (urlparse(url).netloc or "").lower()
        except Exception:
            host = ""
        if host.startswith("www."):
            host = host[4:]
    return (st + ":" + host) if host else st


# ---------------------------------------------------------------------------
# Market positioning (the "what's already priced in" side of the gap)
# ---------------------------------------------------------------------------


def market_positioning_score(ctx: Dict[str, Any]) -> float:
    """0..100 — how bullishly the market is already positioned on this name.

    Blends price momentum (50%), valuation richness (30%), analyst-rating
    drift (20%). Missing components default to neutral (50) and the weights
    renormalise so a thin context still yields a sane score.
    """
    parts: List[tuple] = []  # (score, weight)

    mom6 = ctx.get("momentum6mPct")
    mom3 = ctx.get("momentum3mPct")
    mom = mom6 if mom6 is not None else mom3
    if mom is not None:
        parts.append((100.0 * _sigmoid(float(mom) / 30.0), 0.50))

    pe = ctx.get("pe")
    if pe is not None and float(pe) > 0:
        parts.append((100.0 * float(pe) / (float(pe) + 40.0), 0.30))

    drift = ctx.get("ratingDrift")
    if drift is not None and ctx.get("ratingCount"):
        parts.append((_clamp(50.0 + 10.0 * _clamp(float(drift), -5.0, 5.0), 0.0, 100.0), 0.20))

    if not parts:
        return 50.0
    wsum = sum(w for _s, w in parts) or 1.0
    return round(sum(s * w for s, w in parts) / wsum, 2)


# ---------------------------------------------------------------------------
# Single-ticker scoring
# ---------------------------------------------------------------------------


def _evidence_breakdown(evidence: List[CapexEvidence]) -> Dict[str, Any]:
    pos_mass = neg_mass = delay_mass = hype_mass = 0.0
    pos_count = 0
    pos_voices = set()
    near_pos = far_pos = 0.0
    for e in evidence:
        w = _weight(e)
        if e.is_hype:
            hype_mass += w
        elif e.signal_type == models.SIG_DELAY:
            delay_mass += w
            neg_mass += 0.6 * w
        elif e.is_hard_positive:
            pos_mass += w
            pos_count += 1
            pos_voices.add(_independence_key(e))
            if e.timing == models.TIMING_NEAR:
                near_pos += w
            elif e.timing == models.TIMING_FAR:
                far_pos += w
        elif e.is_hard_negative:
            neg_mass += w
        else:
            # neutral second_order_link / weak signal: small positive nudge
            pos_mass += 0.3 * w
    return {
        "pos_mass": pos_mass, "neg_mass": neg_mass, "delay_mass": delay_mass,
        "hype_mass": hype_mass, "pos_count": pos_count,
        "independent_pos_sources": len(pos_voices),
        "near_pos": near_pos, "far_pos": far_pos,
    }


def score_ticker(
    ticker: str,
    category: str,
    evidence: List[CapexEvidence],
    market_context: Optional[Dict[str, Any]] = None,
    *,
    flags: Any = None,
) -> TickerVerdict:
    """Score one ticker from its own evidence (no cross-ticker propagation)."""
    ctx = market_context or {}
    min_corr = int(getattr(flags, "AI_CAPEX_MIN_CORROBORATION", 2))
    real_min = float(getattr(flags, "AI_CAPEX_REALITY_REAL_MIN", 60.0))
    gap_thr = float(getattr(flags, "AI_CAPEX_GAP_THRESHOLD", 35.0))
    hype_max = float(getattr(flags, "AI_CAPEX_HYPE_RATIO_MAX", 0.45))

    bd = _evidence_breakdown(evidence)
    pos_mass, neg_mass = bd["pos_mass"], bd["neg_mass"]
    delay_mass, hype_mass = bd["delay_mass"], bd["hype_mass"]

    verdict = TickerVerdict(ticker=ticker, category=category)
    verdict.evidence_count = len(evidence)
    verdict.evidence_ids = [e.evidence_id for e in evidence]

    total_mass = pos_mass + neg_mass + delay_mass + hype_mass
    if total_mass < 0.15 or not evidence:
        verdict.label = models.LABEL_NEUTRAL
        verdict.rationale = "Insufficient evidence to form a view."
        verdict.market_context = ctx
        return verdict

    # Corroboration: shrink positive credit until enough INDEPENDENT sources
    # agree. A single dense earnings call (one "issuer" voice) is discounted no
    # matter how many claims it makes; full credit needs >= min_corr independent
    # voices (e.g. the call + a news outlet, or + a FERC/ISO filing).
    indep = int(bd["independent_pos_sources"])
    corr_factor = _clamp(0.55 + 0.45 * indep / max(1, min_corr), 0.55, 1.0)
    pos = pos_mass * corr_factor

    hype_ratio = hype_mass / (hype_mass + pos + 1e-9)
    hype_penalty_pts = 25.0 * hype_ratio

    base = _saturate(pos)
    penalty = _saturate(neg_mass)
    reality = _clamp(base - 0.7 * penalty - hype_penalty_pts, 0.0, 100.0)

    positioning = market_positioning_score(ctx)
    gap = round(_clamp(reality - positioning, -100.0, 100.0), 2)

    # Delay dominance: delay signals rival the positive capex mass (the thesis
    # is real but the *timing* keeps slipping). Requires meaningful delay mass so
    # a single stale headline can't flip a clean real-beneficiary read.
    delay_dominant = delay_mass > 0.5 * max(pos_mass, 1e-9) and delay_mass > 0.4

    verdict.reality_score = round(reality, 1)
    verdict.consensus_gap = gap
    verdict.hype_ratio = round(hype_ratio, 3)
    verdict.corroboration = indep
    verdict.market_context = {**ctx, "marketPositioning": positioning}
    verdict.top_evidence = [
        e.to_dict() for e in sorted(evidence, key=_weight, reverse=True)[:8]
    ]

    label, direction, conviction, rationale = _classify(
        reality=reality, gap=gap, hype_ratio=hype_ratio, positioning=positioning,
        delay_dominant=delay_dominant, neg_mass=neg_mass, pos=pos,
        real_min=real_min, gap_thr=gap_thr, hype_max=hype_max,
    )
    verdict.label = label
    verdict.direction = direction
    verdict.conviction = round(conviction, 1)
    if label != models.LABEL_NEUTRAL:
        rationale += f" Corroboration: {indep} independent source{'' if indep == 1 else 's'}."
    verdict.rationale = rationale
    return verdict


def _classify(
    *, reality: float, gap: float, hype_ratio: float, positioning: float,
    delay_dominant: bool, neg_mass: float, pos: float,
    real_min: float, gap_thr: float, hype_max: float,
) -> tuple:
    """Fixed-rule label/direction/conviction. Order encodes priority."""
    # Overhyped: the market is positioned for a winner the evidence doesn't
    # support. Two independent triggers (the LLM rarely tags literal hype, so we
    # lead with the positioning>>reality divergence, not the hype-word ratio):
    #   (a) positioning-driven — clearly bullish positioning, sub-par reality,
    #       and a strongly negative gap (priced well ahead of corroborated capex);
    #   (b) hype-language-driven — the extractor flagged mostly bare AI language.
    # hype_ratio scales conviction in both cases.
    overhyped_by_position = (
        reality < real_min and positioning >= _OVERHYPED_POS_MIN and gap <= -gap_thr
    )
    overhyped_by_hype = (
        hype_ratio >= hype_max and reality < real_min and gap <= -gap_thr * 0.4
    )
    if overhyped_by_position or overhyped_by_hype:
        conviction = _clamp(35.0 + 40.0 * hype_ratio + 0.45 * abs(gap), 0.0, 100.0)
        why = ("hype-heavy language without substance"
               if (overhyped_by_hype and not overhyped_by_position)
               else "market positioned well ahead of corroborated capex")
        return (models.LABEL_OVERHYPED, "short", conviction,
                f"Overhyped ({why}): reality {reality:.0f} vs market positioning "
                f"{positioning:.0f} (gap {gap:+.0f}), hype share {hype_ratio*100:.0f}%.")

    # Net-negative capex reality (cuts dominate) — bearish for the chain.
    if neg_mass > pos and reality < real_min * 0.6:
        conviction = _clamp(30.0 + 0.4 * abs(gap) + 20.0 * (neg_mass / (neg_mass + pos + 1e-9)), 0.0, 100.0)
        return (models.LABEL_SECOND_ORDER_LOSER, "short", conviction,
                f"Capex cuts / negative signals outweigh positives (reality {reality:.0f}).")

    # Delayed: there IS real intent, but it's pushed out / gated by power/permits.
    if delay_dominant and reality >= 30.0:
        conviction = _clamp(25.0 + 0.4 * reality, 0.0, 70.0)
        return (models.LABEL_DELAYED, "neutral", conviction,
                f"Real demand but timing slips (delay-dominated). Reality {reality:.0f}, "
                f"gap {gap:+.0f} — a 'right thesis, wrong quarter' name.")

    # Real + market hasn't repriced => the highest-edge bucket.
    if reality >= real_min and gap >= gap_thr:
        conviction = _clamp(0.55 * reality + 0.45 * gap, 0.0, 100.0)
        return (models.LABEL_CONSENSUS_NOT_UPDATED, "long", conviction,
                f"Corroborated real capex (reality {reality:.0f}) but the market hasn't "
                f"repriced (gap {gap:+.0f}, positioning {positioning:.0f}). Pre-consensus long.")

    # Real but already (at least partly) priced.
    if reality >= real_min:
        conviction = _clamp(0.5 * reality + 0.2 * max(0.0, gap), 0.0, 90.0)
        return (models.LABEL_REAL, "long", conviction,
                f"Corroborated real capex (reality {reality:.0f}); market positioning "
                f"{positioning:.0f} so partly priced (gap {gap:+.0f}).")

    # Moderate reality + unpriced still worth flagging as pre-consensus.
    if reality >= real_min * 0.65 and gap >= gap_thr:
        conviction = _clamp(0.45 * reality + 0.35 * gap, 0.0, 75.0)
        return (models.LABEL_CONSENSUS_NOT_UPDATED, "long", conviction,
                f"Building real capex (reality {reality:.0f}) ahead of consensus (gap {gap:+.0f}).")

    return (models.LABEL_NEUTRAL, "neutral", 0.0,
            f"No decisive read (reality {reality:.0f}, gap {gap:+.0f}, hype {hype_ratio*100:.0f}%).")


# ---------------------------------------------------------------------------
# Universe scoring (adds second-order propagation through the taxonomy)
# ---------------------------------------------------------------------------


def score_universe(
    evidence_by_ticker: Dict[str, List[CapexEvidence]],
    context_by_ticker: Optional[Dict[str, Dict[str, Any]]] = None,
    *,
    flags: Any = None,
) -> List[TickerVerdict]:
    """Score every ticker, then propagate driver-category reality to thin
    second-order names as winner/loser overlays."""
    context_by_ticker = context_by_ticker or {}
    tcat = models.ticker_to_category_map()

    verdicts: Dict[str, TickerVerdict] = {}
    for ticker, evid in evidence_by_ticker.items():
        cat = tcat.get(ticker) or models.category_of(ticker) or ""
        verdicts[ticker] = score_ticker(
            ticker, cat, evid, context_by_ticker.get(ticker), flags=flags,
        )

    # Driver-category reality = mean reality of that category's own-evidence names.
    driver_reality: Dict[str, float] = {}
    driver_delay: Dict[str, float] = {}
    by_cat: Dict[str, List[TickerVerdict]] = {}
    for v in verdicts.values():
        if v.category:
            by_cat.setdefault(v.category, []).append(v)
    for cat, vs in by_cat.items():
        scored = [v.reality_score for v in vs if v.reality_score > 0]
        if scored:
            driver_reality[cat] = sum(scored) / len(scored)
        delayed = sum(1 for v in vs if v.label == models.LABEL_DELAYED)
        driver_delay[cat] = delayed / max(1, len(vs))

    real_min = float(getattr(flags, "AI_CAPEX_REALITY_REAL_MIN", 60.0))

    # For each second-order ticker whose own read is weak/neutral, see whether
    # an upstream driver category propagates a strong signal to it.
    for ticker, v in verdicts.items():
        if v.category and models.category_role(v.category) != "second_order":
            continue
        if v.label not in (models.LABEL_NEUTRAL,) and v.conviction >= 40:
            continue  # own evidence already gives a confident read
        best_drive = 0.0
        best_driver_cat = ""
        delay_drive = False
        for driver_cat, edges in models.load_universe().get("second_order_edges", {}).items():
            if not isinstance(edges, list):
                continue
            for edge in edges:
                if not isinstance(edge, dict) or edge.get("category") != v.category:
                    continue
                dr = driver_reality.get(driver_cat, 0.0)
                propagated = dr * float(edge.get("weight", 0.0))
                if propagated > best_drive:
                    best_drive = propagated
                    best_driver_cat = driver_cat
                    delay_drive = driver_delay.get(driver_cat, 0.0) >= 0.5
        if best_drive >= real_min * 0.6 and best_driver_cat:
            driver_name = models.category_name(best_driver_cat)
            if delay_drive:
                v.label = models.LABEL_SECOND_ORDER_LOSER
                v.direction = "short"
                v.conviction = round(_clamp(0.5 * best_drive, 0.0, 60.0), 1)
                v.rationale = (f"Second-order exposure to {driver_name} capex, which is "
                               f"showing delays — timing risk flows downstream.")
            else:
                v.label = models.LABEL_SECOND_ORDER_WINNER
                v.direction = "long"
                v.conviction = round(_clamp(0.55 * best_drive, 0.0, 80.0), 1)
                v.rationale = (f"Second-order winner: {driver_name} shows corroborated real "
                               f"capex (driver reality {driver_reality.get(best_driver_cat, 0):.0f}) "
                               f"that flows to this name before its own numbers move.")

    # Stable, desk-useful ordering: actionable first, then by conviction.
    out = list(verdicts.values())
    out.sort(key=lambda v: (0 if v.is_actionable else 1, -v.conviction, v.ticker))
    return out
