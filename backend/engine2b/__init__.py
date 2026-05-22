"""Engine 2b — SPX Iron Condor Scanner, Flex-Expiry edition.

Sibling of :mod:`backend.spx_ic` that lets the desk evaluate any SPX/SPXW
expiration (not just the same-week Friday) with the full Engine 2 odds
stack: historical breach %, outside-wing %, MAE, live EM at the requested
expiry, dealer gamma, macro / regime / deskConsensus signals.

The Friday-locked main flow (`backend.spx_ic`) is intentionally untouched.
This module imports the read-only signal helpers from `spx_ic` and
re-implements the (Friday-only) window builder + live EM picker + grid
loop in flex form, anchored on the user-supplied (entry_date, expiry)
pair. NYSE holiday math comes from :mod:`backend.engine15.trading_calendar`
so the May 25 (Memorial Day 2026) example skips correctly.

Public surface:

- :func:`compute_engine2b_flex_ic` — payload generator
- :class:`backend.engine2b.flex_windows.FlexWindow` — historical window
- :func:`backend.engine2b.flex_em.compute_expected_move_flex` — EM picker
"""

from backend.engine2b.engine import compute_engine2b_flex_ic

__all__ = ["compute_engine2b_flex_ic"]
