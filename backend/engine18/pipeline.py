"""Engine 18 pipeline — ingest -> grade -> score -> Redis snapshot.

``build_scan()`` is the heavy path (network + LLM), run by cron / background
refresh. ``rescore_from_store()`` rebuilds the payload from stored evidence
with zero network calls (used when scoring knobs change).

All external dependencies are injectable for tests.
"""
from __future__ import annotations

import datetime as dt
import logging
import time
from typing import Any, Dict, List, Optional

from backend.config import FeatureFlags, get_flags
from backend.engine18 import ingest as ing
from backend.engine18 import store as st
from backend.engine18.grade import grade_transcript
from backend.engine18.models import (
    DriftCandidate,
    EarningsReport,
    QualityGrade,
    candidates_to_payload,
    utcnow_iso,
)
from backend.engine18.score import score_candidate, surprise_bucket

LOG = logging.getLogger(__name__)


def _process_report(
    rep: EarningsReport,
    *,
    f: FeatureFlags,
    trailing: List[float],
    regime: Optional[str],
    eodhd_client=None,
    transcript_provider=None,
    llm_fn=None,
    store=None,
) -> Dict[str, Any]:
    """Liquidity -> transcript -> grade -> score -> evidence for ONE report.

    Shared by the auto-scan loop and the manual profile so both paths stay
    byte-identical to the validated pipeline. Returns a dict with
    ``candidate`` (None if liquidity-skipped or non-qualifying), ``grade``,
    ``skipped_liquidity``, and ``adv_usd``.
    """
    adv_usd, last_close = ing.fetch_liquidity(rep.ticker, client=eodhd_client)
    if adv_usd is not None and adv_usd < float(f.ENGINE18_MIN_ADV_USD):
        return {"candidate": None, "grade": None, "skipped_liquidity": True, "adv_usd": adv_usd}

    text = ing.fetch_transcript(rep.ticker, rep.report_date, provider=transcript_provider)
    grade = grade_transcript(
        text,
        model=str(f.ENGINE18_MODEL),
        trailing=trailing,
        llm_fn=llm_fn,
    )
    cand = score_candidate(
        rep,
        grade,
        min_surprise=float(f.ENGINE18_MIN_SURPRISE),
        large_surprise=float(f.ENGINE18_LARGE_SURPRISE),
        hold_days=int(f.ENGINE18_HOLD_DAYS),
        adv_usd=adv_usd,
        last_close=last_close,
        regime_context=regime,
    )
    if cand is not None:
        st.set_evidence(
            rep.ticker,
            {
                "report": rep.to_dict(),
                "grade": grade.to_dict(),
                "transcriptChars": len(text),
                "transcriptExcerpt": text[:1200],
                "advUsd": adv_usd,
                "gradedAt": utcnow_iso(),
            },
            ttl_s=int(f.ENGINE18_EVIDENCE_TTL_S),
            store=store,
        )
    return {"candidate": cand, "grade": grade, "skipped_liquidity": False, "adv_usd": adv_usd}


