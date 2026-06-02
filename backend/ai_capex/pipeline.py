"""Orchestration for the AI Capex Reality Engine.

Ties the pieces together:

- ``build_scan``        — the heavy path: ingest text + market context, run the
  LLM extractor (and optional Tier-2 web agent), persist evidence, score the
  universe, attach trades, and cache the scan payload. Used by the nightly
  refresh job and the manual ``/refresh`` route.
- ``rescore_from_store``— the cheap path: rebuild verdicts from already-persisted
  evidence (no LLM/network), so the API can serve something useful between
  nightly rebuilds.

Both share ``_assemble`` so the payload shape is identical.
"""
from __future__ import annotations

import datetime as dt
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional

from backend.ai_capex import agent, extract, horizon, ingest, models, score, store, trades
from backend.ai_capex.models import CapexEvidence, TickerVerdict

LOG = logging.getLogger("ai_capex.pipeline")


def _has_llm_grade(evidence: List[CapexEvidence]) -> bool:
    """True if the trail has at least one LLM-extracted (non-fallback) item.

    Keyword-fallback evidence caps at confidence 0.35 and the deterministic
    fundamentals item is its own (always-present) thing, so a real LLM pass is
    the only thing that yields a non-fundamental item at confidence >= 0.5.
    """
    return any(
        getattr(e, "confidence", 0.0) >= 0.5 and e.source_type != models.SOURCE_FUNDAMENTAL
        for e in (evidence or [])
    )


def _summarize(verdicts: List[TickerVerdict]) -> Dict[str, Any]:
    by_label: Dict[str, int] = {}
    by_category: Dict[str, int] = {}
    actionable = 0
    for v in verdicts:
        by_label[v.label] = by_label.get(v.label, 0) + 1
        if v.category:
            by_category[v.category] = by_category.get(v.category, 0) + 1
        if v.is_actionable:
            actionable += 1
    return {
        "total": len(verdicts),
        "actionable": actionable,
        "byLabel": by_label,
        "byCategory": by_category,
    }


def _assemble(
    verdicts: List[TickerVerdict],
    baskets: List[Dict[str, Any]],
    *,
    source: str,
    evidence_total: int,
    web_evidence: int = 0,
) -> Dict[str, Any]:
    return {
        "asOf": dt.datetime.utcnow().isoformat() + "Z",
        "engine": 17,
        "engineName": "AI Capex Reality Engine",
        "source": source,                     # "scan" | "rescore"
        "evidenceTotal": evidence_total,
        "webEvidence": web_evidence,
        "verdicts": [v.to_dict() for v in verdicts],
        "baskets": baskets,
        "summary": _summarize(verdicts),
        "labels": models.LABEL_DISPLAY,
        "categories": {
            cid: {"name": meta.get("name"), "role": meta.get("role"), "blurb": meta.get("blurb")}
            for cid, meta in models.load_universe().get("categories", {}).items()
        },
        "cached": False,
    }


