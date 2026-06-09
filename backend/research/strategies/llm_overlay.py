"""Pass B — LLM quality overlay: does grading add *incremental* edge?

Pass A proves the raw anomaly exists. Pass B asks the question that actually
matters for NRGX: does an LLM grading layer (guidance quality for PEAD, filing
intent for insider, a news veto for reversal) sort the *same* signals into
better and worse cohorts? If the top quality-quintile out-earns the full sample
and beats the bottom quintile, the moat is real; if not, the LLM is decoration.

Design: graders are pluggable. The deterministic ``HeuristicGuidanceGrader`` and
``NewsVetoGrader`` let the whole overlay run and be unit-tested offline; the
``OpenAIQualityGrader`` is an optional live grader (lazy OpenAI, no test
dependency). Quality scores are bucketed into quintiles and attached to events
as the ``quality_quintile`` tag, which flows through to trade results for
cohort analysis.
"""
from __future__ import annotations

import logging
import os
from dataclasses import replace
from typing import Callable, Dict, List, Optional, Protocol

from backend.research.cohort_stats import group_by_tag, summarize
from backend.research.cost_model import CostModel
from backend.research.event_study import SignalEvent, TradeResult, run_event_study
from backend.research.data_provider import PriceProvider

_LOG = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Grader protocol + implementations
# ---------------------------------------------------------------------------

class QualityGrader(Protocol):
    def grade(self, context: dict) -> float:
        """Return a quality score in [0, 1] for one signal's context."""
        ...


_BULLISH = (
    "raise", "raised", "raising", "beat", "record", "accelerat", "strong",
    "ahead of", "above expectations", "upside", "robust", "momentum",
    "increased guidance", "outperform", "expand",
)
_BEARISH = (
    "cut", "lower", "lowered", "miss", "missed", "headwind", "soft", "weak",
    "below expectations", "downside", "decelerat", "challeng", "uncertain",
    "reduce", "slowdown", "guidance cut",
)


class HeuristicGuidanceGrader:
    """Deterministic, offline grader: bullish-vs-bearish keyword balance.

    A transparent stand-in for an LLM read of transcript/guidance tone. Lets the
    overlay run with zero API cost (and makes Pass B unit-testable).
    """

    def grade(self, context: dict) -> float:
        text = str(context.get("text", "")).lower()
        if not text:
            return 0.5
        bull = sum(text.count(w) for w in _BULLISH)
        bear = sum(text.count(w) for w in _BEARISH)
        total = bull + bear
        if total == 0:
            return 0.5
        return bull / total


class NewsVetoGrader:
    """Veto grader for reversal: low score when fresh news likely explains the move.

    ``context['has_fresh_news']`` truthy -> 0.0 (don't fade a real-news move);
    otherwise 1.0. Sorting on this separates clean technical reversions from
    falling knives.
    """

    def grade(self, context: dict) -> float:
        return 0.0 if context.get("has_fresh_news") else 1.0


class OpenAIQualityGrader:
    """Optional live grader using OpenAI. Returns 0..1 guidance-quality score.

    Lazy + best-effort: returns 0.5 (neutral) if the client/key is unavailable
    or the call fails, so a backtest never crashes on a single grading error.
    """

    def __init__(self, model: Optional[str] = None) -> None:
        self._model = model or os.getenv("LLM_MODEL_NARRATIVE", "gpt-5.5")

    def grade(self, context: dict) -> float:
        text = str(context.get("text", "")).strip()
        if not text:
            return 0.5
        try:
            import openai

            key = os.getenv("OPENAI_API_KEY", "").strip()
            if not key:
                return 0.5
            client = openai.OpenAI(api_key=key)
            prompt = (
                "You are a sell-side analyst grading the FORWARD quality of an "
                "earnings report from its call text. Output ONLY a JSON object "
                '{"quality": <float 0..1>} where 1 = strongly bullish guidance/'
                "tone likely to drive multi-week drift up, 0 = strongly bearish. "
                "Text:\n" + text[:6000]
            )
            resp = client.chat.completions.create(
                model=self._model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
            )
            content = resp.choices[0].message.content or ""
            return _parse_quality(content)
        except Exception as exc:  # pragma: no cover - network path
            _LOG.warning("OpenAI grader failed, neutral score: %s", exc)
            return 0.5


