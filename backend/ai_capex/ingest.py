"""Tier-1 evidence ingestion for the AI Capex Reality Engine.

Pulls the raw material the LLM extractor reasons over — using only data the
desk already pays for:

- Earnings-call transcripts (API Ninjas) — the richest source: capex guidance,
  GPU/supply-chain commentary, cloud backlog, vendor + lead-time mentions.
- News headlines (Benzinga + EODHD) — PPAs, construction delays, fiber, ratings.
- Capex fundamentals (EODHD) — reported capex trend (a hard "is it real" tell).

It also assembles a per-ticker *market context* (price momentum, valuation,
analyst-rating drift) that the deterministic scorer uses to compute the
Consensus Gap. Everything is defensive: a missing client / failed call yields
an empty bundle, never an exception.
"""
from __future__ import annotations

import datetime as dt
import logging
from typing import Any, Dict, List, Optional

from backend.ai_capex import models
from backend.ai_capex.models import CapexEvidence

LOG = logging.getLogger("ai_capex.ingest")


# ---------------------------------------------------------------------------
# Client helpers (optional — return None when keys/clients are unavailable)
# ---------------------------------------------------------------------------


def _eodhd_optional():
    try:
        from backend.eodhd_client import EodhdClient
        return EodhdClient.from_env()
    except Exception:
        return None


def _api_ninjas_optional():
    try:
        from backend.deps import get_api_ninjas_client_optional
        return get_api_ninjas_client_optional()
    except Exception:
        return None


def _benzinga_optional():
    try:
        from backend.deps import get_benzinga_client_optional
        return get_benzinga_client_optional()
    except Exception:
        return None


def _eodhd_symbol(ticker: str) -> str:
    t = str(ticker or "").upper().strip()
    return t if "." in t else f"{t}.US"


def _f(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Text gathering (input to the LLM extractor)
# ---------------------------------------------------------------------------


def _transcript_snippets(ticker: str, *, quarters: int, max_chars: int = 16000) -> List[Dict[str, Any]]:
    """Recent transcript text bundles for a ticker (newest first).

    Returns ``[{date, title, text}]``. Truncated per transcript so we don't
    feed the extractor a 100k-token call; the extractor only needs the capex /
    supply-chain language, which clusters in guidance + Q&A.
    """
    client = _api_ninjas_optional()
    if client is None:
        return []
    out: List[Dict[str, Any]] = []
    try:
        transcripts = client.get_transcript_history(ticker, quarters=int(quarters))
    except Exception as exc:  # pragma: no cover - defensive
        LOG.debug("transcript fetch failed for %s: %s", ticker, exc)
        return []
    for t in transcripts or []:
        text = str(t.get("transcript") or "").strip()
        if not text:
            continue
        y = t.get("year")
        q = t.get("quarter")
        out.append({
            "date": str(t.get("date") or "")[:10],
            "title": f"{ticker} Q{q} {y} earnings call",
            "text": text[:max_chars],
        })
    return out


def _news_items(ticker: str, *, lookback_days: int, limit: int) -> List[Dict[str, Any]]:
    """Recent headlines for a ticker from Benzinga (preferred) + EODHD."""
    items: List[Dict[str, Any]] = []
    today = dt.date.today()
    date_from = (today - dt.timedelta(days=int(lookback_days))).isoformat()

    bz = _benzinga_optional()
    if bz is not None:
        try:
            resp = bz.news(
                tickers=str(ticker).upper(),
                date_from=date_from,
                date_to=today.isoformat(),
                page_size=min(limit, 50),
                display_output="abstract",
            )
            for row in (getattr(resp, "rows", None) or []):
                if not isinstance(row, dict):
                    continue
                title = str(row.get("title") or "").strip()
                if not title:
                    continue
                body = str(row.get("teaser") or row.get("body") or "")[:600]
                items.append({
                    "date": str(row.get("created") or row.get("updated") or "")[:10],
                    "title": title,
                    "text": body,
                    "url": str(row.get("url") or ""),
                    "source": "benzinga",
                })
        except Exception as exc:  # pragma: no cover - defensive
            LOG.debug("benzinga news failed for %s: %s", ticker, exc)

    if len(items) < limit:
        eodhd = _eodhd_optional()
        if eodhd is not None:
            try:
                resp = eodhd.get_news(
                    ticker=_eodhd_symbol(ticker),
                    from_date=date_from,
                    to_date=today.isoformat(),
                    limit=min(limit, 50),
                )
                for row in (getattr(resp, "rows", None) or []):
                    if not isinstance(row, dict):
                        continue
                    title = str(row.get("title") or "").strip()
                    if not title:
                        continue
                    items.append({
                        "date": str(row.get("date") or "")[:10],
                        "title": title,
                        "text": str(row.get("content") or "")[:600],
                        "url": str(row.get("link") or ""),
                        "source": "eodhd",
                    })
            except Exception as exc:  # pragma: no cover - defensive
                LOG.debug("eodhd news failed for %s: %s", ticker, exc)

    # De-dupe by lowercased title; keep newest order, cap at limit.
    seen = set()
    deduped: List[Dict[str, Any]] = []
    for it in items:
        key = it["title"].lower()[:120]
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)
        if len(deduped) >= limit:
            break
    return deduped


