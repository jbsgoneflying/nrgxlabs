"""AI Capex Reality Engine (Engine 17).

An LLM-evidence pipeline that tracks where AI infrastructure capex is becoming
*real*, where it is *delayed*, who benefits *before consensus updates*, and who
is priced as a winner without evidence. The information is fragmented across
transcripts, news, fundamentals, and document-heavy public sources (utility
interconnection queues, data-center permits) — too much for a human to track
across hundreds of tickers, but tractable for an LLM pipeline.

Architecture (mirrors the platform's "LLM never drives sizing" guardrail):

- ``models``    — ``CapexEvidence`` / ``TickerVerdict`` + the universe taxonomy.
- ``ingest``    — Tier-1 evidence text from transcripts / news / fundamentals
                  (data we already pay for).
- ``agent``     — Tier-2 agentic web sourcing (OpenAI Responses + web_search)
                  for fragmented public docs. Gated OFF by default, batch-only.
- ``extract``   — single-shot LLM that turns raw text into ``CapexEvidence``.
- ``score``     — *deterministic* Reality Score + Consensus Gap + the six labels.
- ``trades``    — directional / basket / ORATS option structures from the labels.
- ``store``     — Redis persistence (``ai_capex:*`` keys).

The LLM only *extracts and sources* evidence; every label and any Desk Brain
sizing is reproducible from the deterministic scorer over the evidence table.
"""

__all__ = [
    "models",
    "ingest",
    "agent",
    "extract",
    "score",
    "trades",
    "store",
]
