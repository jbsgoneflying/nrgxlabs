"""Aggregator — normalise every engine's current signals into one schema.

The allocator only ever sees ``Opportunity`` objects, so it stays decoupled
from each engine's bespoke payload shape. Extractors here are deliberately
**defensive and cheap**: they read engines' already-persisted Redis
trackers / snapshots (no live ORATS scans), and any failure degrades to an
empty list rather than raising.

Sources wired for the MVP:
- Red Dog (UI E4) desk tracker + actionable signals.
- Ichimoku (UI E5) desk tracker + actionable signals.
- A ``consensus_engine.ConsensusResult`` (regime / IC / VIX / credit reads),
  which contributes overlay context and any income-sleeve directional bias.

The router decides which sources to hand in; the aggregator just normalises
whatever it is given.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from backend.desk_brain import sleeves

# Verdict strings emitted by gating.reconcile_*_verdict.
_VERDICT_TRADABLE = "TRADABLE"
_VERDICT_WATCH = "WATCH"
_VERDICT_STAND_DOWN = "STAND_DOWN"

# Desk-managed lifecycle states that mean "the desk is actively in/eyeing it".
_LIVE_DESK_STATES = {"watching", "entered", "working"}


@dataclass
class Opportunity:
    """One normalised candidate trade the allocator can size."""
    engine_id: int
    engine_name: str
    sleeve: str
    ticker: str
    direction: str            # "long" | "short" | "sell_vol" | "neutral" | ...
    structure: str            # "mean_reversion" | "trend" | "iron_condor" | ...
    conviction: float         # 0..100 (engine's own quality/conviction read)
    verdict: str              # TRADABLE | WATCH | STAND_DOWN | n/a
    desk_status: str          # "" | watching | entered | working | ...
    risk_dollars: Optional[float] = None   # per-unit risk if the engine sized it
    reward_r: Optional[float] = None       # reward in R if known
    summary: str = ""
    source: str = ""          # provenance tag
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def is_live(self) -> bool:
        """True if the desk is already in or actively watching the name."""
        return self.desk_status in _LIVE_DESK_STATES

    @property
    def is_actionable(self) -> bool:
        """Eligible to be sized: tradable verdict or a live desk position."""
        return self.verdict == _VERDICT_TRADABLE or self.is_live

    def to_dict(self) -> dict:
        return {
            "engineId": self.engine_id,
            "engineName": self.engine_name,
            "sleeve": self.sleeve,
            "ticker": self.ticker,
            "direction": self.direction,
            "structure": self.structure,
            "conviction": round(float(self.conviction), 1),
            "verdict": self.verdict,
            "deskStatus": self.desk_status,
            "riskDollars": self.risk_dollars,
            "rewardR": self.reward_r,
            "summary": self.summary,
            "source": self.source,
            "isLive": self.is_live,
            "isActionable": self.is_actionable,
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _f(val: Any) -> Optional[float]:
    try:
        if val is None:
            return None
        return float(val)
    except (TypeError, ValueError):
        return None


def _verdict_of(rec: Dict[str, Any]) -> str:
    v = rec.get("verdict")
    if isinstance(v, dict):
        return str(v.get("status") or "n/a")
    return "n/a"


def _conviction_of(rec: Dict[str, Any]) -> float:
    """Prefer the reconciled conviction, fall back to the quality score."""
    v = rec.get("verdict")
    if isinstance(v, dict) and v.get("conviction") is not None:
        c = _f(v.get("conviction"))
        if c is not None:
            return max(0.0, min(100.0, c))
    q = rec.get("quality")
    if isinstance(q, dict):
        c = _f(q.get("score"))
        if c is not None:
            return max(0.0, min(100.0, c))
    c = _f(rec.get("score"))
    return max(0.0, min(100.0, c)) if c is not None else 0.0


def _records_from_tracker(tracker: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Flatten a get_all_signals() payload into a de-duplicated record list.

    Includes desk-managed live states + any pending/triggered actionable
    records. De-dupes by (ticker, signalDate) keeping the richest record.
    """
    if not isinstance(tracker, dict):
        return []
    wanted = ["watching", "entered", "working", "pending", "triggered"]
    seen: Dict[str, Dict[str, Any]] = {}
    for bucket in wanted:
        rows = tracker.get(bucket)
        if not isinstance(rows, list):
            continue
        for rec in rows:
            if not isinstance(rec, dict):
                continue
            key = f"{(rec.get('ticker') or '').upper()}|{str(rec.get('signalDate', ''))[:10]}"
            # First write wins (desk states are iterated before raw scan buckets).
            seen.setdefault(key, rec)
    return list(seen.values())


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------


