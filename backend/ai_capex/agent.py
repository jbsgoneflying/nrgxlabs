"""Tier-2 agentic web sourcing for the AI Capex Reality Engine.

This is the first *agentic* LLM use in the codebase. Everything else is
single-shot completion; here we use the OpenAI **Responses API** with the
hosted ``web_search`` tool so the model can pull fragmented public documents
that no data feed gives us — utility interconnection queues (PJM/ERCOT/MISO/
CAISO), data-center permits, and FERC filings — and map them to universe
tickers as source-attributed ``CapexEvidence``.

Guardrails:
- Batch-only. NEVER called on a request path — only by ``scripts/refresh_ai_capex``.
- Gated behind ``AI_CAPEX_ENABLE_WEB_AGENT`` (default OFF) — it costs money + latency.
- Hard cap on calls per run (``AI_CAPEX_MAX_WEB_CALLS``).
- Output is still just *evidence* fed into the deterministic scorer; it cannot
  produce labels or sizing directly.
- Fully defensive: any failure (no key, SDK lacks Responses/web_search, bad
  JSON) yields an empty list, never an exception.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, List, Optional

from backend.ai_capex import models
from backend.ai_capex.models import CapexEvidence

LOG = logging.getLogger("ai_capex.agent")


# Research themes the agent sweeps. Each is one web_search-enabled call that can
# return evidence across multiple tickers, keeping the call count bounded.
_THEMES: List[Dict[str, str]] = [
    {
        "id": "interconnection_queues",
        "focus": "US utility / ISO (PJM, ERCOT, MISO, CAISO, SPP) interconnection "
                 "queue activity and large-load (data-center) interconnection requests, "
                 "approvals, or denials in the last 90 days",
    },
    {
        "id": "datacenter_permits",
        "focus": "newly filed or approved US data-center construction permits, zoning "
                 "approvals/denials, and announced campus buildouts (hyperscaler or "
                 "colo) in the last 90 days, including any delays",
    },
    {
        "id": "power_ppas",
        "focus": "data-center power purchase agreements (PPAs), nuclear/gas supply deals, "
                 "transformer/switchgear lead-time commentary, and grid-equipment shortages "
                 "tied to AI data-center demand in the last 90 days",
    },
]


def _get_openai_client():
    try:
        import openai
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            return None
        return openai.OpenAI(api_key=api_key)
    except Exception as exc:  # pragma: no cover - defensive
        LOG.warning("ai_capex.agent: OpenAI client unavailable: %s", exc)
        return None


def _responses_supported(client: Any) -> bool:
    return client is not None and hasattr(client, "responses")


def _output_text(response: Any) -> str:
    """Pull the concatenated text out of a Responses API result, defensively."""
    txt = getattr(response, "output_text", None)
    if isinstance(txt, str) and txt.strip():
        return txt
    # Fallback: walk the structured output.
    chunks: List[str] = []
    for item in (getattr(response, "output", None) or []):
        for content in (getattr(item, "content", None) or []):
            t = getattr(content, "text", None)
            if isinstance(t, str):
                chunks.append(t)
    return "\n".join(chunks)


def _parse_json(text: str) -> Optional[dict]:
    text = (text or "").strip()
    if text.startswith("```"):
        lines = text.split("\n")
        text = "\n".join(lines[1:])
        if text.rstrip().endswith("```"):
            text = text.rstrip()[:-3]
        text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    for i in range(start, len(text)):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _theme_prompt(theme: Dict[str, str], tickers: List[str]) -> str:
    universe = ", ".join(tickers)
    return (
        "You are a data-center / power-grid research analyst. Use web search to find "
        f"RECENT, SPECIFIC, source-backed facts about: {theme['focus']}.\n\n"
        "Map each finding to the single most-affected publicly traded ticker from this "
        f"universe (skip findings that don't clearly map to one): {universe}.\n\n"
        'Return ONLY a JSON object: {"evidence": [ {\n'
        '  "ticker": "<one ticker from the universe>",\n'
        '  "claim": "<one concise sentence with the specific fact>",\n'
        '  "signal_type": one of ["capex_up","supply_constraint","delay","demand_pull","second_order_link"],\n'
        '  "magnitude": 0.0-1.0, "timing": "near"|"mid"|"far", "polarity": 1|0|-1,\n'
        '  "confidence": 0.0-1.0, "source_url": "<the source URL you used>", "date": "YYYY-MM-DD"\n'
        "} ] }\n\n"
        "Rules: prefer primary sources (ISO queue dashboards, county permit portals, FERC, "
        "company filings). Only include items with a real source_url. 4-10 items max. "
        "No speculation, no commentary outside the JSON."
    )


def _evidence_from_rows(rows: List[Any], valid_tickers: set, tcat: Dict[str, str]) -> List[CapexEvidence]:
    out: List[CapexEvidence] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        ticker = str(r.get("ticker") or "").upper().strip()
        claim = str(r.get("claim") or "").strip()
        if ticker not in valid_tickers or not claim:
            continue
        out.append(CapexEvidence(
            ticker=ticker,
            category=tcat.get(ticker, ""),
            source_type=models.SOURCE_WEB,
            signal_type=str(r.get("signal_type") or models.SIG_DEMAND_PULL),
            claim=claim[:240],
            date=str(r.get("date") or "")[:10],
            source_url=str(r.get("source_url") or ""),
            source_title=str(r.get("source_title") or "web source"),
            magnitude=r.get("magnitude", 0.5),
            timing=str(r.get("timing") or models.TIMING_MID),
            polarity=r.get("polarity", 1),
            confidence=r.get("confidence", 0.55),
        ))
    return out


def run_web_agent(
    tickers: Optional[List[str]] = None,
    *,
    model: str = "gpt-5.5",
    max_calls: int = 12,
) -> List[CapexEvidence]:
    """Sweep the web-research themes and return source-attributed evidence.

    Returns ``[]`` immediately if the flag is off, the SDK lacks the Responses
    API, or no API key is present. Bounded by ``max_calls`` (one call/theme).
    """
    try:
        from backend.config import get_flags
        flags = get_flags()
    except Exception:
        return []

    if not getattr(flags, "AI_CAPEX_ENABLE_WEB_AGENT", False):
        LOG.info("ai_capex.agent: web agent disabled (AI_CAPEX_ENABLE_WEB_AGENT=0)")
        return []

    client = _get_openai_client()
    if not _responses_supported(client):
        LOG.warning("ai_capex.agent: Responses API / web_search not available; skipping Tier-2")
        return []

    tcat = models.ticker_to_category_map()
    valid = set(tickers) if tickers else set(tcat.keys())
    valid = {t for t in valid if t in tcat}
    if not valid:
        return []
    universe = sorted(valid)

    budget = max(0, int(max_calls))
    collected: List[CapexEvidence] = []
    for theme in _THEMES:
        if budget <= 0:
            break
        budget -= 1
        try:
            response = client.responses.create(
                model=model,
                tools=[{"type": "web_search"}],
                input=_theme_prompt(theme, universe),
                timeout=120,
            )
            parsed = _parse_json(_output_text(response))
            rows = (parsed or {}).get("evidence") if isinstance(parsed, dict) else None
            if isinstance(rows, list):
                got = _evidence_from_rows(rows, valid, tcat)
                LOG.info("ai_capex.agent: theme=%s -> %d evidence item(s)", theme["id"], len(got))
                collected.extend(got)
        except Exception as exc:  # pragma: no cover - network/SDK dependent
            LOG.warning("ai_capex.agent: theme=%s failed: %s", theme["id"], exc)
            continue

    return collected


def group_by_ticker(evidence: List[CapexEvidence]) -> Dict[str, List[CapexEvidence]]:
    """Bucket a flat evidence list by ticker (helper for the refresh job)."""
    out: Dict[str, List[CapexEvidence]] = {}
    for e in evidence:
        out.setdefault(e.ticker, []).append(e)
    return out