def build_scan(
    *,
    flags: Optional[FeatureFlags] = None,
    eodhd_client=None,
    transcript_provider=None,
    llm_fn=None,
    store=None,
    as_of: Optional[dt.date] = None,
    universe: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Full scan: fetch fresh reports, grade qualifying beats, persist, return payload."""
    f = flags or get_flags()
    t0 = time.time()
    status: Dict[str, Any] = {"startedAt": utcnow_iso(), "ok": False}

    try:
        reports = ing.fetch_recent_reports(
            lookback_days=int(f.ENGINE18_LOOKBACK_DAYS),
            as_of=as_of,
            client=eodhd_client,
            universe=universe,
        )
        status["reportsFound"] = len(reports)

        # Only beats above the floor are graded — misses/sub-threshold beats
        # never spend an LLM call (the bake-off says they're not tradable).
        qualifying = [
            r for r in reports
            if surprise_bucket(
                r.surprise_pct,
                min_surprise=float(f.ENGINE18_MIN_SURPRISE),
                large_surprise=float(f.ENGINE18_LARGE_SURPRISE),
            )
        ]
        status["qualifyingBeats"] = len(qualifying)

        trailing = st.get_trailing_grades(store=store)
        regime = ing.fetch_regime_context()

        candidates: List[DriftCandidate] = []
        new_scores: List[float] = []
        grade_log: List[Dict[str, Any]] = []
        skipped_liquidity = 0

        for rep in qualifying:
            result = _process_report(
                rep,
                f=f,
                trailing=trailing + new_scores,
                regime=regime,
                eodhd_client=eodhd_client,
                transcript_provider=transcript_provider,
                llm_fn=llm_fn,
                store=store,
            )
            if result["skipped_liquidity"]:
                skipped_liquidity += 1
                continue
            cand = result["candidate"]
            grade = result["grade"]
            if cand is None:
                continue
            candidates.append(cand)

            if grade.transcript_found:
                new_scores.append(grade.score)
                grade_log.append({
                    "ticker": rep.ticker,
                    "reportDate": rep.report_date,
                    "llmScore": grade.score if grade.source == "llm" else None,
                    "heuristicScore": grade.heuristic_score,
                    "source": grade.source,
                    "gradedAt": utcnow_iso(),
                })

        if new_scores:
            st.append_trailing_grades(
                new_scores,
                max_len=int(f.ENGINE18_TRAILING_GRADES_MAX),
                ttl_s=365 * 86400,
                store=store,
            )
        if grade_log:
            st.append_grade_log(grade_log, ttl_s=365 * 86400, store=store)

        payload = candidates_to_payload(
            candidates,
            meta={
                "reportsFound": len(reports),
                "qualifyingBeats": len(qualifying),
                "skippedLiquidity": skipped_liquidity,
                "lookbackDays": int(f.ENGINE18_LOOKBACK_DAYS),
                "minSurprise": float(f.ENGINE18_MIN_SURPRISE),
                "largeSurprise": float(f.ENGINE18_LARGE_SURPRISE),
                "holdDays": int(f.ENGINE18_HOLD_DAYS),
                "minAdvUsd": float(f.ENGINE18_MIN_ADV_USD),
                "regimeContext": regime,
                "buildSeconds": round(time.time() - t0, 1),
            },
        )
        st.set_scan(payload, ttl_s=int(f.ENGINE18_SCAN_TTL_S), store=store)

        status.update({
            "ok": True,
            "candidates": len(candidates),
            "actionable": payload["summary"]["actionable"],
            "elapsedS": round(time.time() - t0, 1),
            "finishedAt": utcnow_iso(),
        })
        return payload
    except Exception as exc:
        LOG.exception("engine18: build_scan failed")
        status.update({"ok": False, "error": str(exc), "finishedAt": utcnow_iso()})
        raise
    finally:
        st.set_last_run(status, store=store)


def _business_days_since(date: str, today: dt.date) -> int:
    d = dt.date.fromisoformat(str(date)[:10])
    n = 0
    while d < today:
        d += dt.timedelta(days=1)
        if d.weekday() < 5:
            n += 1
    return n


def _merge_candidate_into_scan(
    cand: DriftCandidate, *, f: FeatureFlags, store=None
) -> Dict[str, Any]:
    """Replace/insert one candidate in the stored scan payload and re-persist."""
    prior = st.get_scan(store=store)
    existing: List[DriftCandidate] = []
    meta: Dict[str, Any] = {}
    if isinstance(prior, dict):
        meta = dict(prior.get("meta") or {})
        existing = [
            DriftCandidate.from_dict(row)
            for row in (prior.get("candidates") or [])
            if str(row.get("ticker") or "").upper() != cand.ticker.upper()
        ]
    meta["lastManualProfile"] = {"ticker": cand.ticker, "at": utcnow_iso()}
    payload = candidates_to_payload(existing + [cand], meta=meta)
    st.set_scan(payload, ttl_s=int(f.ENGINE18_SCAN_TTL_S), store=store)
    return payload


def build_profile(
    ticker: str,
    *,
    overrides: Optional[Dict[str, Any]] = None,
    flags: Optional[FeatureFlags] = None,
    eodhd_client=None,
    fmp_client=None,
    transcript_provider=None,
    llm_fn=None,
    store=None,
    as_of: Optional[dt.date] = None,
) -> Dict[str, Any]:
    """On-demand single-ticker PEAD profile (the manual desk entry path).

    Runs the EXACT validated pipeline on one name and always returns an
    explicit verdict — including for reports the bake-off says are not
    tradable (misses / sub-floor beats), which the auto-scan silently drops.
    Qualifying candidates are merged into the stored scan (tagged
    ``origin="manual"``) so the page, tracker, and Desk Brain all see them.
    """
    f = flags or get_flags()
    ticker = str(ticker or "").strip().upper()
    today = as_of or dt.date.today()

    rep, eps_source, reason = ing.fetch_report_for_ticker(
        ticker,
        as_of=today,
        client=eodhd_client,
        fmp_client=fmp_client,
        overrides=overrides,
    )
    if rep is None:
        return {"found": False, "verdict": "no_report", "reason": reason}

    bucket = surprise_bucket(
        rep.surprise_pct,
        min_surprise=float(f.ENGINE18_MIN_SURPRISE),
        large_surprise=float(f.ENGINE18_LARGE_SURPRISE),
    )
    if bucket is None:
        s = rep.surprise_pct
        if s is None:
            why = "Surprise not computable (estimate is zero/missing)."
        elif s < 0:
            why = (
                f"EPS miss ({s * 100:+.1f}%) — not tradable. The bake-off shows "
                "misses LOSE money long (-0.57%/trade) and shorting them also "
                "loses. PEAD is long qualifying beats only."
            )
        else:
            why = (
                f"Beat of {s * 100:+.1f}% is below the {float(f.ENGINE18_MIN_SURPRISE) * 100:.0f}% "
                "floor — sub-threshold beats carried no validated edge."
            )
        return {
            "found": True,
            "verdict": "not_tradable",
            "reason": why,
            "source": eps_source,
            "report": rep.to_dict(),
        }

    trailing = st.get_trailing_grades(store=store)
    regime = ing.fetch_regime_context()
    result = _process_report(
        rep,
        f=f,
        trailing=trailing,
        regime=regime,
        eodhd_client=eodhd_client,
        transcript_provider=transcript_provider,
        llm_fn=llm_fn,
        store=store,
    )
    if result["skipped_liquidity"]:
        return {
            "found": True,
            "verdict": "illiquid",
            "reason": (
                f"ADV ${(result['adv_usd'] or 0) / 1e6:.1f}M is below the "
                f"${float(f.ENGINE18_MIN_ADV_USD) / 1e6:.0f}M floor the edge was validated on."
            ),
            "source": eps_source,
            "report": rep.to_dict(),
        }

    cand: Optional[DriftCandidate] = result["candidate"]
    grade: QualityGrade = result["grade"]
    if cand is None:  # defensive: bucket qualified above, so this shouldn't happen
        return {
            "found": True,
            "verdict": "not_tradable",
            "reason": "Report did not score as a candidate.",
            "source": eps_source,
            "report": rep.to_dict(),
        }

    cand.origin = "manual"
    cand.eps_source = eps_source
    days_late = _business_days_since(cand.entry_date, today)
    cand.entry_status = "late" if days_late > 0 else "on_time"
    cand.days_late = days_late

    if grade.transcript_found:
        st.append_trailing_grades(
            [grade.score],
            max_len=int(f.ENGINE18_TRAILING_GRADES_MAX),
            ttl_s=365 * 86400,
            store=store,
        )
        st.append_grade_log(
            [{
                "ticker": rep.ticker,
                "reportDate": rep.report_date,
                "llmScore": grade.score if grade.source == "llm" else None,
                "heuristicScore": grade.heuristic_score,
                "source": grade.source,
                "origin": "manual",
                "gradedAt": utcnow_iso(),
            }],
            ttl_s=365 * 86400,
            store=store,
        )

    _merge_candidate_into_scan(cand, f=f, store=store)

    out: Dict[str, Any] = {
        "found": True,
        "verdict": "candidate",
        "source": eps_source,
        "candidate": cand.to_dict(),
    }
    if cand.entry_status == "late":
        out["warning"] = (
            f"Validated entry was the {cand.entry_date} open — you are "
            f"{days_late} trading day{'s' if days_late != 1 else ''} late. "
            "Mid-drift entries were never backtested; expected stats do not apply."
        )
    return out


def rescore_from_store(
    *,
    flags: Optional[FeatureFlags] = None,
    store=None,
) -> Optional[Dict[str, Any]]:
    """Rebuild the payload from stored evidence (no network, no LLM).

    Returns None when there is no prior scan to rescore from.
    """
    f = flags or get_flags()
    prior = st.get_scan(store=store)
    if not isinstance(prior, dict):
        return None
    candidates: List[DriftCandidate] = []
    for row in prior.get("candidates") or []:
        old = DriftCandidate.from_dict(row)
        ev = st.get_evidence(old.ticker, store=store)
        rep = EarningsReport.from_dict((ev or {}).get("report") or old.report.to_dict())
        grade = QualityGrade.from_dict((ev or {}).get("grade") or old.grade.to_dict())
        cand = score_candidate(
            rep,
            grade,
            min_surprise=float(f.ENGINE18_MIN_SURPRISE),
            large_surprise=float(f.ENGINE18_LARGE_SURPRISE),
            hold_days=int(f.ENGINE18_HOLD_DAYS),
            adv_usd=old.adv_usd,
            last_close=old.last_close,
            regime_context=old.regime_context,
        )
        if cand is not None:
            candidates.append(cand)
    payload = candidates_to_payload(
        candidates,
        meta={**(prior.get("meta") or {}), "rescoredAt": utcnow_iso()},
    )
    st.set_scan(payload, ttl_s=int(f.ENGINE18_SCAN_TTL_S), store=store)
    return payload
