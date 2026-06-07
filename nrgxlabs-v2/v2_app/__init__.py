"""NRGX Labs v2 — Foundation Brain + Agentic Reasoning trading platform.

This package is intentionally separate from the v1 ``backend/`` codebase. It
runs as its own FastAPI service (port 8001) so v1 trading flows are never
blocked by v2 development.

Layers:
    foundation/   Layer 1 - learned regime / analogue / path / conformal models
    engines/      Layer 2 - E1/E2/E14/E15/MI v2 cores built on foundation
    agents/       Layer 3 - Researcher / Quant / Devil / Risk / Synthesizer
    eval/         Layer 4 - counterfactual logger, conformal coverage tracker

See ``nrgxlabs-v2/README.md`` for the architecture overview.
"""

__version__ = "2.0.0-alpha.0"
