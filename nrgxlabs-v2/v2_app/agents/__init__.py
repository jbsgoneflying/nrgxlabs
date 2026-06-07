"""Phase 1 module 4 — agent committee.

A 5-agent Claude mesh that deliberates over a candidate trade. Each
agent has a sharply-defined role, sees the same setup, and writes a
typed verdict. The Synthesizer reads everyone's output and emits the
final committee decision.

The committee reads from the Foundation Brain (regime encoder,
contrastive analogues, conformal calibrator) so a single deliberation
fuses learned regime context, historical precedent, and calibrated
breach probability into one verdict.
"""

from .committee import (  # noqa: F401
    AgentRole,
    AgentVerdict,
    CommitteeDecision,
    CommitteeRunner,
    SetupPayload,
)