def _opportunity_from_signal(
    rec: Dict[str, Any],
    *,
    engine_id: int,
    structure: str,
    source: str,
) -> Optional[Opportunity]:
    ticker = str(rec.get("ticker") or "").upper()
    if not ticker:
        return None
    levels = rec.get("levels") if isinstance(rec.get("levels"), dict) else {}
    edge = sleeves.get_engine_edge(engine_id)
    return Opportunity(
        engine_id=engine_id,
        engine_name=edge.engine_name or "",
        sleeve=sleeves.sleeve_for_engine(engine_id),
        ticker=ticker,
        direction=str(rec.get("direction") or "").lower() or "long",
        structure=structure,
        conviction=_conviction_of(rec),
        verdict=_verdict_of(rec),
        desk_status=str(rec.get("status") or "").strip().lower(),
        risk_dollars=_f(levels.get("riskDollars")),
        reward_r=_f(levels.get("reward1R")),
        summary=_signal_summary(rec, structure),
        source=source,
        raw={"signalDate": rec.get("signalDate"), "grade": (rec.get("quality") or {}).get("grade")},
    )


def _signal_summary(rec: Dict[str, Any], structure: str) -> str:
    q = rec.get("quality") if isinstance(rec.get("quality"), dict) else {}
    grade = q.get("grade") or "?"
    direction = str(rec.get("direction") or "").lower()
    status = str(rec.get("status") or "").lower()
    bits = [f"grade {grade}", direction or structure]
    if status and status not in ("pending",):
        bits.append(status)
    return " · ".join(b for b in bits if b)


def from_reddog_tracker(tracker: Optional[Dict[str, Any]]) -> List[Opportunity]:
    """Normalise Red Dog (UI E4) tracker records into opportunities."""
    out: List[Opportunity] = []
    for rec in _records_from_tracker(tracker or {}):
        opp = _opportunity_from_signal(
            rec, engine_id=4, structure="mean_reversion", source="reddog_tracker",
        )
        if opp is not None:
            out.append(opp)
    return out


def from_ichimoku_tracker(tracker: Optional[Dict[str, Any]]) -> List[Opportunity]:
    """Normalise Ichimoku (UI E5) tracker records into opportunities."""
    out: List[Opportunity] = []
    for rec in _records_from_tracker(tracker or {}):
        opp = _opportunity_from_signal(
            rec, engine_id=5, structure="trend_continuation", source="ichimoku_tracker",
        )
        if opp is not None:
            out.append(opp)
    return out


def from_signal_list(
    signals: Optional[List[Dict[str, Any]]],
    *,
    engine_id: int,
    structure: str,
    source: str,
) -> List[Opportunity]:
    """Normalise a raw list of engine signal dicts (e.g. a cached scan)."""
    out: List[Opportunity] = []
    for rec in (signals or []):
        if not isinstance(rec, dict):
            continue
        opp = _opportunity_from_signal(
            rec, engine_id=engine_id, structure=structure, source=source,
        )
        if opp is not None:
            out.append(opp)
    return out


def from_earnings_radar(radar: Optional[Dict[str, Any]]) -> List[Opportunity]:
    """Normalise the earnings radar into volatility/income opportunities.

    Each mega-cap reporter becomes an E1 earnings-IC candidate carrying its
    *factual* report date + BMO/AMC timing (from EODHD) and a materiality-driven
    conviction. This is how Desk Brain auto-flags the names to run E1/E15 on.
    """
    out: List[Opportunity] = []
    if not isinstance(radar, dict):
        return out
    edge = sleeves.get_engine_edge(1)
    for r in (radar.get("reporters") or []):
        ticker = str(r.get("ticker") or "").upper()
        if not ticker:
            continue
        timing = str(r.get("timing") or "TBD")
        report_date = str(r.get("reportDate") or "")
        mcap_b = round(float(r.get("marketCap") or 0.0) / 1e9, 0)
        out.append(
            Opportunity(
                engine_id=1,
                engine_name=edge.engine_name or "Earnings Hold Risk (IC)",
                sleeve=sleeves.SLEEVE_VOLATILITY,
                ticker=ticker,
                direction="sell_vol",
                structure="earnings_ic",
                conviction=max(0.0, min(100.0, float(r.get("materiality") or 0.0))),
                verdict=_VERDICT_TRADABLE,
                desk_status="",
                summary=f"Earnings {report_date} {timing} · ${mcap_b:.0f}B · {r.get('sector') or ''}".strip(),
                source="earnings_radar",
                raw={
                    "reportDate": report_date,
                    "timing": timing,
                    "marketCap": r.get("marketCap"),
                    "sector": r.get("sector"),
                    "daysToReport": r.get("daysToReport"),
                },
            )
        )
    return out


