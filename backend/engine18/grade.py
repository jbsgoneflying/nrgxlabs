"""Engine 18 transcript quality grading.

Primary grader: OpenAI (per desk decision) — returns a 0..1 quality score plus
a short rationale for the candidate card. The validated heuristic keyword
grader (the exact artifact the bake-off Pass B was measured with) ALWAYS runs
too: it is the fallback when the LLM is unavailable, and both scores are logged
per event so the live LLM accumulates a validation sample against the
backtested proxy.

Quintiles are assigned against a trailing distribution of scores
(``e18:grades:trailing``). Cold start is seeded with a fixed distribution
approximating the bake-off overlay run, and washes out as live grades
accumulate.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import List, Optional, Tuple

from backend.engine18.models import QualityGrade

LOG = logging.getLogger(__name__)

# Seed for the trailing distribution before live grades accumulate. Shape
# approximates the bake-off heuristic-overlay run on S&P 500 transcripts
# (keyword bull-ratio, centered ~0.55-0.60 — big-cap calls skew bullish).
COLD_START_SCORES: List[float] = [
    0.20, 0.28, 0.33, 0.38, 0.42,
    0.45, 0.48, 0.50, 0.52, 0.54,
    0.56, 0.58, 0.60, 0.62, 0.64,
    0.66, 0.68, 0.71, 0.75, 0.82,
]

_MIN_TRAILING_FOR_LIVE = 25  # below this, blend in the cold-start seed

_QUINTILES = ("Q1", "Q2", "Q3", "Q4", "Q5")


def heuristic_score(text: str) -> float:
    """The validated keyword grader from the bake-off (single source of truth)."""
    try:
        from backend.research.strategies.llm_overlay import HeuristicGuidanceGrader

        return float(HeuristicGuidanceGrader().grade({"text": text}))
    except Exception as exc:
        LOG.warning("engine18: heuristic grader unavailable: %s", exc)
        return 0.5


def llm_score(text: str, *, model: str) -> Optional[Tuple[float, str]]:
    """OpenAI quality grade -> (score 0..1, short rationale). None on failure."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        import openai

        key = os.getenv("OPENAI_API_KEY", "").strip()
        if not key:
            return None
        client = openai.OpenAI(api_key=key)
        prompt = (
            "You are a sell-side analyst grading the FORWARD quality of an "
            "earnings report from its call text for a 10-trading-day "
            "post-earnings drift trade. Output ONLY a JSON object "
            '{"quality": <float 0..1>, "rationale": "<one sentence, <=160 chars>"} '
            "where quality 1 = strongly bullish guidance/tone likely to drive "
            "multi-week drift up, 0 = strongly bearish. "
            "Text:\n" + text[:6000]
        )
        # gpt-5.5 rejects non-default temperature (see 225f89b) — the request
        # 400s and every grade silently falls back to the heuristic.
        resp = client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=1,
        )
        content = resp.choices[0].message.content or ""
        return _parse_grade(content)
    except Exception as exc:  # pragma: no cover - network path
        LOG.warning("engine18: OpenAI grader failed: %s", exc)
        return None


def _parse_grade(content: str) -> Optional[Tuple[float, str]]:
    m = re.search(r"\{.*\}", content, re.DOTALL)
    if m:
        try:
            doc = json.loads(m.group(0))
            v = float(doc.get("quality"))
            rationale = str(doc.get("rationale") or "")[:200]
            return max(0.0, min(1.0, v)), rationale
        except Exception:
            pass
    m2 = re.search(r"(\d*\.?\d+)", content)
    if m2:
        try:
            return max(0.0, min(1.0, float(m2.group(1)))), ""
        except Exception:
            pass
    return None


def quintile_for(score: float, trailing: List[float]) -> str:
    """Bucket a score into Q1..Q5 by percentile vs the trailing distribution."""
    basis = list(trailing or [])
    if len(basis) < _MIN_TRAILING_FOR_LIVE:
        basis = basis + COLD_START_SCORES
    below = sum(1 for x in basis if x < score)
    ties = sum(1 for x in basis if x == score)
    pct = (below + 0.5 * ties) / len(basis)
    idx = min(4, int(pct * 5))
    return _QUINTILES[idx]


def grade_transcript(
    text: str,
    *,
    model: str,
    trailing: List[float],
    llm_fn=None,
) -> QualityGrade:
    """Grade one transcript: LLM primary, heuristic fallback, both logged.

    ``llm_fn`` is injectable for tests (signature: text -> (score, rationale) | None).
    """
    text = (text or "").strip()
    h_score = heuristic_score(text) if text else 0.5
    if not text:
        # No transcript -> neutral grade; quintile vs trailing keeps it honest
        # (a neutral 0.5 in a strong trailing window lands mid/low).
        return QualityGrade(
            score=0.5,
            source="none",
            heuristic_score=h_score,
            quintile=quintile_for(0.5, trailing),
            rationale="No transcript available — neutral quality assumed.",
            transcript_found=False,
        )

    call = llm_fn if llm_fn is not None else (lambda t: llm_score(t, model=model))
    result = call(text)
    if result is not None:
        score, rationale = result
        source = "llm"
    else:
        score, rationale, source = h_score, "Heuristic keyword grade (LLM unavailable).", "heuristic"

    return QualityGrade(
        score=float(score),
        source=source,
        heuristic_score=h_score,
        quintile=quintile_for(float(score), trailing),
        rationale=rationale,
        transcript_found=True,
    )
