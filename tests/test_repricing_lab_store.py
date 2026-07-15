"""Repricing Lab — PIT store tests (schema, upsert idempotency, PIT reads)."""
from __future__ import annotations

import sqlite3

import pytest

from backend.repricing_lab import store


@pytest.fixture()
def db_path(tmp_path, monkeypatch):
    p = str(tmp_path / "lab_test.db")
    monkeypatch.setenv("REPRICING_LAB_SQLITE_PATH", p)
    return p


def _bar(instrument_id="eodhd:AAPL.US", session_date="2024-03-04", close=181.0,
         available_at="2024-03-05T01:00:00Z"):
    return {
        "instrument_id": instrument_id,
        "session_date": session_date,
        "open": 180.0,
        "high": 182.5,
        "low": 179.4,
        "close": close,
        "adjusted_close": close,
        "adj_factor": 1.0,
        "volume": 55_000_000.0,
        "ca_version": 1,
        "source": "eodhd",
        "available_at": available_at,
        "ingested_at": store.utcnow_iso(),
    }


# ---------------------------------------------------------------------------
# Schema / migration
# ---------------------------------------------------------------------------

def test_connect_applies_schema_and_is_idempotent(db_path):
    with store.connect() as conn:
        assert store.schema_version(conn) == 1
        tables = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    expected = {
        "schema_migration", "instrument_master", "symbol_map", "daily_bar",
        "corporate_action", "universe_snapshot", "raw_payload", "event",
        "event_cluster", "earnings_event", "estimate_snapshot",
        "fundamental_snapshot", "feature_snapshot", "research_candidate",
        "sim_order", "sim_position", "research_run", "promotion_decision",
        "job_run",
    }
    assert expected <= tables

    # Reconnecting must not re-apply or fail (idempotent migrations).
    with store.connect() as conn:
        assert store.schema_version(conn) == 1
        row = conn.execute("SELECT COUNT(*) FROM schema_migration").fetchone()
        assert int(row[0]) == 1


def test_resolve_db_path_respects_flag(db_path):
    assert store.resolve_db_path() == db_path


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------

def test_upsert_insert_then_replace_on_pk(db_path):
    with store.connect() as conn:
        assert store.upsert(conn, "daily_bar", [_bar(close=181.0)]) == 1
        # Same PK (instrument, session) with a corrected close → replaces.
        assert store.upsert(conn, "daily_bar", [_bar(close=181.5)]) == 1
        rows = store.read_rows(conn, "daily_bar")
        assert len(rows) == 1
        assert rows[0]["close"] == 181.5


def test_upsert_rejects_unknown_columns(db_path):
    with store.connect() as conn:
        bad = _bar()
        bad["closing_price"] = bad.pop("close")
        with pytest.raises(ValueError, match="unknown column"):
            store.upsert(conn, "daily_bar", [bad])
        # Loud failure must not have written anything.
        assert store.read_rows(conn, "daily_bar") == []


def test_upsert_rejects_unknown_table(db_path):
    with store.connect() as conn:
        with pytest.raises(ValueError, match="unknown table"):
            store.upsert(conn, "no_such_table", [{"a": 1}])
        with pytest.raises(ValueError, match="invalid table name"):
            store.upsert(conn, "daily_bar; DROP TABLE daily_bar", [{"a": 1}])


def test_upsert_empty_batch_is_noop(db_path):
    with store.connect() as conn:
        assert store.upsert(conn, "daily_bar", []) == 0


# ---------------------------------------------------------------------------
# PIT reads — the leakage guard
# ---------------------------------------------------------------------------

def test_read_available_excludes_future_rows(db_path):
    with store.connect() as conn:
        store.upsert(conn, "daily_bar", [
            _bar(session_date="2024-03-04", available_at="2024-03-05T01:00:00Z"),
            _bar(session_date="2024-03-05", available_at="2024-03-06T01:00:00Z"),
        ])
        # As of the evening of 3/5, only the 3/4 bar has been published.
        visible = store.read_available(
            conn, "daily_bar", as_of="2024-03-05T20:00:00Z",
            order_by="session_date",
        )
        assert [r["session_date"] for r in visible] == ["2024-03-04"]
        # A day later both are visible.
        visible = store.read_available(
            conn, "daily_bar", as_of="2024-03-06T20:00:00Z",
            order_by="session_date",
        )
        assert [r["session_date"] for r in visible] == ["2024-03-04", "2024-03-05"]


def test_read_available_bare_date_is_start_of_day_conservative(db_path):
    with store.connect() as conn:
        store.upsert(conn, "daily_bar", [
            _bar(session_date="2024-03-04", available_at="2024-03-05T01:00:00Z"),
        ])
        # "2024-03-05" < "2024-03-05T01:00:00Z" lexicographically → excluded.
        assert store.read_available(conn, "daily_bar", as_of="2024-03-05") == []
        assert len(store.read_available(conn, "daily_bar", as_of="2024-03-06")) == 1


def test_read_available_composes_with_extra_where(db_path):
    with store.connect() as conn:
        store.upsert(conn, "daily_bar", [
            _bar(instrument_id="eodhd:AAPL.US", available_at="2024-03-05T01:00:00Z"),
            _bar(instrument_id="eodhd:MSFT.US", available_at="2024-03-05T01:00:00Z"),
        ])
        rows = store.read_available(
            conn, "daily_bar", as_of="2024-03-06",
            where="instrument_id = ?", params=("eodhd:MSFT.US",),
        )
        assert len(rows) == 1
        assert rows[0]["instrument_id"] == "eodhd:MSFT.US"


def test_read_available_refuses_non_pit_table(db_path):
    with store.connect() as conn:
        # sim_position has no available_at — PIT reads must refuse it.
        with pytest.raises(ValueError, match="not PIT-readable"):
            store.read_available(conn, "sim_position", as_of="2024-03-06")


def test_read_available_requires_as_of(db_path):
    with store.connect() as conn:
        with pytest.raises(ValueError, match="as_of is required"):
            store.read_available(conn, "daily_bar", as_of="")


# ---------------------------------------------------------------------------
# Job bookkeeping
# ---------------------------------------------------------------------------

def test_job_run_lifecycle(db_path):
    with store.connect() as conn:
        started = store.record_job_start(conn, "lab_backfill_bars")
        running = store.last_job_run(conn, "lab_backfill_bars")
        assert running is not None
        assert running["finished_at"] is None and running["ok"] is None

        store.record_job_finish(
            conn, "lab_backfill_bars", started, ok=True, detail={"bars": 1234},
        )
        done = store.last_job_run(conn, "lab_backfill_bars")
        assert done["ok"] == 1
        assert done["finished_at"] is not None
        assert '"bars": 1234' in done["detail_json"]


def test_upsert_failure_rolls_back(db_path):
    with store.connect() as conn:
        # NOT NULL violation mid-batch must roll back the whole batch.
        good = _bar(session_date="2024-03-04")
        bad = _bar(session_date=None)  # session_date is part of the PK (NOT NULL)
        with pytest.raises(sqlite3.IntegrityError):
            store.upsert(conn, "daily_bar", [good, bad])
        assert store.read_rows(conn, "daily_bar") == []
