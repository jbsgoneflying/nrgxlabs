"""Engine 14 v2 — shared scenario cache tests."""
from __future__ import annotations

from backend.engine14 import (
    build_scenario_cache_key,
    clear_scenario_cache,
    get_or_compute_scenario,
    get_scenario_cache_stats,
    reset_scenario_cache_stats,
)


def setup_function(_fn):
    clear_scenario_cache()
    reset_scenario_cache_stats()


def test_cache_hit_reuses_payload():
    calls = {"n": 0}

    def _compute():
        calls["n"] += 1
        return {"payload": calls["n"]}

    kw = dict(
        entry_date="2026-01-06", expiry_date="2026-01-10",
        strikes=(4900, 4895, 5100, 5105), credit=1.5,
        flags_fp=("v2",), compute=_compute,
    )
    first = get_or_compute_scenario(**kw)
    second = get_or_compute_scenario(**kw)
    assert first is second
    assert calls["n"] == 1
    stats = get_scenario_cache_stats()
    assert stats["hits"] >= 1
    assert stats["stores"] == 1


def test_cache_miss_recomputes_on_key_change():
    calls = {"n": 0}

    def _compute():
        calls["n"] += 1
        return {"payload": calls["n"]}

    base = dict(
        entry_date="2026-01-06", expiry_date="2026-01-10",
        strikes=(4900, 4895, 5100, 5105), credit=1.5,
        flags_fp=("v2",), compute=_compute,
    )
    get_or_compute_scenario(**base)
    # Different strikes -> different key -> cache miss.
    get_or_compute_scenario(**{**base, "strikes": (4800, 4795, 5200, 5205)})
    assert calls["n"] == 2


def test_force_refresh_bypasses_cache():
    calls = {"n": 0}

    def _compute():
        calls["n"] += 1
        return {"payload": calls["n"]}

    kw = dict(
        entry_date="2026-01-06", expiry_date="2026-01-10",
        strikes=(4900, 4895, 5100, 5105), credit=1.5,
        flags_fp=("v2",), compute=_compute,
    )
    get_or_compute_scenario(**kw)
    get_or_compute_scenario(**kw, force_refresh=True)
    assert calls["n"] == 2


def test_build_key_stable_across_calls():
    k1 = build_scenario_cache_key(
        entry_date="2026-01-06", expiry_date="2026-01-10",
        strikes=(4900, 4895, 5100, 5105), credit=1.5,
        flags_fp=("v2",),
    )
    k2 = build_scenario_cache_key(
        entry_date="2026-01-06", expiry_date="2026-01-10",
        strikes=(4900, 4895, 5100, 5105), credit=1.5,
        flags_fp=("v2",),
    )
    assert k1 == k2