def _parse_quality(content: str) -> float:
    import json
    import re

    m = re.search(r"\{.*\}", content, re.DOTALL)
    if m:
        try:
            v = float(json.loads(m.group(0)).get("quality"))
            return max(0.0, min(1.0, v))
        except Exception:
            pass
    m2 = re.search(r"(\d*\.?\d+)", content)
    if m2:
        try:
            return max(0.0, min(1.0, float(m2.group(1))))
        except Exception:
            pass
    return 0.5


# ---------------------------------------------------------------------------
# Quintile assignment + incremental-edge analysis
# ---------------------------------------------------------------------------

def _event_key(ev: SignalEvent) -> str:
    return f"{ev.ticker}:{ev.signal_date}"


def assign_quality_quintiles(
    events: List[SignalEvent],
    grader: QualityGrader,
    context_fn: Callable[[SignalEvent], dict],
) -> List[SignalEvent]:
    """Grade each event, rank-bucket scores into quintiles, return tagged events.

    Q1 = lowest quality, Q5 = highest. Buckets are by rank position so they stay
    balanced even when raw scores cluster.
    """
    scored = [(ev, grader.grade(context_fn(ev))) for ev in events]
    n = len(scored)
    if n == 0:
        return list(events)

    order = sorted(range(n), key=lambda i: scored[i][1])
    quint_by_index: Dict[int, str] = {}
    for pos, i in enumerate(order):
        q = min(5, int(pos / n * 5) + 1)
        quint_by_index[i] = f"Q{q}"

    out: List[SignalEvent] = []
    for i, (ev, score) in enumerate(scored):
        tags = dict(ev.tags)
        tags["quality_quintile"] = quint_by_index[i]
        tags["quality_score"] = f"{score:.3f}"
        out.append(replace(ev, tags=tags))
    return out


def incremental_edge(results: List[TradeResult]) -> dict:
    """Compare top vs bottom quality quintile against the full sample.

    Returns a verdict on whether the overlay adds edge: the top quintile must
    out-earn the full sample AND beat the bottom quintile (monotone-ish sort).
    """
    full = summarize(results)
    per_q = group_by_tag(results, "quality_quintile")
    top = per_q.get("Q5")
    bottom = per_q.get("Q1")

    top_avg = top.avg_net_return if top else 0.0
    bottom_avg = bottom.avg_net_return if bottom else 0.0
    spread = top_avg - bottom_avg
    lift_vs_full = top_avg - full.avg_net_return
    adds_edge = bool(top and bottom and top_avg > full.avg_net_return and spread > 0)

    return {
        "full_sample_avg_net": full.avg_net_return,
        "top_quintile_avg_net": round(top_avg, 6),
        "bottom_quintile_avg_net": round(bottom_avg, 6),
        "top_minus_bottom_spread": round(spread, 6),
        "top_lift_vs_full": round(lift_vs_full, 6),
        "adds_incremental_edge": adds_edge,
        "by_quintile": {k: v.to_dict() for k, v in per_q.items()},
    }


def run_quality_overlay(
    events: List[SignalEvent],
    price_provider: PriceProvider,
    grader: QualityGrader,
    context_fn: Callable[[SignalEvent], dict],
    *,
    cost_model: Optional[CostModel] = None,
    oos_start: Optional[str] = "2023-01-01",
) -> dict:
    """Full Pass B: grade -> quintile -> event study -> incremental-edge verdict.

    When ``oos_start`` is set, the incremental-edge verdict is computed on the
    out-of-sample slice (the honest test); full-sample is also returned.
    """
    tagged = assign_quality_quintiles(events, grader, context_fn)
    outcome = run_event_study(tagged, price_provider, cost_model=cost_model or CostModel())

    results = outcome.results
    out: Dict[str, dict] = {"full": incremental_edge(results)}
    if oos_start:
        oos = [r for r in results if r.entry_date >= oos_start]
        out["out_of_sample"] = incremental_edge(oos)
    out["coverage"] = {"evaluated": outcome.n, "skipped": len(outcome.skipped)}
    return out