def from_vix_alert(alert: Optional[Dict[str, Any]]) -> List[Opportunity]:
    """Normalise the cheap E12 Redis spike alert into a fade opportunity.

    Only emits when a spike is currently detected. Conviction scales with the
    spike magnitude above the 20d MA (capped) — bigger dislocations are richer
    fades, though the E12 engine itself sizes/structures the actual trade.
    """
    if not isinstance(alert, dict) or not alert.get("detected"):
        return []
    edge = sleeves.get_engine_edge(12)
    spike_pct = _f(alert.get("spikePctAboveMA")) or 0.0
    # 0% above MA -> 40 conviction floor; ~+60% -> ~100.
    conviction = max(40.0, min(100.0, 40.0 + spike_pct))
    vix = _f(alert.get("vixCurrent"))
    return [
        Opportunity(
            engine_id=12,
            engine_name=edge.engine_name or "VIX Spike Fade",
            sleeve=sleeves.SLEEVE_VOLATILITY,
            ticker="VIX",
            direction="sell_vol",
            structure="vix_fade",
            conviction=conviction,
            verdict=_VERDICT_TRADABLE,
            desk_status="",
            summary=f"VIX spike detected: {vix if vix is not None else '?'} ({spike_pct:.0f}% > 20d MA) — fadeable",
            source="e12_alert",
            raw={"vixCurrent": vix, "spikePctAboveMA": spike_pct, "zScore": alert.get("zScore")},
        )
    ]


def from_ai_capex(scan: Optional[Dict[str, Any]]) -> List[Opportunity]:
    """Normalise an AI Capex Reality Engine (UI E17) scan into opportunities.

    Only *actionable, directional* verdicts (the real / pre-consensus longs and
    overhyped / second-order-loser shorts) become tradable candidates; delayed
    and neutral names are watch-only and skipped. Conviction is the engine's own
    deterministic read. Sized into the thematic sleeve, where the thin edge prior
    keeps it tiny until paper history proves it (same pattern as every engine).
    """
    out: List[Opportunity] = []
    if not isinstance(scan, dict):
        return out
    edge = sleeves.get_engine_edge(17)
    for v in (scan.get("verdicts") or []):
        if not isinstance(v, dict):
            continue
        ticker = str(v.get("ticker") or "").upper()
        direction = str(v.get("direction") or "").lower()
        if not ticker or direction not in ("long", "short"):
            continue
        conviction = _f(v.get("conviction")) or 0.0
        if conviction <= 0:
            continue
        label = str(v.get("label") or "")
        out.append(
            Opportunity(
                engine_id=17,
                engine_name=edge.engine_name or "AI Capex Reality Engine",
                sleeve=sleeves.SLEEVE_THEMATIC,
                ticker=ticker,
                direction=direction,
                structure="ai_capex_thematic",
                conviction=max(0.0, min(100.0, conviction)),
                verdict=_VERDICT_TRADABLE,
                desk_status="",
                summary=f"{v.get('labelDisplay') or label} · reality {int(round(_f(v.get('realityScore')) or 0))}"
                        f" · gap {int(round(_f(v.get('consensusGap')) or 0)):+d}",
                source="ai_capex",
                raw={
                    "label": label,
                    "realityScore": v.get("realityScore"),
                    "consensusGap": v.get("consensusGap"),
                    "category": v.get("category"),
                },
            )
        )
    return out


def from_engine18(scan: Optional[Dict[str, Any]]) -> List[Opportunity]:
    """Normalise an Earnings Drift (UI E18) scan into opportunities.

    Only candidates the deterministic matrix sized full or half become
    tradable; passes never reach the allocator. Long-only by engine design
    (misses lose money — validated). Conviction maps the sizing tier plus the
    quality grade: the same gradient the bake-off cohorts showed (+1.05%
    large beats, +1.45% top-quintile quality).
    """
    out: List[Opportunity] = []
    if not isinstance(scan, dict):
        return out
    edge = sleeves.get_engine_edge(18)
    for c in (scan.get("candidates") or []):
        if not isinstance(c, dict):
            continue
        ticker = str(c.get("ticker") or "").upper()
        sizing = str(c.get("sizing") or "").lower()
        if not ticker or sizing not in ("full", "half"):
            continue
        # Late manual profiles: the validated entry (next open after the
        # report) has passed — mid-drift entries were never backtested, so
        # they stay informational and never reach the allocator.
        if str(c.get("entry_status") or "") == "late":
            continue
        grade = c.get("grade") or {}
        quality = _f(grade.get("score")) or 0.5
        base = 70.0 if sizing == "full" else 50.0
        conviction = max(0.0, min(100.0, base + 20.0 * (quality - 0.5) * 2.0))
        rep = c.get("report") or {}
        surprise = _f(rep.get("surprise_pct"))
        out.append(
            Opportunity(
                engine_id=18,
                engine_name=edge.engine_name or "Earnings Drift (PEAD)",
                sleeve=sleeves.SLEEVE_DIRECTIONAL,
                ticker=ticker,
                direction="long",
                structure="earnings_drift",
                conviction=conviction,
                verdict=_VERDICT_TRADABLE,
                desk_status="",
                summary=(
                    f"{'Large' if c.get('bucket') == 'beat_large' else 'Small'} beat"
                    f" {f'{surprise * 100:+.0f}%' if surprise is not None else ''}"
                    f" · quality {grade.get('quintile') or '?'} · {sizing} size"
                    f" · exit {c.get('exit_date') or '?'}"
                ),
                source="engine18",
                raw={
                    "bucket": c.get("bucket"),
                    "sizing": sizing,
                    "quintile": grade.get("quintile"),
                    "qualityScore": quality,
                    "surprisePct": surprise,
                    "entryDate": c.get("entry_date"),
                    "exitDate": c.get("exit_date"),
                },
            )
        )
    return out


