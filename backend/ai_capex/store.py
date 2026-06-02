"""Redis persistence for the AI Capex Reality Engine (``ai_capex:*`` keys).

Defensive throughout: a missing/broken Redis store degrades to in-memory only
(the router falls back to a fresh build), never raises.

Keys:
- ``ai_capex:scan:latest``        — full scan payload served by the API.
- ``ai_capex:evidence:{TICKER}``  — per-ticker evidence list (audit trail).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from backend.ai_capex.models import CapexEvidence

_SCAN_KEY = "ai_capex:scan:latest"
_EVIDENCE_PREFIX = "ai_capex:evidence:"
_LAST_RUN_KEY = "ai_capex:last_run"


def _store():
    try:
        from backend.redis_store import get_store_optional
        return get_store_optional()
    except Exception:
        return None


def scan_key() -> str:
    return _SCAN_KEY


def evidence_key(ticker: str) -> str:
    return _EVIDENCE_PREFIX + str(ticker or "").upper().strip()


def get_scan(*, store: Any = None) -> Optional[Dict[str, Any]]:
    store = store or _store()
    if store is None:
        return None
    try:
        return store.get_json(_SCAN_KEY)
    except Exception:
        return None


def set_scan(payload: Dict[str, Any], *, ttl_s: int, store: Any = None) -> bool:
    store = store or _store()
    if store is None:
        return False
    try:
        return bool(store.set_json(_SCAN_KEY, payload, ttl_s=int(ttl_s)))
    except Exception:
        return False


def set_evidence(ticker: str, evidence: List[CapexEvidence], *, ttl_s: int, store: Any = None) -> bool:
    store = store or _store()
    if store is None:
        return False
    try:
        rows = [e.to_dict() for e in evidence]
        return bool(store.set_json(evidence_key(ticker), rows, ttl_s=int(ttl_s)))
    except Exception:
        return False


def set_last_run(record: Dict[str, Any], *, ttl_s: int = 7 * 86400, store: Any = None) -> bool:
    """Persist a small status record for the last refresh attempt (observability).

    Lets operators read run health (ok / error / counts / timing) over the API
    without shell access to the droplet — important since the heavy refresh runs
    detached and its stdout goes nowhere readable.
    """
    store = store or _store()
    if store is None:
        return False
    try:
        return bool(store.set_json(_LAST_RUN_KEY, record, ttl_s=int(ttl_s)))
    except Exception:
        return False


def get_last_run(*, store: Any = None) -> Optional[Dict[str, Any]]:
    store = store or _store()
    if store is None:
        return None
    try:
        return store.get_json(_LAST_RUN_KEY)
    except Exception:
        return None


def get_evidence(ticker: str, *, store: Any = None) -> List[CapexEvidence]:
    store = store or _store()
    if store is None:
        return []
    try:
        rows = store.get_json(evidence_key(ticker))
        if not isinstance(rows, list):
            return []
        return [CapexEvidence.from_dict(r) for r in rows if isinstance(r, dict)]
    except Exception:
        return []
