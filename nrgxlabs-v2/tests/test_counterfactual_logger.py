"""Unit tests for the counterfactual logger helper."""

from __future__ import annotations

from v2_app.counterfactual_logger import _shallow_agree


def test_shallow_agree_matches_verdicts() -> None:
    assert _shallow_agree({"verdict": "GO"}, {"verdict": "go"}) is True
    assert _shallow_agree({"verdict": "GO"}, {"verdict": "PASS"}) is False


def test_shallow_agree_uses_first_present_key() -> None:
    a = {"verdict": "GO", "stance": "long"}
    b = {"verdict": "GO", "stance": "short"}
    # First common key wins; "verdict" matches so they agree even though stance differs.
    assert _shallow_agree(a, b) is True


def test_shallow_agree_handles_missing_keys() -> None:
    assert _shallow_agree({}, {}) is True
    assert _shallow_agree({"foo": 1}, {"foo": 1}) is True


def test_shallow_agree_handles_none() -> None:
    assert _shallow_agree(None, None) is True
    assert _shallow_agree(None, {}) is False
    assert _shallow_agree({"verdict": "GO"}, None) is False
