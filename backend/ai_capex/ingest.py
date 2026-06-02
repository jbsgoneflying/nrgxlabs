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
