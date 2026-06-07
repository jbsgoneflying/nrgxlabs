#!/usr/bin/env python3
"""Engine 5 – Nightly Global EOD Refresh Script (cron wrapper).

Usage:
    python scripts/refresh_engine5_snapshot.py [--force]
"""

from __future__ import annotations

import logging
import os
import sys

try:
    from dotenv import load_dotenv  # type: ignore
    load_dotenv()
except Exception:
    pass

# Ensure repo root is on sys.path for cron-friendly execution.
REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
)

from backend.engine5_pipeline import run_pipeline
from backend.redis_store import get_store_optional

LOG = logging.getLogger(__name__)


def _emit_sequencer_events(store, snapshot_id: str) -> None:
    """Compare new snapshot state to prior state and emit SequencerEvents."""
    try:
        import json
        from backend.sequencer import detect_state_changes, current_week_id

        snap = store.get_json(f"engine5:snapshot:{snapshot_id}")
        if not snap:
            return

        data = snap.get("data", {})
        regime = data.get("regime", {})
        vol = data.get("volLeadLag", {})

        # Dealer gamma from ORATS SPY strikes (if available)
        dg_sign = ""
        try:
            from backend.orats_client import OratsClient
            from backend.dealer_gamma_context import compute_dealer_gamma_context
            orats = OratsClient.from_env()
            spy_strikes = orats.live_strikes(ticker="SPY")
            if spy_strikes.rows:
                dg = compute_dealer_gamma_context(spy_strikes.rows)
                dg_sign = dg.get("netGammaSign", "")
                LOG.info("Dealer gamma sign: %s", dg_sign or "unavailable")
        except Exception as e:
            LOG.debug("Dealer gamma unavailable for sequencer: %s", e)

        current_state = {
            "regime": regime.get("label") or regime.get("current_label") or "",
            "vol_leadlag": vol.get("vol_lag_state") or vol.get("volLagState") or "",
            "dealer_gamma": dg_sign,
            "earnings_dispersion": str(store.get_json("sequencer:state:earnings_dispersion") or ""),
            "red_dog_breadth": str(store.get_json("sequencer:state:red_dog_breadth") or ""),
            "ichimoku_breadth": str(store.get_json("sequencer:state:ichimoku_breadth") or ""),
        }

        # Load prior state from Redis
        prior_raw = store.get_json("sequencer:prior_state")
        prior_state = prior_raw if isinstance(prior_raw, dict) else {}

        if prior_state:
            events = detect_state_changes(
                previous=prior_state,
                current=current_state,
            )
            if events:
                wid = current_week_id()
                # Append events to the week's list in Redis
                existing_raw = store.get_json(f"sequencer:week:{wid}")
                existing = existing_raw if isinstance(existing_raw, list) else []
                for ev in events:
                    existing.append(ev.to_dict())
                    LOG.info("Sequencer event: %s (%s -> %s)", ev.event_type, ev.from_state, ev.to_state)
                store.set_json(f"sequencer:week:{wid}", existing, ttl_s=30 * 86400)
            else:
                LOG.info("No sequencer state changes detected.")
        else:
            LOG.info("No prior sequencer state; initializing baseline.")

        # Save current state as prior for next run
        store.set_json("sequencer:prior_state", current_state, ttl_s=7 * 86400)

    except Exception as e:
        LOG.warning("Sequencer event emission failed: %s", e)


def main() -> int:
    force = "--force" in sys.argv
    exit_code, snapshot_id = run_pipeline(force=force, source="cron")
    if snapshot_id:
        LOG.info("Snapshot created: %s", snapshot_id)
        # Emit sequencer events on state changes (NRGX Labs 2.0)
        store = get_store_optional()
        if store:
            _emit_sequencer_events(store, snapshot_id)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
