"""Repricing Lab PIT store — SQLite open/migrate/upsert/read helpers.

Modeled on the repo's blessed durable-store precedent
(``backend/engine14/chain_cache.py``): stdlib ``sqlite3``, WAL journal,
module-level lock, connection-per-batch via a context manager. No ORM, no new
dependencies.

Three rules every caller must respect:

1. **Writes go through ``upsert()``** (INSERT OR REPLACE keyed on the table's
   natural primary key) so re-running any backfill is an idempotent no-op.
2. **PIT reads go through ``read_available()``**, which enforces
   ``available_at <= as_of``. Leakage protection lives here, in one place —
   feature/cohort code must not hand-roll that predicate.
3. **Jobs record themselves** via ``record_job_start``/``record_job_finish``
   so `job_run` is the monitoring surface for cron.

Schema is applied from ``schema.sql`` and versioned in ``schema_migration``.
Migration v(N+1) files would be added as ``schema_v{N+1}.sql`` and listed in
``_MIGRATIONS``; statements must stay idempotent.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Sequence, Tuple

from backend.config import get_flags

LOG = logging.getLogger("repricing_lab.store")

_DB_LOCK = threading.Lock()

# Ordered list of (version, sql filename relative to this package).
_MIGRATIONS: Tuple[Tuple[int, str], ...] = (
    (1, "schema.sql"),
)


def utcnow_iso() -> str:
    """UTC timestamp in the repo's canonical Z-suffixed second precision."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def resolve_db_path() -> str:
    """DB path from ``REPRICING_LAB_SQLITE_PATH``, workspace-root relative."""
    flags = get_flags()
    raw = str(
        getattr(flags, "REPRICING_LAB_SQLITE_PATH", None) or "data/repricing_lab.db"
    )
    p = Path(raw)
    if not p.is_absolute():
        here = Path(__file__).resolve()
        root = here.parent.parent.parent  # backend/repricing_lab/ -> workspace root
        p = (root / raw).resolve()
    return str(p)


@contextmanager
def connect(db_path: Optional[str] = None) -> Iterator[sqlite3.Connection]:
    """Open (and migrate) the lab store for a batch of work.

    Holds the module lock for the duration — lab jobs are single-writer by
    design (serialized cron), so simplicity beats concurrency here, matching
    the engine14 cache.
    """
    path = db_path or resolve_db_path()
    if path != ":memory:":
        parent = Path(path).parent
        try:
            parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            LOG.warning(
                "repricing_lab store dir not writable (%s) — using in-memory DB.",
                parent,
            )
            path = ":memory:"

    with _DB_LOCK:
        conn = sqlite3.connect(path, timeout=30.0, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute("PRAGMA synchronous=NORMAL;")
            conn.execute("PRAGMA temp_store=MEMORY;")
            conn.execute("PRAGMA busy_timeout=30000;")
            conn.row_factory = sqlite3.Row
            migrate(conn)
            yield conn
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Migrations
# ---------------------------------------------------------------------------

def migrate(conn: sqlite3.Connection) -> int:
    """Apply any unapplied migrations. Returns the resulting schema version."""
    conn.execute(
        """CREATE TABLE IF NOT EXISTS schema_migration (
               version INTEGER PRIMARY KEY,
               applied_at TEXT NOT NULL
           )"""
    )
    row = conn.execute("SELECT MAX(version) FROM schema_migration").fetchone()
    current = int(row[0]) if row and row[0] is not None else 0

    for version, filename in _MIGRATIONS:
        if version <= current:
            continue
        sql = (Path(__file__).parent / filename).read_text()
        # No explicit transaction here: executescript() issues an implicit
        # COMMIT before running, which would break a BEGIN wrapper. All DDL
        # is IF-NOT-EXISTS idempotent, so a partial failure is safe to re-run
        # (the version row is only written after the script succeeds).
        conn.executescript(sql)
        conn.execute(
            "INSERT OR REPLACE INTO schema_migration (version, applied_at) VALUES (?, ?)",
            (version, utcnow_iso()),
        )
        current = version
        LOG.info("repricing_lab store: applied schema migration v%d", version)
    return current


def schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) FROM schema_migration").fetchone()
    return int(row[0]) if row and row[0] is not None else 0


# ---------------------------------------------------------------------------
# Column validation (guards generic upsert against typos / injection)
# ---------------------------------------------------------------------------

