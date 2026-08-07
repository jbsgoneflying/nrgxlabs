"""Shared pytest configuration.

Ichimoku-only desk (2026-08-07): every engine except Engine 4 (Ichimoku)
was taken offline in full — their API routers are no longer mounted in
``backend.app``, so HTTP tests against those surfaces would 404. The
engine modules themselves still exist in the tree, so we skip the
route-level suites for the offline engines rather than delete them; if
an engine is ever brought back online, remove it from the list below.
"""
from __future__ import annotations

import pytest

# Test modules whose assertions target HTTP routes of offline engines.
OFFLINE_ENGINE_TEST_MODULES = {
    "test_e14_advisor_endpoint",
    "test_e14_command_deck_response",
    "test_e14_score_placement_api",
    "test_e14_wing_console_api",
    "test_e15_command_deck_api",
    "test_e1_event_date_parity",
    "test_e1_live_review",
    "test_e1_mc_always_on",
    "test_e1_tracked_trades",
    "test_e1_v2_follow_ups",
    "test_e2_advisor_always_on",
    "test_e2_command_deck_response",
    "test_e2_score_placement_api",
    "test_e2_wing_console_api",
    "test_engine18_router",
    "test_mi_router",
    "test_strike_scanner",
}

_SKIP = pytest.mark.skip(
    reason="Engine offline: Ichimoku-only desk (router not mounted since 2026-08-07)"
)


def pytest_collection_modifyitems(config, items):
    for item in items:
        module_name = item.module.__name__.rsplit(".", 1)[-1]
        if module_name in OFFLINE_ENGINE_TEST_MODULES:
            item.add_marker(_SKIP)
