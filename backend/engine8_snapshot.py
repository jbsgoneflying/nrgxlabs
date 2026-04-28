"""Engine 8 – Post-Event State Capture.

Assembles the complete decision state from ORATS, EODHD, Benzinga, and
OpenAI into a frozen ``PostEventSnapshot``.

Determinism guarantees
----------------------
* Computed fields (prices, ratios, ATR, IV crush) are fully deterministic
  given the same ORATS + EODHD data.
* Annotation fields (LLM sentiment, Benzinga headlines) are persisted to
  Redis on first capture and replayed for subsequent calls with the same
  ``(ticker, earnings_date)`` tuple.
* LLM calls use the default ``temperature=1`` (gpt-5.5 rejects others),
  a fixed model version, and lexicographically sorted inputs. Results are keyed by
  ``engine8:llm:{ticker}:{date}:{input_hash}:{model_version}``.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import math
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.config import FeatureFlags, get_flags

LOG = logging.getLogger(__name__)

_NEUTRAL_SENTIMENT = "MIXED"
_NEUTRAL_CONFIDENCE = 0.5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _fmt_date(d: dt.date) -> str:
    return d.isoformat()


def _compute_atr(bars: List[dict], period: int = 14) -> Optional[float]:
    """ATR from a list of OHLCV dicts (keys: high, low, close or adjusted_close)."""
    if len(bars) < period + 1:
        return None
    trs: list[float] = []
    for i in range(1, len(bars)):
        h = _to_float(bars[i].get("high"))
        lo = _to_float(bars[i].get("low"))
        prev_c = _to_float(bars[i - 1].get("close") or bars[i - 1].get("adjusted_close"))
        if h is None or lo is None or prev_c is None:
            continue
        tr = max(h - lo, abs(h - prev_c), abs(lo - prev_c))
        trs.append(tr)
    if len(trs) < period:
        return None
    return sum(trs[-period:]) / period


def _classify_gap_structure(
    open_price: float,
    high: float,
    low: float,
    close: float,
    pre_close: float,
    atr: Optional[float],
) -> str:
    """HOLD / FADE / STALL based on post-event bar."""
    gap_dir_up = open_price > pre_close
    if gap_dir_up:
        if close >= open_price:
            return "HOLD"
        if atr and (high - low) < 0.5 * atr:
            return "STALL"
        return "FADE"
    else:
        if close <= open_price:
            return "HOLD"
        if atr and (high - low) < 0.5 * atr:
            return "STALL"
        return "FADE"


# ---------------------------------------------------------------------------
# LLM sentiment helpers
# ---------------------------------------------------------------------------

def _build_llm_input(
    ticker: str,
    earnings_date: str,
    headline_texts: List[str],
    metrics: Dict[str, Any],
) -> str:
    """Build deterministic sorted JSON input for the LLM."""
    payload = {
        "earnings_date": earnings_date,
        "headlines": sorted(headline_texts),
        "metrics": dict(sorted(metrics.items())),
        "ticker": ticker,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _input_hash(input_json: str) -> str:
    return hashlib.sha256(input_json.encode("utf-8")).hexdigest()[:16]


def _llm_redis_key(ticker: str, earnings_date: str, ih: str, model: str) -> str:
    return f"engine8:llm:{ticker}:{earnings_date}:{ih}:{model}"


def _call_llm_classify(
    input_json: str,
    model: str,
) -> Optional[dict]:
    """Call OpenAI for event classification (gpt-5.5 only accepts default temperature=1)."""
    try:
        import openai
    except ImportError:
        LOG.warning("openai package not installed; LLM classification unavailable")
        return None

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        return None

    prompt_path = Path(__file__).parent / "prompts" / "engine8_event_classify.txt"
    system_prompt = ""
    if prompt_path.exists():
        system_prompt = prompt_path.read_text(encoding="utf-8").strip()
    if not system_prompt:
        LOG.warning("Missing engine8_event_classify.txt prompt")
        return None

    try:
        client = openai.OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": input_json},
            ],
            temperature=1,
            max_completion_tokens=400,
            timeout=15,
            response_format={"type": "json_object"},
        )
        content = (response.choices[0].message.content or "").strip()
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:])
            if content.rstrip().endswith("```"):
                content = content.rstrip()[:-3]
            content = content.strip()
        return json.loads(content)
    except Exception as e:
        LOG.warning("Engine 8 LLM classify failed: %s", e)
        return None


def _resolve_sentiment(
    *,
    ticker: str,
    earnings_date: str,
    headline_texts: List[str],
    metrics: Dict[str, Any],
    flags: FeatureFlags,
    store: Any,
) -> dict:
    """Resolve sentiment via Redis replay or fresh LLM call.

    Returns dict with keys: sentiment, sentiment_confidence, narrative_tags.
    """
    neutral = {
        "sentiment": _NEUTRAL_SENTIMENT,
        "sentiment_confidence": _NEUTRAL_CONFIDENCE,
        "narrative_tags": [],
    }

    if not flags.ENGINE8_ENABLE_LLM_CLASSIFY:
        return neutral

    model = flags.ENGINE8_LLM_MODEL_VERSION
    input_json = _build_llm_input(ticker, earnings_date, headline_texts, metrics)
    ih = _input_hash(input_json)
    redis_key = _llm_redis_key(ticker, earnings_date, ih, model)

    if store is not None:
        cached = store.get_json(redis_key)
        if cached and isinstance(cached, dict):
            return {
                "sentiment": cached.get("sentiment", _NEUTRAL_SENTIMENT),
                "sentiment_confidence": cached.get("sentiment_confidence", _NEUTRAL_CONFIDENCE),
                "narrative_tags": cached.get("narrative_tags", []),
            }

    result = _call_llm_classify(input_json, model)
    if result is None:
        return neutral

    out = {
        "sentiment": str(result.get("sentiment", _NEUTRAL_SENTIMENT)).upper(),
        "sentiment_confidence": min(1.0, max(0.0, float(result.get("confidence", _NEUTRAL_CONFIDENCE)))),
        "narrative_tags": list(result.get("narrative_tags", result.get("tags", []))),
    }
    if out["sentiment"] not in ("POSITIVE", "NEGATIVE", "MIXED"):
        out["sentiment"] = _NEUTRAL_SENTIMENT

    if store is not None:
        store.set_json(redis_key, out, ttl_s=flags.ENGINE8_LLM_RESULT_TTL_S)

    return out


# ---------------------------------------------------------------------------
# Benzinga headline capture
# ---------------------------------------------------------------------------

def _fetch_benzinga_headlines(
    ticker: str,
    earnings_date: str,
    flags: FeatureFlags,
) -> List[dict]:
    """Fetch and freeze Benzinga headlines for the event date."""
    if not flags.ENABLE_BENZINGA:
        return []
    try:
        from backend.benzinga_client import BenzingaClient
        client = BenzingaClient()
        resp = client.news(tickers=ticker, date=earnings_date, page_size=10)
        headlines = []
        for row in (resp.rows or []):
            headlines.append({
                "id": str(row.get("id", "")),
                "text": str(row.get("title", ""))[:500],
                "timestamp": str(row.get("created", row.get("updated", "")))[:30],
            })
        return headlines
    except Exception as e:
        LOG.warning("Engine 8 Benzinga fetch failed for %s: %s", ticker, e)
        return []


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class PostEventSnapshot:
    ticker: str
    earnings_date: str

    # Deterministic computed fields
    pre_close: Optional[float] = None
    post_open: Optional[float] = None
    post_high: Optional[float] = None
    post_low: Optional[float] = None
    post_close: Optional[float] = None
    actual_move_pct: Optional[float] = None
    expected_move_pct: Optional[float] = None
    move_vs_em: Optional[float] = None
    atr_14: Optional[float] = None
    atr_multiple: Optional[float] = None
    direction: Optional[str] = None          # UP | DOWN
    gap_structure: Optional[str] = None      # HOLD | FADE | STALL
    pre_iv: Optional[float] = None
    post_iv: Optional[float] = None
    iv_crush_pct: Optional[float] = None

    # Annotation fields (persisted on first capture)
    sentiment: str = _NEUTRAL_SENTIMENT
    sentiment_confidence: float = _NEUTRAL_CONFIDENCE
    narrative_tags: List[str] = field(default_factory=list)
    benzinga_headlines: List[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PostEventSnapshot":
        if not isinstance(d, dict):
            return cls(ticker="", earnings_date="")
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in known})


# ---------------------------------------------------------------------------
# Snapshot Redis key
# ---------------------------------------------------------------------------

def _snapshot_redis_key(ticker: str, earnings_date: str) -> str:
    return f"engine8:snapshot:{ticker}:{earnings_date}"


# ---------------------------------------------------------------------------
# Main builder
# ---------------------------------------------------------------------------

def _resolve_pre_post_dates(
    earnings_date: dt.date,
    timing: str,
) -> tuple:
    """Return (pre_date, post_date) adjusted for BMO/AMC timing.

    BMO: gap at open on earnings day  → pre = prior trading day, post = earnings day
    AMC: gap at open next day         → pre = earnings day, post = next trading day
    UNK: default to AMC-style (safe for most US equities)
    """
    if timing == "BMO":
        pre_date = earnings_date - dt.timedelta(days=1)
        while pre_date.weekday() >= 5:
            pre_date -= dt.timedelta(days=1)
        post_date = earnings_date
        if post_date.weekday() >= 5:
            post_date += dt.timedelta(days=(7 - post_date.weekday()))
    else:
        pre_date = earnings_date
        if pre_date.weekday() >= 5:
            pre_date -= dt.timedelta(days=(pre_date.weekday() - 4))
        post_date = earnings_date + dt.timedelta(days=1)
        while post_date.weekday() >= 5:
            post_date += dt.timedelta(days=1)
    return pre_date, post_date


def build_post_event_snapshot(
    *,
    ticker: str,
    earnings_date: dt.date,
    orats_client: Any,
    eodhd_client: Any = None,
    price_service_mod: Any = None,
    store: Any = None,
    flags: Optional[FeatureFlags] = None,
    timing: str = "UNK",
) -> PostEventSnapshot:
    """Build (or replay from Redis) the post-event snapshot for a ticker.

    ``timing`` should be "BMO", "AMC", or "UNK" (from ORATS anncTod).
    If the snapshot is already persisted in Redis, it is replayed.
    Otherwise it is built from live data and persisted.
    """
    if flags is None:
        flags = get_flags()

    ed_str = _fmt_date(earnings_date)
    redis_key = _snapshot_redis_key(ticker, ed_str)

    if store is not None:
        cached = store.get_json(redis_key)
        if cached and isinstance(cached, dict):
            return PostEventSnapshot.from_dict(cached)

    # -- Parallel data fetch --------------------------------------------------
    pre_close: Optional[float] = None
    post_bar: Optional[dict] = None
    bars_for_atr: List[dict] = []
    expected_move_pct: Optional[float] = None
    pre_iv: Optional[float] = None
    post_iv: Optional[float] = None
    benzinga_headlines: List[dict] = []

    pre_date, post_date = _resolve_pre_post_dates(earnings_date, timing)
    lookback_start = earnings_date - dt.timedelta(days=30)

    def _fetch_orats_cores():
        nonlocal expected_move_pct, pre_iv, post_iv
        try:
            fields = "ticker,tradeDate,impErnMv,iv30d"
            resp = orats_client.hist_cores(ticker, _fmt_date(pre_date), fields)
            for row in (resp.rows or []):
                em = _to_float(row.get("impErnMv"))
                if em is not None:
                    expected_move_pct = abs(em) * 100.0 if abs(em) <= 1.0 else abs(em)
                iv = _to_float(row.get("iv30d"))
                if iv is not None:
                    pre_iv = iv
                break
            resp2 = orats_client.hist_cores(ticker, _fmt_date(post_date), fields)
            for row in (resp2.rows or []):
                iv = _to_float(row.get("iv30d"))
                if iv is not None:
                    post_iv = iv
                break
        except Exception as e:
            LOG.warning("Engine 8 ORATS cores fetch failed for %s: %s", ticker, e)

    def _fetch_price_bars():
        nonlocal pre_close, post_bar, bars_for_atr
        try:
            if price_service_mod is not None:
                from backend.price_service import fetch_bars_range
                all_bars = fetch_bars_range(
                    ticker=ticker,
                    start=_fmt_date(lookback_start),
                    end=_fmt_date(post_date),
                    eodhd_client=eodhd_client,
                )
            elif eodhd_client is not None:
                resp = eodhd_client.eod(ticker=f"{ticker}.US", from_date=_fmt_date(lookback_start), to_date=_fmt_date(post_date))
                all_bars = resp.rows or []
            else:
                all_bars = []

            bars_for_atr = all_bars

            pre_bars = [
                b for b in all_bars
                if str(b.get("date", ""))[:10] <= _fmt_date(pre_date)
            ]
            if pre_bars:
                last_pre = sorted(pre_bars, key=lambda b: str(b.get("date", "")))[-1]
                pre_close = _to_float(last_pre.get("close") or last_pre.get("adjusted_close"))

            post_bars = [
                b for b in all_bars
                if str(b.get("date", ""))[:10] >= _fmt_date(post_date)
            ]
            if post_bars:
                post_bar = sorted(post_bars, key=lambda b: str(b.get("date", "")))[0]
        except Exception as e:
            LOG.warning("Engine 8 price fetch failed for %s: %s", ticker, e)

    def _fetch_benzinga():
        nonlocal benzinga_headlines
        benzinga_headlines = _fetch_benzinga_headlines(ticker, ed_str, flags)

    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(_fetch_orats_cores),
            pool.submit(_fetch_price_bars),
            pool.submit(_fetch_benzinga),
        ]
        for f in as_completed(futures):
            f.result()

    # -- Compute deterministic fields -----------------------------------------
    atr_14 = _compute_atr(bars_for_atr, period=14) if bars_for_atr else None

    actual_move_pct: Optional[float] = None
    move_vs_em: Optional[float] = None
    atr_multiple: Optional[float] = None
    direction: Optional[str] = None
    gap_structure: Optional[str] = None
    iv_crush_pct: Optional[float] = None

    post_open = _to_float(post_bar.get("open")) if post_bar else None
    post_high = _to_float(post_bar.get("high")) if post_bar else None
    post_low = _to_float(post_bar.get("low")) if post_bar else None
    post_close_val = _to_float(post_bar.get("close") or (post_bar.get("adjusted_close") if post_bar else None)) if post_bar else None

    if pre_close and pre_close > 0 and post_close_val is not None:
        actual_move_pct = ((post_close_val - pre_close) / pre_close) * 100.0
        direction = "UP" if actual_move_pct > 0 else "DOWN"

        if expected_move_pct and expected_move_pct > 0:
            move_vs_em = abs(actual_move_pct) / expected_move_pct

        if atr_14 and atr_14 > 0:
            atr_multiple = abs(post_close_val - pre_close) / atr_14

    if post_open is not None and post_high is not None and post_low is not None and post_close_val is not None and pre_close is not None:
        gap_structure = _classify_gap_structure(post_open, post_high, post_low, post_close_val, pre_close, atr_14)

    if pre_iv is not None and post_iv is not None and pre_iv > 0:
        iv_crush_pct = ((pre_iv - post_iv) / pre_iv) * 100.0

    # -- Resolve annotations (LLM + Benzinga) ---------------------------------
    headline_texts = [h["text"] for h in benzinga_headlines if h.get("text")]
    metrics_for_llm: Dict[str, Any] = {}
    if actual_move_pct is not None:
        metrics_for_llm["actual_move_pct"] = round(actual_move_pct, 4)
    if move_vs_em is not None:
        metrics_for_llm["move_vs_em"] = round(move_vs_em, 4)
    if iv_crush_pct is not None:
        metrics_for_llm["iv_crush_pct"] = round(iv_crush_pct, 4)

    sentiment_result = _resolve_sentiment(
        ticker=ticker,
        earnings_date=ed_str,
        headline_texts=headline_texts,
        metrics=metrics_for_llm,
        flags=flags,
        store=store,
    )

    snap = PostEventSnapshot(
        ticker=ticker,
        earnings_date=ed_str,
        pre_close=pre_close,
        post_open=post_open,
        post_high=post_high,
        post_low=post_low,
        post_close=post_close_val,
        actual_move_pct=round(actual_move_pct, 4) if actual_move_pct is not None else None,
        expected_move_pct=round(expected_move_pct, 4) if expected_move_pct is not None else None,
        move_vs_em=round(move_vs_em, 4) if move_vs_em is not None else None,
        atr_14=round(atr_14, 4) if atr_14 is not None else None,
        atr_multiple=round(atr_multiple, 4) if atr_multiple is not None else None,
        direction=direction,
        gap_structure=gap_structure,
        pre_iv=pre_iv,
        post_iv=post_iv,
        iv_crush_pct=round(iv_crush_pct, 4) if iv_crush_pct is not None else None,
        sentiment=sentiment_result["sentiment"],
        sentiment_confidence=sentiment_result["sentiment_confidence"],
        narrative_tags=sentiment_result["narrative_tags"],
        benzinga_headlines=benzinga_headlines,
    )

    if store is not None:
        store.set_json(redis_key, snap.to_dict(), ttl_s=flags.ENGINE8_SNAPSHOT_TTL_S)

    return snap
