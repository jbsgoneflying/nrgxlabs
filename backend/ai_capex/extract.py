"""LLM evidence extractor for the AI Capex Reality Engine.

Turns the raw text bundle (transcripts + news) for one ticker into a list of
structured ``CapexEvidence`` records via a single, fallback-safe
``chat.completions.create`` call (JSON-object response, same pattern as
``front_layer_llm``). The LLM ONLY classifies/structures text into evidence —
it does not produce scores, labels, or trades. Those are deterministic
(``score.py``), so the platform's "LLM never drives sizing" guardrail holds.

If the LLM is unavailable (no key / package / rate-limited / bad JSON) the
extractor falls back to a deterministic keyword pass so the engine still
produces an evidence trail (lower-confidence) rather than nothing.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Dict, List, Optional

from backend.ai_capex import models
from backend.ai_capex.models import CapexEvidence

LOG = logging.getLogger("ai_capex.extract")

_MAX_PAYLOAD_CHARS = 18000


# ---------------------------------------------------------------------------
# Rate limiter (own budget, separate from front-layer)
# ---------------------------------------------------------------------------


class _RateLimiter:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ts: List[float] = []

    def _max(self) -> int:
        try:
            from backend.config import get_flags
            return int(getattr(get_flags(), "AI_CAPEX_LLM_MAX_CALLS_PER_MINUTE", 20))
        except Exception:
            return 20

    def acquire(self, *, block: bool = False, timeout: float = 240.0) -> bool:
        """Reserve one call slot.

        With ``block=True`` we WAIT for budget rather than giving up. A slot
        always frees within 60s as older reservations age out of the window, so
        a batch *paces* at the per-minute cap instead of dumping the overflow
        into the keyword fallback — which is what silently degraded whole scans
        (only ~20 of 72 tickers got real LLM extraction; the rest fell back).
        """
        deadline = time.time() + max(0.0, timeout)
        while True:
            with self._lock:
                now = time.time()
                self._ts = [t for t in self._ts if now - t < 60.0]
                if len(self._ts) < self._max():
                    self._ts.append(now)
                    return True
                sleep_for = max(0.05, 60.0 - (now - self._ts[0]))
            if not block or time.time() + sleep_for > deadline:
                return False
            time.sleep(min(sleep_for, 5.0))


_rate_limiter = _RateLimiter()


def _get_openai_client():
    try:
        import openai
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None
        return openai.OpenAI(api_key=api_key)
    except ImportError:
        LOG.warning("openai package not installed; AI Capex extractor falls back to keywords")
        return None
    except Exception as exc:  # pragma: no cover - defensive
        LOG.warning("Failed to create OpenAI client: %s", exc)
        return None


def _parse_llm_json(content: str) -> Optional[dict]:
    """Robust JSON parse (handles fences + GPT-5.5 preamble)."""
    raw = content
    content = (content or "").strip()
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:])
        if content.rstrip().endswith("```"):
            content = content.rstrip()[:-3]
        content = content.strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    start = content.find("{")
    if start == -1:
        LOG.debug("extractor: no JSON object in LLM output: %s", raw[:200])
        return None
    depth = 0
    for i in range(start, len(content)):
        if content[i] == "{":
            depth += 1
        elif content[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(content[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------

_SYSTEM_PROMPT = """You are a senior equity analyst on an institutional trading desk specializing in the AI-infrastructure capex chain (semiconductors, memory, networking, cloud/hyperscalers, data-center REITs, power generation, utilities, electrical equipment, transformers, cooling, construction, industrial automation, cybersecurity, and AI software).

You will receive recent earnings-call transcript excerpts and news headlines for ONE ticker. Extract DISCRETE, EVIDENCE-GRADE observations about AI capital expenditure reality. You are hunting for the difference between real, funded, near-term spend versus vague AI marketing language.