def gather_ticker_text(
    ticker: str,
    *,
    transcript_quarters: int = 2,
    news_lookback_days: int = 30,
    news_limit: int = 12,
) -> Dict[str, Any]:
    """Assemble the raw text bundle the LLM extractor reasons over."""
    ticker = str(ticker or "").upper().strip()
    return {
        "ticker": ticker,
        "transcripts": _transcript_snippets(ticker, quarters=transcript_quarters),
        "news": _news_items(ticker, lookback_days=news_lookback_days, limit=news_limit),
    }


# ---------------------------------------------------------------------------
# Reported-capex evidence (the "is the money actually going out the door" tell)
# ---------------------------------------------------------------------------


def _cash_flow_quarterly(fundamentals: Dict[str, Any]) -> Dict[str, Any]:
    """Best-effort extraction of EODHD's quarterly cash-flow block.

    EODHD nests it at Financials -> Cash_Flow -> quarterly (a date-keyed dict).
    Returns ``{}`` for any shape we don't recognise so callers stay defensive.
    """
    fin = fundamentals.get("Financials") if isinstance(fundamentals, dict) else None
    if not isinstance(fin, dict):
        return {}
    cf = fin.get("Cash_Flow") if isinstance(fin.get("Cash_Flow"), dict) else {}
    q = cf.get("quarterly") if isinstance(cf, dict) else {}
    return q if isinstance(q, dict) else {}


def capex_evidence_from_fundamentals(
    ticker: str, category: str, fundamentals: Dict[str, Any],
) -> Optional[CapexEvidence]:
    """Turn the audited cash-flow capex trend into ONE hard, independent evidence item.

    Management can talk up AI capex on the call, but the cash-flow statement
    shows whether the spend is actually happening. Because the scorer counts the
    ``fundamental`` feed as its own independent "voice" (distinct from the
    issuer's earnings narrative), this is what lets a genuinely-real name clear
    the corroboration bar — and what leaves an all-talk name stuck at one source.

    We compare the latest reported quarter's capex to the same quarter a year
    ago (4 periods back) to strip seasonality, and only emit a signal when the
    move is material (>= 8% YoY in either direction). Returns ``None`` when the
    data is missing, flat, or unparseable.
    """
    cf = _cash_flow_quarterly(fundamentals or {})
    if not cf:
        return None

    rows: List[tuple] = []
    for key, row in cf.items():
        if not isinstance(row, dict):
            continue
        capex = _f(row.get("capitalExpenditures"))
        if capex is None:
            continue
        when = str(row.get("date") or key)[:10]
        rows.append((when, abs(capex)))  # EODHD reports capex as a negative outflow
    if len(rows) < 5:
        return None
    rows.sort(reverse=True)  # newest first by date string

    latest_date, latest = rows[0]
    _, year_ago = rows[4]
    if latest <= 0 or year_ago <= 0:
        return None
    yoy = latest / year_ago - 1.0
    if abs(yoy) < 0.08:
        return None  # roughly flat — no decisive "is it real" signal

    bn = latest / 1e9
    amount = f"${bn:.1f}B" if bn >= 1.0 else f"${latest/1e6:.0f}M"
    # Material moves get more magnitude; audited numbers get high confidence.
    magnitude = max(0.0, min(1.0, 0.45 + min(abs(yoy), 1.0) * 0.5))
    rising = yoy >= 0.0
    claim = (
        f"Reported capex {amount} in the quarter ending {latest_date}, "
        f"{yoy * 100:+.0f}% YoY (audited cash-flow statement) — "
        f"{'spend is accelerating' if rising else 'spend is contracting'}."
    )
    return CapexEvidence(
        ticker=ticker,
        category=category,
        source_type=models.SOURCE_FUNDAMENTAL,
        signal_type=models.SIG_CAPEX_UP if rising else models.SIG_CAPEX_DOWN,
        claim=claim,
        date=latest_date,
        source_title="Reported capex (cash-flow statement)",
        magnitude=magnitude,
        timing=models.TIMING_NEAR,   # already realised, not guidance
        polarity=1 if rising else -1,
        confidence=0.9,
    )


