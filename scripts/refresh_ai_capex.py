#!/usr/bin/env python3
"""AI Capex Reality Engine (Engine 17) — nightly refresh (cron wrapper).

Runs the full pipeline once and writes the scan + per-ticker evidence to Redis:

  Tier-1 ingest (transcripts / news / fundamentals + market context)
    -> LLM evidence extractor
    -> Tier-2 web agent (ISO queues / permits / FERC) IF AI_CAPEX_ENABLE_WEB_AGENT
    -> deterministic score (Reality / Consensus Gap / 6 labels)
    -> trade ideas + baskets
    -> Redis (ai_capex:scan:latest + ai_capex:evidence:{ticker})

Usage:
    python scripts/refresh_ai_capex.py [--no-web] [--tickers AAA,BBB]

The engine is gated by ENABLE_AI_CAPEX; this job still runs the pipeline (so
evidence accrues) but logs a warning when the API is disabled. The Tier-2 web
agent only fires when AI_CAPEX_ENABLE_WEB_AGENT=1 (default OFF) and is hard-
capped by AI_CAPEX_MAX_WEB_CALLS.
"""
from __future__ import annotations

import logging
import os
import sys

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)

LOG = logging.getLogger("refresh_ai_capex")


def main() -> int:
    use_web = "--no-web" not in sys.argv
    tickers = None
    for arg in sys.argv:
        if arg.startswith("--tickers="):
            tickers = [t.strip().upper() for t in arg.split("=", 1)[1].split(",") if t.strip()]

    from backend.ai_capex import pipeline
    from backend.config import get_flags

    flags = get_flags()
    if not getattr(flags, "ENABLE_AI_CAPEX", False):
        LOG.warning("ENABLE_AI_CAPEX is OFF — building the scan anyway so evidence accrues, "
                    "but the /api/ai-capex route will 404 until the flag is enabled.")

    orats_client = None
    try:
        from backend.deps import get_client_optional
        orats_client = get_client_optional()
    except Exception:
        orats_client = None

    LOG.info("AI Capex refresh starting (web_agent=%s, tickers=%s)",
             use_web and getattr(flags, "AI_CAPEX_ENABLE_WEB_AGENT", False),
             "all" if not tickers else ",".join(tickers))

    payload = pipeline.build_scan(
        flags=flags,
        tickers=tickers,
        with_web_agent=use_web,
        orats_client=orats_client,
        persist=True,
    )

    summary = payload.get("summary", {})
    LOG.info("AI Capex refresh done: %d names scored, %d actionable, %d evidence items (%d web).",
             summary.get("total", 0), summary.get("actionable", 0),
             payload.get("evidenceTotal", 0), payload.get("webEvidence", 0))
    LOG.info("Labels: %s", summary.get("byLabel", {}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