Return ONLY a JSON object: {"evidence": [ ... ]}. Each evidence item:
{
  "claim": "<one concise sentence paraphrasing the specific observation>",
  "signal_type": one of [
    "capex_up"            (concrete capex increase / raised guidance / new build),
    "capex_down"          (cut / reduced / paused capex),
    "supply_constraint"   (shortage, sold out, lead times extending, capacity tight),
    "delay"               (slippage, pushed out, permitting/power/interconnect delay),
    "demand_pull"         (booked backlog, RPO growth, signed PPAs, new contracts),
    "hype_language"       (AI buzzwords with NO numbers, funding, or timeline behind them),
    "second_order_link"   (explicitly names a supplier/beneficiary downstream)
  ],
  "magnitude": 0.0-1.0   (how material/large the claim is),
  "timing": "near" | "mid" | "far"   (near = this/next quarter happening now; mid = 2-4 quarters; far = 12m+/aspirational),
  "polarity": 1 | 0 | -1   (1 = bullish for AI capex reality, -1 = bearish, 0 = neutral),
  "confidence": 0.0-1.0   (your confidence this is a real, substantiated claim vs noise)
}

Rules:
- Be skeptical. If a company only uses AI buzzwords without dollars/units/dates, classify it "hype_language" with low magnitude.
- Quantified, funded, near-term spend = high magnitude + high confidence.
- 6-14 items max. Prefer the highest-signal observations. No duplicates.
- Output strictly valid JSON. No commentary."""


def _build_user_payload(ticker: str, bundle: Dict[str, Any]) -> str:
    parts: List[str] = [f"TICKER: {ticker}", ""]
    transcripts = bundle.get("transcripts") or []
    if transcripts:
        parts.append("=== TRANSCRIPT EXCERPTS (newest first) ===")
        for t in transcripts[:2]:
            parts.append(f"[{t.get('date','')}] {t.get('title','')}")
            parts.append(str(t.get("text") or "")[:8000])
            parts.append("")
    news = bundle.get("news") or []
    if news:
        parts.append("=== RECENT NEWS HEADLINES ===")
        for n in news[:14]:
            line = f"[{n.get('date','')}] {n.get('title','')}"
            teaser = str(n.get("text") or "").strip()
            if teaser:
                line += f" — {teaser[:200]}"
            parts.append(line)
    payload = "\n".join(parts)
    return payload[:_MAX_PAYLOAD_CHARS]


# ---------------------------------------------------------------------------
# Deterministic keyword fallback
# ---------------------------------------------------------------------------

_FALLBACK_RULES = [
    (models.SIG_SUPPLY_CONSTRAINT, 1, ["sold out", "supply constrain", "lead time", "shortage", "capacity tight", "backlog", "allocation"]),
    (models.SIG_CAPEX_UP, 1, ["raising capex", "increased capex", "capex guidance", "capital expenditure", "expand capacity", "new data center", "building"]),
    (models.SIG_DELAY, -1, ["delay", "pushed out", "slippage", "interconnection queue", "permitting", "power constraint", "behind schedule"]),
    (models.SIG_CAPEX_DOWN, -1, ["cut capex", "reduce capex", "lower capex", "paused", "scaling back"]),
    (models.SIG_DEMAND_PULL, 1, ["signed", "power purchase agreement", "ppa", "contract", "rpo", "remaining performance obligation", "bookings"]),
]


def _fallback_extract(ticker: str, category: str, bundle: Dict[str, Any]) -> List[CapexEvidence]:
    """Keyword pass used when the LLM is unavailable. Low confidence."""
    out: List[CapexEvidence] = []
    hype_kw = models.hype_keywords()
    blobs: List[Dict[str, str]] = []
    for t in (bundle.get("transcripts") or [])[:2]:
        blobs.append({"date": t.get("date", ""), "title": t.get("title", ""), "text": (t.get("text") or "")[:6000], "url": ""})
    for n in (bundle.get("news") or [])[:14]:
        blobs.append({"date": n.get("date", ""), "title": n.get("title", ""), "text": (n.get("text") or ""), "url": n.get("url", "")})

    for b in blobs:
        hay = (b["title"] + " " + b["text"]).lower()
        if not hay.strip():
            continue
        matched = False
        for signal_type, polarity, kws in _FALLBACK_RULES:
            if any(k in hay for k in kws):
                out.append(CapexEvidence(
                    ticker=ticker, category=category,
                    source_type=models.SOURCE_TRANSCRIPT if "earnings call" in b["title"].lower() else models.SOURCE_NEWS,
                    signal_type=signal_type,
                    claim=(b["title"] or hay[:100])[:160],
                    date=b["date"], source_url=b["url"], source_title=b["title"],
                    magnitude=0.45, timing=models.TIMING_MID, polarity=polarity, confidence=0.35,
                ))
                matched = True
                break
        if not matched and any(k in hay for k in hype_kw):
            out.append(CapexEvidence(
                ticker=ticker, category=category, source_type=models.SOURCE_NEWS,
                signal_type=models.SIG_HYPE, claim=(b["title"] or "AI language without specifics")[:160],
                date=b["date"], source_url=b["url"], source_title=b["title"],
                magnitude=0.25, timing=models.TIMING_FAR, polarity=0, confidence=0.3,
            ))
    return out[:14]


# ---------------------------------------------------------------------------
# Public extractor
# ---------------------------------------------------------------------------


def extract_evidence(
    ticker: str,
    category: str,
    bundle: Dict[str, Any],
    *,
    model: str = "gpt-5.5",
) -> List[CapexEvidence]:
    """Extract ``CapexEvidence`` for one ticker from its text bundle.

    Tries the LLM first; on any failure falls back to the keyword pass. Always
    returns a list (possibly empty), never raises.
    """
    ticker = str(ticker or "").upper().strip()
    transcripts = bundle.get("transcripts") or []
    news = bundle.get("news") or []
    if not transcripts and not news:
        return []

    client = _get_openai_client()
    if client is None:
        return _fallback_extract(ticker, category, bundle)
    # Wait for rate budget instead of falling back just because we're pacing —
    # a fallback here means shallow, low-confidence evidence for this ticker.
    if not _rate_limiter.acquire(block=True):
        LOG.warning("extractor: no LLM budget for %s after waiting; using fallback", ticker)
        return _fallback_extract(ticker, category, bundle)

    try:
        payload = _build_user_payload(ticker, bundle)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": payload},
            ],
            temperature=1,
            max_completion_tokens=2500,
            timeout=45,
            response_format={"type": "json_object"},
        )
        content = (response.choices[0].message.content or "").strip()
        parsed = _parse_llm_json(content)
        if not parsed or not isinstance(parsed.get("evidence"), list):
            LOG.debug("extractor: LLM JSON missing 'evidence' for %s; using fallback", ticker)
            return _fallback_extract(ticker, category, bundle)
        return _evidence_from_llm(ticker, category, bundle, parsed["evidence"])
    except Exception as exc:  # pragma: no cover - defensive
        LOG.warning("extractor LLM failed for %s: %s", ticker, exc)
        return _fallback_extract(ticker, category, bundle)


def _evidence_from_llm(
    ticker: str,
    category: str,
    bundle: Dict[str, Any],
    rows: List[Any],
) -> List[CapexEvidence]:
    """Map LLM JSON rows -> CapexEvidence, attaching best-guess source attribution."""
    # A representative date/url to attach (the most recent source we have).
    src_date = ""
    src_title = ""
    src_url = ""
    src_type = models.SOURCE_NEWS
    if bundle.get("transcripts"):
        t0 = bundle["transcripts"][0]
        src_date, src_title, src_type = t0.get("date", ""), t0.get("title", ""), models.SOURCE_TRANSCRIPT
    elif bundle.get("news"):
        n0 = bundle["news"][0]
        src_date, src_title, src_url = n0.get("date", ""), n0.get("title", ""), n0.get("url", "")

    out: List[CapexEvidence] = []
    for r in rows[:16]:
        if not isinstance(r, dict):
            continue
        claim = str(r.get("claim") or "").strip()
        if not claim:
            continue
        out.append(CapexEvidence(
            ticker=ticker,
            category=category,
            source_type=src_type,
            signal_type=str(r.get("signal_type") or models.SIG_DEMAND_PULL),
            claim=claim[:240],
            date=src_date,
            source_url=src_url,
            source_title=src_title,
            magnitude=r.get("magnitude", 0.5),
            timing=str(r.get("timing") or models.TIMING_MID),
            polarity=r.get("polarity", 1),
            confidence=r.get("confidence", 0.6),
        ))
    return out