def fetch_capex_fundamental_evidence(ticker: str, category: str) -> Optional[CapexEvidence]:
    """Fetch EODHD fundamentals and derive the reported-capex evidence item."""
    ticker = str(ticker or "").upper().strip()
    eodhd = _eodhd_optional()
    if eodhd is None:
        return None
    try:
        fund = eodhd.get_fundamentals(_eodhd_symbol(ticker)) or {}
    except Exception as exc:  # pragma: no cover - defensive
        LOG.debug("fundamentals capex fetch failed for %s: %s", ticker, exc)
        return None
    return capex_evidence_from_fundamentals(ticker, category, fund)


# ---------------------------------------------------------------------------
# Market context (input to the deterministic Consensus-Gap scorer)
# ---------------------------------------------------------------------------


def _momentum_pct(bars: List[Dict[str, Any]], lookback: int) -> Optional[float]:
    """% change of close over the last ``lookback`` trading days."""
    closes = [
        _f(b.get("adjusted_close") if b.get("adjusted_close") is not None else b.get("close"))
        for b in bars
    ]
    closes = [c for c in closes if c is not None and c > 0]
    if len(closes) <= lookback:
        return None
    recent = closes[-1]
    past = closes[-1 - lookback]
    if past <= 0:
        return None
    return round((recent / past - 1.0) * 100.0, 2)


def fetch_market_context(ticker: str) -> Dict[str, Any]:
    """Price momentum + valuation + analyst-rating drift for the Consensus Gap.

    Returns a dict with (all optional):
    - ``momentum3mPct`` / ``momentum6mPct`` — trailing price change.
    - ``pe`` — trailing P/E (valuation richness proxy).
    - ``ratingDrift`` — net upgrades(-)/downgrades over ~90d from Benzinga.
    """
    ticker = str(ticker or "").upper().strip()
    ctx: Dict[str, Any] = {
        "momentum3mPct": None, "momentum6mPct": None,
        "pe": None, "ratingDrift": 0, "ratingCount": 0,
    }

    eodhd = _eodhd_optional()
    if eodhd is not None:
        try:
            today = dt.date.today()
            frm = (today - dt.timedelta(days=320)).isoformat()
            resp = eodhd.get_eod(_eodhd_symbol(ticker), from_date=frm, to_date=today.isoformat())
            bars = [r for r in (getattr(resp, "rows", None) or []) if isinstance(r, dict)]
            ctx["momentum3mPct"] = _momentum_pct(bars, 63)
            ctx["momentum6mPct"] = _momentum_pct(bars, 126)
        except Exception as exc:  # pragma: no cover - defensive
            LOG.debug("eod momentum failed for %s: %s", ticker, exc)
        try:
            fund = eodhd.get_fundamentals(_eodhd_symbol(ticker)) or {}
            highlights = fund.get("Highlights") or {}
            ctx["pe"] = _f(highlights.get("PERatio"))
        except Exception as exc:  # pragma: no cover - defensive
            LOG.debug("fundamentals failed for %s: %s", ticker, exc)

    bz = _benzinga_optional()
    if bz is not None:
        try:
            today = dt.date.today()
            frm = (today - dt.timedelta(days=90)).isoformat()
            resp = bz.calendar_ratings(
                tickers=str(ticker).upper(),
                date_from=frm,
                date_to=today.isoformat(),
                pagesize=50,
            )
            up = down = 0
            for row in (getattr(resp, "rows", None) or []):
                if not isinstance(row, dict):
                    continue
                action = str(row.get("action_company") or row.get("action_pt") or "").lower()
                if any(k in action for k in ("upgrade", "raise", "initiat", "overweight", "buy")):
                    up += 1
                elif any(k in action for k in ("downgrade", "lower", "cut", "underweight", "sell")):
                    down += 1
            ctx["ratingDrift"] = up - down   # +ve = net upgrades (consensus already bullish)
            ctx["ratingCount"] = up + down
        except Exception as exc:  # pragma: no cover - defensive
            LOG.debug("ratings failed for %s: %s", ticker, exc)

    return ctx