def from_consensus(consensus: Any) -> List[Opportunity]:
    """Normalise a consensus_engine.ConsensusResult's income-sleeve signals.

    Volatility engines (E1/E2/E12) don't expose per-ticker trackers the way
    E4/E5 do, so the consensus extractor gives the allocator a coarse
    book-level read of the short-vol stance (one opportunity per active
    income/vol engine signal). Overlay engines (regime/credit) are skipped
    here — they feed the regime tilt, not the tradable set.
    """
    out: List[Opportunity] = []
    signals = getattr(consensus, "signals", None)
    if not signals:
        return out
    for sig in signals:
        try:
            engine_id = int(getattr(sig, "engine_id", 0))
        except (TypeError, ValueError):
            continue
        sleeve = sleeves.sleeve_for_engine(engine_id)
        if sleeve != sleeves.SLEEVE_VOLATILITY:
            continue
        if not getattr(sig, "active", True):
            continue
        conviction = max(0.0, min(100.0, float(getattr(sig, "conviction", 0) or 0)))
        if conviction <= 0:
            continue
        out.append(
            Opportunity(
                engine_id=engine_id,
                engine_name=str(getattr(sig, "engine_name", "") or ""),
                sleeve=sleeve,
                ticker=str(getattr(sig, "structure", "") or "vol").upper()[:12] or "VOL",
                direction=str(getattr(sig, "direction", "") or "sell_vol"),
                structure=str(getattr(sig, "structure", "") or "premium"),
                conviction=conviction,
                # Consensus signals are book-level reads, treat as tradable
                # so the income sleeve isn't starved when E4/E5 are quiet.
                verdict=_VERDICT_TRADABLE,
                desk_status="",
                summary=str(getattr(sig, "summary", "") or ""),
                source="consensus",
            )
        )
    return out


# ---------------------------------------------------------------------------
# Top-level builder
# ---------------------------------------------------------------------------


def build_opportunity_set(
    *,
    reddog_tracker: Optional[Dict[str, Any]] = None,
    ichimoku_tracker: Optional[Dict[str, Any]] = None,
    consensus: Any = None,
    earnings_radar: Optional[Dict[str, Any]] = None,
    vix_alert: Optional[Dict[str, Any]] = None,
    ai_capex: Optional[Dict[str, Any]] = None,
    engine18: Optional[Dict[str, Any]] = None,
    extra: Optional[List[Opportunity]] = None,
) -> List[Opportunity]:
    """Assemble the normalised opportunity set from all wired sources."""
    opps: List[Opportunity] = []
    opps.extend(from_reddog_tracker(reddog_tracker))
    opps.extend(from_ichimoku_tracker(ichimoku_tracker))
    opps.extend(from_earnings_radar(earnings_radar))
    opps.extend(from_vix_alert(vix_alert))
    opps.extend(from_ai_capex(ai_capex))
    opps.extend(from_engine18(engine18))
    if consensus is not None:
        opps.extend(from_consensus(consensus))
    if extra:
        opps.extend(extra)
    return opps


def summarize_opportunities(opps: List[Opportunity]) -> Dict[str, Any]:
    """Counts by sleeve / verdict for the API status line."""
    by_sleeve: Dict[str, int] = {}
    by_verdict: Dict[str, int] = {}
    actionable = 0
    for o in opps:
        by_sleeve[o.sleeve] = by_sleeve.get(o.sleeve, 0) + 1
        by_verdict[o.verdict] = by_verdict.get(o.verdict, 0) + 1
        if o.is_actionable:
            actionable += 1
    return {
        "total": len(opps),
        "actionable": actionable,
        "bySleeve": by_sleeve,
        "byVerdict": by_verdict,
    }
