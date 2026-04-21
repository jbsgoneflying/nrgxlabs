"""Engine 14 v2 — E14_EMIT_DESK_CONSENSUS flag behaviour."""
from __future__ import annotations

from dataclasses import replace

import pytest


def test_strip_removes_reconcile_overall_and_engine2_verdict():
    from backend.routers.engine14_ic_scenario import _augment_scenario_v2
    from backend.config import get_flags

    f_off = replace(
        get_flags(),
        ENABLE_E14_V2=True,
        E14_EMIT_DESK_CONSENSUS=False,
    )
    base = {
        "engine": 14,
        "reconcile": {"overall": "PASS", "chips": [{"status": "ok"}]},
        "engine2":   {"deskConsensus": {"verdict": "TRADE"}, "recSimple": "TRADE"},
    }
    out = _augment_scenario_v2(dict(base), body={}, flags=f_off)
    assert "overall" not in out["reconcile"]
    assert out["reconcile"]["chips"]   # per-chip detail preserved
    assert "deskConsensus" not in out["engine2"]
    assert "recSimple" not in out["engine2"]


def test_strip_preserves_when_flag_on():
    from backend.routers.engine14_ic_scenario import _augment_scenario_v2
    from backend.config import get_flags

    f_on = replace(
        get_flags(),
        ENABLE_E14_V2=True,
        E14_EMIT_DESK_CONSENSUS=True,
    )
    base = {
        "engine": 14,
        "reconcile": {"overall": "PASS"},
        "engine2":   {"deskConsensus": {"verdict": "TRADE"}, "recSimple": "TRADE"},
    }
    out = _augment_scenario_v2(dict(base), body={}, flags=f_on)
    assert out["reconcile"]["overall"] == "PASS"
    assert out["engine2"]["deskConsensus"] == {"verdict": "TRADE"}


def test_source_chip_defaults_to_desk_default():
    from backend.routers.engine14_ic_scenario import _augment_scenario_v2
    from backend.config import get_flags
    f = get_flags()
    out = _augment_scenario_v2({}, body={}, flags=f)
    assert out["sourceChip"] == "desk_default"


def test_source_chip_respects_user_override():
    from backend.routers.engine14_ic_scenario import _augment_scenario_v2
    from backend.config import get_flags
    f = get_flags()
    out = _augment_scenario_v2({}, body={"sourceChip": "user_override"}, flags=f)
    assert out["sourceChip"] == "user_override"


def test_placement_rank_is_parsed_int():
    from backend.routers.engine14_ic_scenario import _augment_scenario_v2
    from backend.config import get_flags
    f = get_flags()
    out = _augment_scenario_v2({}, body={"placementRank": "3"}, flags=f)
    assert out["placementRank"] == 3
