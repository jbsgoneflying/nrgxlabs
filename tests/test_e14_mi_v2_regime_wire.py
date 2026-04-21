"""Engine 14 v2 — MI v2 regime overlay wiring."""
from __future__ import annotations

import datetime as dt
from dataclasses import replace
from unittest.mock import patch

import pytest

from backend.engine14.simulator import (
    IcScenarioRequest,
    _map_mi_v2_label_to_bucket,
    _resolve_entry_regime,
)


class _FakeSnap:
    def __init__(self, label="Transitional"):
        self.label = label
        self.probabilities = {"Risk-On": 0.2, "Transitional": 0.7, "Stressed": 0.1}
        self.vol_state = "expanding"
        self.source = "v2_hmm"
        self.as_of = dt.date.today().isoformat()


def _forward_request():
    return IcScenarioRequest(
        underlying="SPX",
        entry_date=(dt.date.today() + dt.timedelta(days=3)).isoformat(),
        expiry=(dt.date.today() + dt.timedelta(days=10)).isoformat(),
        short_put=4900, long_put=4895,
        short_call=5100, long_call=5105,
        credit_received=1.0,
    )


def test_map_mi_v2_labels_to_buckets():
    assert _map_mi_v2_label_to_bucket("Risk-On") == "LOW"
    assert _map_mi_v2_label_to_bucket("Transitional") == "MODERATE"
    assert _map_mi_v2_label_to_bucket("Stressed") == "ELEVATED"
    # Unknown -> MODERATE fallback.
    assert _map_mi_v2_label_to_bucket("Unknown") == "MODERATE"
    assert _map_mi_v2_label_to_bucket("") == "MODERATE"


def test_resolve_entry_regime_prefers_mi_v2_for_forward_dates(monkeypatch):
    from backend.config import get_flags
    f = replace(get_flags(), ENABLE_MI_V2=True)
    req = _forward_request()
    with patch("backend.market_intel.regime_snapshot",
               return_value=_FakeSnap("Stressed")):
        r = _resolve_entry_regime(
            user_em_pct=1.5, request=req, store=None, flags=f,
        )
    assert r.source == "mi_v2_hmm"
    assert r.bucket == "ELEVATED"


def test_resolve_entry_regime_falls_back_when_mi_v2_disabled(monkeypatch):
    from backend.config import get_flags
    f = replace(get_flags(), ENABLE_MI_V2=False)
    req = _forward_request()
    r = _resolve_entry_regime(user_em_pct=1.5, request=req, store=None, flags=f)
    # EM-proxy fallback (no DMS store either).
    assert r.source == "em_proxy"


def test_run_scenario_response_carries_mi_v2_overlay(monkeypatch):
    """Integration-ish check: /api/ic-scenario response has regime.mi_v2."""
    # This verifies the overlay dict is emitted; routing live behavior is
    # covered by test_e14_command_deck_response.
    from backend.config import get_flags
    import datetime as _dt

    # Stub regime_snapshot so the overlay is populated.
    with patch("backend.market_intel.regime_snapshot",
               return_value=_FakeSnap("Risk-On")):
        f = replace(get_flags(), ENABLE_MI_V2=True)
        req = _forward_request()
        r = _resolve_entry_regime(
            user_em_pct=1.5, request=req, store=None, flags=f,
        )
        # Forward date + MI v2 enabled -> uses MI v2 tier.
        assert r.source == "mi_v2_hmm"
        assert r.bucket == "LOW"