def build_scan(
    *,
    flags: Any = None,
    tickers: Optional[List[str]] = None,
    with_web_agent: bool = False,
    orats_client: Any = None,
    store_obj: Any = None,
    persist: bool = True,
) -> Dict[str, Any]:
    """Full rebuild: ingest -> extract -> (web agent) -> score -> trades -> cache."""
    if flags is None:
        from backend.config import get_flags
        flags = get_flags()
    if orats_client is None:
        # Wire ORATS in by default so trade ideas carry IV + a catalyst horizon
        # (next-earnings date + implied move). Optional: None just degrades to a
        # structural horizon and "IV rank n/a".
        try:
            from backend.deps import get_client_optional
            orats_client = get_client_optional()
        except Exception:
            orats_client = None
    tickers = tickers or models.all_tickers()
    tcat = models.ticker_to_category_map()
    model = str(getattr(flags, "AI_CAPEX_MODEL", "gpt-5.5"))
    workers = max(1, int(getattr(flags, "AI_CAPEX_MAX_WORKERS", 6)))
    quarters = int(getattr(flags, "AI_CAPEX_TRANSCRIPT_QUARTERS", 2))
    news_days = int(getattr(flags, "AI_CAPEX_NEWS_LOOKBACK_DAYS", 30))
    news_n = int(getattr(flags, "AI_CAPEX_NEWS_PER_TICKER", 12))
    evidence_ttl = int(getattr(flags, "AI_CAPEX_EVIDENCE_TTL_S", 14 * 86400))

    evidence_by_ticker: Dict[str, List[CapexEvidence]] = {t: [] for t in tickers}
    context_by_ticker: Dict[str, Dict[str, Any]] = {}
    orats_by_ticker: Dict[str, Dict[str, Any]] = {}

    def _work(ticker: str):
        cat = tcat.get(ticker, "")
        bundle = ingest.gather_ticker_text(
            ticker, transcript_quarters=quarters,
            news_lookback_days=news_days, news_limit=news_n,
        )
        evid = list(extract.extract_evidence(ticker, cat, bundle, model=model) or [])
        # Hard cross-check: the audited cash-flow capex trend is an independent
        # "voice" from the earnings narrative, so it's what corroborates (or
        # exposes) a name whose only other evidence is its own call.
        fund_ev = ingest.fetch_capex_fundamental_evidence(ticker, cat)
        if fund_ev is not None:
            evid.append(fund_ev)
        ctx = ingest.fetch_market_context(ticker)
        orats = horizon.fetch_orats_timing(ticker, orats_client)
        return ticker, evid, ctx, orats

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_work, t): t for t in tickers}
        for fut in as_completed(futs):
            try:
                ticker, evid, ctx, orats = fut.result()
                evidence_by_ticker[ticker] = evid or []
                context_by_ticker[ticker] = ctx or {}
                orats_by_ticker[ticker] = orats or {}
            except Exception as exc:  # pragma: no cover - defensive
                LOG.warning("ai_capex: ticker work failed for %s: %s", futs[fut], exc)

    # Tier-2 web agent (batch-only; merges source-attributed web evidence).
    web_count = 0
    if with_web_agent and getattr(flags, "AI_CAPEX_ENABLE_WEB_AGENT", False):
        try:
            web_ev = agent.run_web_agent(
                tickers,
                model=str(getattr(flags, "AI_CAPEX_WEB_AGENT_MODEL", "gpt-5.5")),
                max_calls=int(getattr(flags, "AI_CAPEX_MAX_WEB_CALLS", 12)),
            )
            for ev in web_ev:
                evidence_by_ticker.setdefault(ev.ticker, []).append(ev)
            web_count = len(web_ev)
        except Exception as exc:  # pragma: no cover - defensive
            LOG.warning("ai_capex: web agent failed: %s", exc)

    # Reconcile with the previously-stored trail: if this run produced only
    # keyword-fallback evidence for a ticker (e.g. an LLM outage / rate-limit
    # storm) but we already have a richer LLM extraction on file, keep the prior
    # trail. This protects both the score and the persisted audit trail from a
    # degraded run silently overwriting good data.
    s_read = store_obj or (store._store() if persist else None)
    if s_read is not None:
        kept = 0
        for ticker, new_ev in list(evidence_by_ticker.items()):
            if _has_llm_grade(new_ev):
                continue
            prior = store.get_evidence(ticker, store=s_read) or []
            if _has_llm_grade(prior):
                evidence_by_ticker[ticker] = prior
                kept += 1
        if kept:
            LOG.warning("ai_capex: kept prior LLM evidence for %d ticker(s) (this run was fallback-grade)", kept)

    # Persist evidence per ticker (audit trail) + the market context snapshot so
    # a between-builds rescore can reproduce positioning/gaps faithfully.
    if persist:
        s = store_obj or store._store()
        if s is not None:
            for ticker, evid in evidence_by_ticker.items():
                if evid:
                    store.set_evidence(ticker, evid, ttl_s=evidence_ttl, store=s)
            store.set_context(context_by_ticker, ttl_s=evidence_ttl, store=s)

    verdicts = score.score_universe(evidence_by_ticker, context_by_ticker, flags=flags)
    for v in verdicts:
        v.horizon = horizon.derive_horizon(v, orats_by_ticker.get(v.ticker))
    baskets = trades.attach_trades(verdicts, orats_client=orats_client)
    evidence_total = sum(len(v) for v in evidence_by_ticker.values())

    payload = _assemble(verdicts, baskets, source="scan",
                        evidence_total=evidence_total, web_evidence=web_count)

    if persist:
        store.set_scan(payload, ttl_s=int(getattr(flags, "AI_CAPEX_CACHE_TTL_S", 6 * 60 * 60)),
                       store=store_obj)
    return payload


def rescore_from_store(
    *, flags: Any = None, store_obj: Any = None, persist: bool = True,
) -> Optional[Dict[str, Any]]:
    """Cheap rebuild from persisted evidence + context (no LLM/network).

    Re-applies the current scoring rules/thresholds to already-stored evidence,
    using the persisted market-context snapshot so positioning and gaps match a
    full build. The fast path for threshold tuning and the between-builds API
    fallback. Returns None if no persisted evidence exists.
    """
    if flags is None:
        from backend.config import get_flags
        flags = get_flags()
    s = store_obj or store._store()
    tickers = models.all_tickers()

    evidence_by_ticker: Dict[str, List[CapexEvidence]] = {}
    for ticker in tickers:
        evid = store.get_evidence(ticker, store=s)
        if evid:
            evidence_by_ticker[ticker] = evid
    if not evidence_by_ticker:
        return None

    context_by_ticker = store.get_context(store=s) or {}
    if not context_by_ticker:
        # Backfill from the last scan's per-verdict context (scans built before
        # the standalone context snapshot existed still carry the raw inputs).
        last = store.get_scan(store=s) or {}
        for vd in last.get("verdicts", []):
            mc = vd.get("marketContext") or {}
            tk = vd.get("ticker")
            if mc and tk:
                context_by_ticker[tk] = {k: v for k, v in mc.items() if k != "marketPositioning"}

    verdicts = score.score_universe(evidence_by_ticker, context_by_ticker, flags=flags)
    for v in verdicts:
        v.horizon = horizon.derive_horizon(v, {})  # structural-only (no network on rescore)
    baskets = trades.attach_trades(verdicts, orats_client=None)
    evidence_total = sum(len(v) for v in evidence_by_ticker.values())
    payload = _assemble(verdicts, baskets, source="rescore", evidence_total=evidence_total)

    if persist:
        store.set_scan(payload, ttl_s=int(getattr(flags, "AI_CAPEX_CACHE_TTL_S", 6 * 60 * 60)),
                       store=s)
    return payload
