"""NRGX Equity Repricing Lab — shared point-in-time research infrastructure.

This package is the durable research foundation described in
``docs/plans/nrgx_equity_repricing_lab_implementation_plan.md``. It is NOT a
production engine:

  * It has no entry in ``ENGINE_REGISTRY``, no router, and no UI.
  * Nothing here places trades or feeds the live desk.
  * Every module is inert unless ``REPRICING_LAB_ENABLED`` is set (jobs/CLI)
    or explicitly invoked by an operator.

Contract
--------
The lab owns the point-in-time (PIT) store: an SQLite database (WAL, same
precedent as ``backend/engine14/chain_cache.py``) holding instruments, daily
bars, corporate actions, universe snapshots, the canonical event ledger, and
research-run artifacts. The single non-negotiable rule of the store is:

    A research decision at as-of time T may only read rows whose
    ``available_at`` is <= T.

``store.py`` exposes the read helpers that enforce this; feature and cohort
code must go through them rather than raw SQL so leakage protection lives in
exactly one place.

Existing engines (E18 PEAD, Ichimoku, Red Dog) and the event-study harness in
``backend/research/`` are never modified by this package — they are replayed
as benchmarks against the lab's PIT data.
"""