def table_columns(conn: sqlite3.Connection, table: str) -> List[str]:
    if not table.replace("_", "").isalnum():
        raise ValueError(f"invalid table name: {table!r}")
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    if not rows:
        raise ValueError(f"unknown table: {table!r}")
    return [str(r[1]) for r in rows]


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def upsert(
    conn: sqlite3.Connection,
    table: str,
    rows: Sequence[Mapping[str, Any]],
) -> int:
    """INSERT OR REPLACE ``rows`` into ``table``, keyed on its primary key.

    Column names are validated against the live schema; unknown keys raise
    rather than being silently dropped (a misspelled field must fail loudly,
    not produce a null column). Returns the number of rows written.
    """
    if not rows:
        return 0
    valid = set(table_columns(conn, table))
    cols = list(rows[0].keys())
    unknown = [c for c in cols if c not in valid]
    if unknown:
        raise ValueError(f"unknown column(s) for {table}: {unknown}")
    for r in rows[1:]:
        if list(r.keys()) != cols:
            raise ValueError(f"inconsistent row keys for {table} batch")

    placeholders = ",".join("?" for _ in cols)
    col_sql = ",".join(cols)
    conn.execute("BEGIN IMMEDIATE;")
    try:
        conn.executemany(
            f"INSERT OR REPLACE INTO {table} ({col_sql}) VALUES ({placeholders})",
            [tuple(r[c] for c in cols) for r in rows],
        )
        conn.execute("COMMIT;")
    except Exception:
        conn.execute("ROLLBACK;")
        raise
    return len(rows)


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def read_rows(
    conn: sqlite3.Connection,
    table: str,
    *,
    where: str = "",
    params: Sequence[Any] = (),
    order_by: str = "",
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Plain (non-PIT) read. Prefer ``read_available`` for research paths."""
    table_columns(conn, table)  # validates table name
    sql = f"SELECT * FROM {table}"
    if where:
        sql += f" WHERE {where}"
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql, tuple(params)).fetchall()]


def read_available(
    conn: sqlite3.Connection,
    table: str,
    *,
    as_of: str,
    where: str = "",
    params: Sequence[Any] = (),
    order_by: str = "",
    limit: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Point-in-time read: only rows with ``available_at <= as_of``.

    This is THE leakage guard. ``as_of`` may be a date ("2024-03-05") or a
    full timestamp; ISO-8601 strings compare lexicographically, and a bare
    date sorts before any timestamp on the same day, so a date `as_of` is the
    conservative (start-of-day) interpretation.
    """
    cols = table_columns(conn, table)
    if "available_at" not in cols:
        raise ValueError(f"table {table!r} has no available_at column — not PIT-readable")
    if not as_of:
        raise ValueError("as_of is required for PIT reads")
    clauses = ["available_at <= ?"]
    all_params: List[Any] = [str(as_of)]
    if where:
        clauses.append(f"({where})")
        all_params.extend(params)
    sql = f"SELECT * FROM {table} WHERE " + " AND ".join(clauses)
    if order_by:
        sql += f" ORDER BY {order_by}"
    if limit is not None:
        sql += f" LIMIT {int(limit)}"
    return [dict(r) for r in conn.execute(sql, tuple(all_params)).fetchall()]


# ---------------------------------------------------------------------------
# Job bookkeeping
# ---------------------------------------------------------------------------

def record_job_start(conn: sqlite3.Connection, job_name: str) -> str:
    """Insert a running job_run row; returns its started_at key."""
    started_at = utcnow_iso()
    conn.execute(
        "INSERT OR REPLACE INTO job_run (job_name, started_at, finished_at, ok, detail_json)"
        " VALUES (?, ?, NULL, NULL, NULL)",
        (job_name, started_at),
    )
    return started_at


def record_job_finish(
    conn: sqlite3.Connection,
    job_name: str,
    started_at: str,
    *,
    ok: bool,
    detail: Optional[Mapping[str, Any]] = None,
) -> None:
    conn.execute(
        "UPDATE job_run SET finished_at = ?, ok = ?, detail_json = ?"
        " WHERE job_name = ? AND started_at = ?",
        (
            utcnow_iso(),
            1 if ok else 0,
            json.dumps(dict(detail or {}), sort_keys=True),
            job_name,
            started_at,
        ),
    )


def last_job_run(conn: sqlite3.Connection, job_name: str) -> Optional[Dict[str, Any]]:
    rows = read_rows(
        conn,
        "job_run",
        where="job_name = ?",
        params=(job_name,),
        order_by="started_at DESC",
        limit=1,
    )
    return rows[0] if rows else None
