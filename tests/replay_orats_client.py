from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple


@dataclass(frozen=True)
class ReplayResp:
    rows: list[dict]
    raw: Any


def _stable_params(params: Dict[str, Any]) -> List[List[str]]:
    return [[str(k), str(v)] for (k, v) in sorted((k, str(v)) for k, v in params.items())]


class ReplayOratsClient:
    """
    Offline ORATS client for deterministic tests.

    Loads a recorded tape from `scripts/generate_golden_payloads.py` and replays responses by
    exact (path, sorted params) lookup.
    """

    def __init__(self, tape: dict) -> None:
        calls = tape.get("calls") or []
        self._by_key: Dict[Tuple[str, Tuple[Tuple[str, str], ...]], List[dict]] = {}
        for c in calls:
            path = str(c.get("path") or "")
            params = tuple((str(k), str(v)) for k, v in (c.get("params") or []))
            self._by_key[(path, params)] = c.get("rows") or []

    def get(self, path: str, params: Dict[str, Any]) -> ReplayResp:
        # Handle range queries for bulk fetch (fromDate/toDate parameters)
        # These aggregate matching single-date records from the tape
        if path in ("/hist/dailies", "/hist/cores") and "fromDate" in params and "toDate" in params:
            return self._handle_range_query(path, params)
        
        key = (str(path), tuple((k, v) for k, v in _stable_params(params)))
        rows = self._by_key.get(key)
        if rows is None:
            # Unknown call -> empty (mirrors 404->empty behavior used for probing)
            rows = []
        return ReplayResp(rows=rows, raw=rows)
    
    def _handle_range_query(self, path: str, params: Dict[str, Any]) -> ReplayResp:
        """
        Handle range queries by aggregating records from the tape.
        
        This supports the bulk fetch optimization in earnings_logic.py while
        maintaining compatibility with existing recorded tapes.
        
        Strategy:
        1. First, look for single-date entries within the requested range
        2. Also look for range entries (fromDate/toDate) that overlap with the request
        """
        ticker = str(params.get("ticker", ""))
        from_date = str(params.get("fromDate", ""))
        to_date = str(params.get("toDate", ""))
        
        if not ticker or not from_date or not to_date:
            return ReplayResp(rows=[], raw=[])
        
        # Collect all matching records from the tape
        aggregated_rows: List[dict] = []
        seen_dates: set = set()
        
        for (tape_path, tape_params), rows in self._by_key.items():
            if tape_path != path:
                continue
            
            # Extract params dict from tuple of tuples
            params_dict = {k: v for k, v in tape_params}
            tape_ticker = params_dict.get("ticker", "")
            
            if tape_ticker != ticker:
                continue
            
            # Check for single-date entries (tradeDate param)
            tape_date = params_dict.get("tradeDate", "")
            if tape_date:
                if from_date <= tape_date <= to_date:
                    for row in rows:
                        td = row.get("tradeDate", "")
                        if td and td not in seen_dates:
                            aggregated_rows.append(row)
                            seen_dates.add(td)
                continue
            
            # Check for range entries (fromDate/toDate params)
            tape_from = params_dict.get("fromDate", "")
            tape_to = params_dict.get("toDate", "")
            if tape_from and tape_to:
                # Check for overlap: not (tape_to < from_date or tape_from > to_date)
                if not (tape_to < from_date or tape_from > to_date):
                    # Filter rows within the requested range
                    for row in rows:
                        td = row.get("tradeDate", "")
                        if td and from_date <= td <= to_date and td not in seen_dates:
                            aggregated_rows.append(row)
                            seen_dates.add(td)
        
        return ReplayResp(rows=aggregated_rows, raw=aggregated_rows)

    # Convenience wrappers used by the app
    def hist_earnings(self, ticker: str) -> ReplayResp:
        return self.get("/hist/earnings", {"ticker": ticker})

    def cores(self, *, ticker: str, fields: str) -> ReplayResp:
        return self.get("/cores", {"ticker": ticker, "fields": fields})

    def hist_cores(self, ticker: str, trade_date: str, fields: str) -> ReplayResp:
        return self.get("/hist/cores", {"ticker": ticker, "tradeDate": trade_date, "fields": fields})

    def hist_dailies(self, ticker: str, trade_date: str, fields: str) -> ReplayResp:
        # Handle comma-separated date range (used by _fetch_dailies_range for bulk fetch)
        if "," in trade_date:
            from_date, to_date = trade_date.split(",")
            return self.get("/hist/dailies", {"ticker": ticker, "fromDate": from_date, "toDate": to_date, "fields": fields})
        return self.get("/hist/dailies", {"ticker": ticker, "tradeDate": trade_date, "fields": fields})

    def hist_strikes(self, *, ticker: str, trade_date: str, fields: str, dte: str | None = None, delta: str | None = None) -> ReplayResp:
        params: Dict[str, Any] = {"ticker": ticker, "tradeDate": str(trade_date)[:10], "fields": fields}
        if dte:
            params["dte"] = dte
        if delta:
            params["delta"] = delta
        return self.get("/hist/strikes", params)

    def hist_monies_implied(self, *, ticker: str, trade_date: str, fields: str, dte: str | None = None) -> ReplayResp:
        params: Dict[str, Any] = {"ticker": ticker, "tradeDate": trade_date, "fields": fields}
        if dte:
            params["dte"] = dte
        return self.get("/hist/monies/implied", params)

    # Skew mapping used by `backend/skew_overlay.py`
    def get_skew_by_delta(
        self,
        *,
        ticker: str,
        trade_date: str,
        dte_target: int,
        deltas: list[int] | None = None,
        rights: list[str] | None = None,
    ) -> dict:
        use_deltas = deltas or [10, 25]
        use_rights = rights or ["C", "P"]

        lo = max(1, int(dte_target) - 2)
        hi = int(dte_target) + 7
        fields = "ticker,tradeDate,expirDate,dte,stockPrice,vol10,vol25,vol50,vol75,vol90"
        resp = self.hist_monies_implied(
            ticker=ticker,
            trade_date=str(trade_date)[:10],
            fields=fields,
            dte=f"{lo},{hi}",
        )
        rows = resp.rows or []
        if not rows:
            return {}

        def _to_float(v: Any) -> Optional[float]:
            try:
                if v is None:
                    return None
                f = float(v)
                if f != f:  # NaN
                    return None
                return f
            except (TypeError, ValueError):
                return None

        best = None
        best_dist = None
        for r in rows:
            dte_val = _to_float(r.get("dte"))
            if dte_val is None:
                continue
            dist = abs(dte_val - float(dte_target))
            if best is None or best_dist is None or dist < best_dist:
                best = r
                best_dist = dist
        if best is None:
            best = rows[0]

        vol10 = _to_float(best.get("vol10"))
        vol25 = _to_float(best.get("vol25"))
        vol50 = _to_float(best.get("vol50") or best.get("atmiv"))
        vol75 = _to_float(best.get("vol75"))
        vol90 = _to_float(best.get("vol90"))

        out: Dict[Any, Any] = {
            "asOfDate": str(best.get("tradeDate") or str(trade_date)[:10])[:10],
            "expirDate": str(best.get("expirDate") or "")[:10] if best.get("expirDate") else None,
            "dte": _to_float(best.get("dte")),
            "stockPrice": _to_float(best.get("stockPrice") or best.get("spotPrice")),
            "atm": vol50,
        }

        def _set(right: str, delta: int, v: Optional[float]) -> None:
            if v is None:
                return
            out[(right, int(delta))] = v

        for d in use_deltas:
            if int(d) == 25:
                if "C" in use_rights:
                    _set("C", 25, vol25)
                if "P" in use_rights:
                    _set("P", 25, vol75)
            if int(d) == 10:
                if "C" in use_rights:
                    _set("C", 10, vol10)
                if "P" in use_rights:
                    _set("P", 10, vol90)

        return out


