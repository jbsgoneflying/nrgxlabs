"""Public health + version endpoints for v2.

These are intentionally NOT behind the invite gate so deploy verification
and external uptime monitors work without credentials.
"""

from __future__ import annotations

import time

from fastapi import APIRouter

from .. import __version__
from ..config import get_config

router = APIRouter()


@router.get("/api/v2/health")
def health() -> dict:
    return {
        "ok": True,
        "service": "nrgxlabs-v2",
        "version": __version__,
        "ts": int(time.time()),
    }


@router.get("/api/v2/version")
def version() -> dict:
    cfg = get_config()
    return {
        "version": __version__,
        "service": cfg.service_name,
        "foundation": {
            "regime_encoder": cfg.enable_regime_encoder,
            "contrastive_analogues": cfg.enable_contrastive_analogues,
            "conformal_calibration": cfg.enable_conformal_calibration,
            "path_generator": cfg.enable_path_generator,
            "learned_ranker": cfg.enable_learned_ranker,
            "agent_committee": cfg.enable_agent_committee,
        },
    }
