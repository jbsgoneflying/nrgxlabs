"""Redis persistence for Engine 18 (``e18:*`` keys).

Defensive throughout: a missing/broken Redis store degrades to in-memory only
(the router falls back to a fresh build), never raises.

Keys:
- ``e18:scan:latest``         — full scan payload served by the API.
- ``e18:evidence:{TICKER}``   — per-ticker evidence (report + transcript + grades).
- ``e18:grades:trailing``     — rolling list of quality scores (quintile basis).
- ``e18:grades:log``          — append log of (llm, heuristic) score pairs for
                                 ongoing grader-vs-grader validation.
- ``e18:last_run``            — small status record for observability.
- ``e18:validation:latest``   — monthly continuous-validation result.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

_SCAN_KEY = "e18:scan:latest"
_EVIDENCE_PREFIX = "e18:evidence:"
_TRAILING_KEY = "e18:grades:trailing"
_GRADE_LOG_KEY = "e18:grades:log"
_LAST_RUN_KEY = "e18:last_run"
_VALIDATION_KEY = "e18:validation:latest"

_GRADE_LOG_MAX = 2000


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


def get_evidence(ticker: str, *, store: Any = None) -> Optional[Dict[str, Any]]:
    store = store or _store()
    if store is None:
        return None
    try:
        row = store.get_json(evidence_key(ticker))
        return row if isinstance(row, dict) else None
    except Exception:
        return None


def set_evidence(ticker: str, evidence: Dict[str, Any], *, ttl_s: int, store: Any = None) -> bool:
    store = store or _store()
    if store is None:
        return False
    try:
        return bool(store.set_json(evidence_key(ticker), evidence, ttl_s=int(ttl_s)))
    except Exception:
        return False


def get_trailing_grades(*, store: Any = None) -> List[float]:
    store = store or _store()
    if store is None:
        return []
    try:
        rows = store.get_json(_TRAILING_KEY)
        if not isinstance(rows, list):
            return []
        return [float(x) for x in rows if isinstance(x, (int, float))]
    except Exception:
        return []


def append_trailing_grades(
    scores: List[float], *, max_len: int, ttl_s: int, store: Any = None
) -> bool:
    """Append fresh quality scores to the rolling window (newest last)."""
    store = store or _store()
    if store is None:
        return False
    try:
        existing = get_trailing_grades(store=store)
        merged = (existing + [float(s) for s in scores])[-int(max_len):]
        return bool(store.set_json(_TRAILING_KEY, merged, ttl_s=int(ttl_s)))
    except Exception:
        return False


def append_grade_log(entries: List[Dict[str, Any]], *, ttl_s: int, store: Any = None) -> bool:
    """Append (llm, heuristic) score pairs — the grader-vs-grader audit trail."""
    store = store or _store()
    if store is None:
        return False
    try:
        existing = store.get_json(_GRADE_LOG_KEY)
        rows = existing if isinstance(existing, list) else []
        rows = (rows + list(entries))[-_GRADE_LOG_MAX:]
        return bool(store.set_json(_GRADE_LOG_KEY, rows, ttl_s=int(ttl_s)))
    except Exception:
        return False


def get_grade_log(*, store: Any = None) -> List[Dict[str, Any]]:
    store = store or _store()
    if store is None:
        return []
    try:
        rows = store.get_json(_GRADE_LOG_KEY)
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def set_last_run(record: Dict[str, Any], *, ttl_s: int = 7 * 86400, store: Any = None) -> bool:
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


def set_validation(record: Dict[str, Any], *, ttl_s: int = 45 * 86400, store: Any = None) -> bool:
    store = store or _store()
    if store is None:
        return False
    try:
        return bool(store.set_json(_VALIDATION_KEY, record, ttl_s=int(ttl_s)))
    except Exception:
        return False


def get_validation(*, store: Any = None) -> Optional[Dict[str, Any]]:
    store = store or _store()
    if store is None:
        return None
    try:
        return store.get_json(_VALIDATION_KEY)
    except Exception:
        return None
