"""Five-agent committee runner.

Architecture
------------
Each agent is a single Claude completion with:
  - a role-specific system prompt
  - the same setup payload
  - an attached "context" block (regime, analogues, calibrated breach)

Agents emit a strict JSON verdict (lean, machine-readable). The
Synthesizer is a 6th call that reads everyone's verdicts plus the
context, and writes the final committee decision.

This is an MVP — sequential calls, no streaming, no tool-use loop.
The shape of ``CommitteeRunner.deliberate`` is stable so we can swap
in tool-using agents (each agent calls our internal HTTP endpoints
directly) and parallel execution in a follow-up.
"""

from __future__ import annotations

import enum
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Protocol

LOG = logging.getLogger("v2.agents")


# ── Roles ──────────────────────────────────────────────────


class AgentRole(str, enum.Enum):
    RESEARCHER     = "researcher"
    QUANT          = "quant"
    DEVILS_ADVOCATE = "devils_advocate"
    RISK_OFFICER   = "risk_officer"
    SYNTHESIZER    = "synthesizer"


ROLE_ORDER: tuple[AgentRole, ...] = (
    AgentRole.RESEARCHER,
    AgentRole.QUANT,
    AgentRole.DEVILS_ADVOCATE,
    AgentRole.RISK_OFFICER,
)


SYSTEM_PROMPTS: dict[AgentRole, str] = {
    AgentRole.RESEARCHER: """You are the Researcher on a 5-agent options-trading committee.
Your job: identify the historical precedent. You have access to a context
block containing the K nearest analogue setups (cross-ticker, cross-time)
and the K nearest historical regime days. Read them. Identify the closest
real-world parallel and tell us what happened then. Be specific: cite
ticker, date, outcome, what made it analogous.

You output STRICT JSON with this exact shape, nothing else:

{
  "headline": "<one-sentence summary of the closest precedent>",
  "key_analogues": ["<ticker date outcome>", ...],
  "regime_context": "<one sentence about today's regime vs analogue regimes>",
  "lean": "constructive" | "neutral" | "cautious",
  "confidence": 0.0..1.0
}""",

    AgentRole.QUANT: """You are the Quant on a 5-agent options-trading committee.
Your job: read the calibrated breach probability and the analogue outcome
distribution. Decide whether the math says go.

You see a ``conformal_breach`` block (point estimate + interval at the
desk's risk tolerance) and an ``analogue_outcomes`` block (wins / losses
/ scratches across the K nearest setups). Compute the implied edge:

  expected_value ≈ (p_win × avg_win) - (p_loss × avg_loss)

You output STRICT JSON with this exact shape, nothing else:

{
  "p_win_estimate": 0.0..1.0,
  "p_breach_estimate": 0.0..1.0,
  "p_breach_interval": [lower, upper],
  "expected_value_bps": <signed integer>,
  "lean": "constructive" | "neutral" | "cautious",
  "confidence": 0.0..1.0,
  "rationale": "<one tight sentence>"
}""",

    AgentRole.DEVILS_ADVOCATE: """You are the Devil's Advocate on a 5-agent options-trading committee.
Your job: kill this trade. Find the single most-likely failure mode.
Don't agree with anyone. Don't hedge. Pick the strongest counter-argument.

You see the same context everyone else sees. Look for:
  - regime mismatch (today is closer to historical losses than wins)
  - feature-coverage gaps (we're z-scoring on incomplete data)
  - skew in the analogue set (concentrated in one ticker / one year)
  - calibration drift (conformal coverage tracking off the target)
  - leverage in the path (where small move → big PnL swing)

You output STRICT JSON with this exact shape, nothing else:

{
  "failure_mode": "<the one thing most likely to break this trade>",
  "evidence": "<the specific data point that supports it>",
  "p_failure_estimate": 0.0..1.0,
  "lean": "veto" | "cautious" | "accept_risk",
  "confidence": 0.0..1.0
}""",

    AgentRole.RISK_OFFICER: """You are the Risk Officer on a 5-agent options-trading committee.
Your job: size the bet and set the rails. You see the desk's existing
exposure context (if any) and the proposed setup.

Output a position size and an exit rule. You are conservative by
construction — when in doubt, size down.

You output STRICT JSON with this exact shape, nothing else:

{
  "max_size_pct_of_buying_power": 0.0..5.0,
  "stop_rule": "<short, executable stop rule>",
  "scale_in": true | false,
  "lean": "constructive" | "neutral" | "cautious",
  "confidence": 0.0..1.0,
  "rationale": "<one tight sentence>"
}""",

    AgentRole.SYNTHESIZER: """You are the Synthesizer on a 5-agent options-trading committee.
You receive four agent verdicts (Researcher, Quant, Devil's Advocate,
Risk Officer) plus the full context block. Your job is to weigh them
and emit the committee's final decision.

Hard rules:
  - If the Devil's Advocate said veto AND its confidence > 0.65, the
    committee must NOT issue an "approve". The strongest you can issue
    is "monitor".
  - If the Quant's expected_value_bps is negative AND |it| > 50, you
    cannot issue an "approve".
  - If conformal coverage is < 0.85 of the target, mark
    ``calibration_warning: true`` regardless of decision.

You output STRICT JSON with this exact shape, nothing else:

{
  "decision": "approve" | "monitor" | "decline",
  "headline": "<one-sentence verdict suitable for the trade ticket>",
  "size_pct_of_bp": 0.0..5.0,
  "stop_rule": "<short, executable stop rule>",
  "key_dissent": "<the strongest counter-argument from the committee>",
  "calibration_warning": true | false,
  "confidence": 0.0..1.0
}"""
}


# ── Data classes ───────────────────────────────────────────


@dataclass
class SetupPayload:
    """The trade setup the committee is asked to review."""

    engine: str            # "e1", "e2", etc.
    ticker: str
    structure: str         # "iron_condor", "vertical", "diagonal", etc.
    short_strikes: list[float] = field(default_factory=list)
    long_strikes: list[float] = field(default_factory=list)
    dte: int = 0
    expected_move: float | None = None
    iv_rank: float | None = None
    notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentVerdict:
    role: AgentRole
    raw: str            # full text returned by the model (for audit)
    parsed: dict[str, Any]
    model: str
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role.value,
            "raw": self.raw,
            "parsed": self.parsed,
            "model": self.model,
            "error": self.error,
        }


@dataclass
class CommitteeDecision:
    setup: SetupPayload
    context: dict[str, Any]
    agent_verdicts: list[AgentVerdict]
    synthesis: AgentVerdict | None
    elapsed_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "setup": self.setup.to_dict(),
            "context": self.context,
            "agent_verdicts": [v.to_dict() for v in self.agent_verdicts],
            "synthesis": self.synthesis.to_dict() if self.synthesis else None,
            "elapsed_ms": self.elapsed_ms,
        }


# ── Claude client protocol ─────────────────────────────────


class ClaudeClient(Protocol):
    """Minimal interface tests can satisfy without hitting the real API."""

    def complete(self, *, system: str, prompt: str, model: str) -> str: ...


class _RealClaudeClient:
    """Thin wrapper around the anthropic SDK."""

    def __init__(self) -> None:
        from anthropic import Anthropic  # imported here so tests don't need the SDK

        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise RuntimeError("ANTHROPIC_API_KEY not set")
        self._client = Anthropic(api_key=api_key)

    def complete(self, *, system: str, prompt: str, model: str) -> str:
        msg = self._client.messages.create(
            model=model,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        # SDK returns a list of content blocks; concat text blocks only.
        parts = []
        for blk in msg.content:
            text = getattr(blk, "text", None)
            if text:
                parts.append(text)
        return "".join(parts)


def get_default_client() -> ClaudeClient:
    return _RealClaudeClient()


# ── Runner ─────────────────────────────────────────────────


@dataclass
class CommitteeRunner:
    """Orchestrates a single committee deliberation."""

    client: ClaudeClient
    model_default: str = "claude-sonnet-4-5-20250929"
    model_synthesizer: str = "claude-sonnet-4-5-20250929"

    def deliberate(
        self,
        *,
        setup: SetupPayload,
        context: Mapping[str, Any],
    ) -> CommitteeDecision:
        import time

        t0 = time.monotonic()
        verdicts: list[AgentVerdict] = []
        prompt = _format_user_prompt(setup, context)

        for role in ROLE_ORDER:
            verdict = self._invoke(role, prompt, self.model_default)
            verdicts.append(verdict)

        synthesis_prompt = _format_synthesis_prompt(setup, context, verdicts)
        synthesis = self._invoke(
            AgentRole.SYNTHESIZER, synthesis_prompt, self.model_synthesizer,
        )

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        return CommitteeDecision(
            setup=setup,
            context=dict(context),
            agent_verdicts=verdicts,
            synthesis=synthesis,
            elapsed_ms=elapsed_ms,
        )

    def _invoke(self, role: AgentRole, prompt: str, model: str) -> AgentVerdict:
        try:
            raw = self.client.complete(
                system=SYSTEM_PROMPTS[role], prompt=prompt, model=model,
            )
        except Exception as exc:
            LOG.exception("agent %s failed", role.value)
            return AgentVerdict(
                role=role, raw="", parsed={}, model=model, error=str(exc),
            )
        parsed, parse_err = _safe_parse_json(raw)
        return AgentVerdict(
            role=role, raw=raw, parsed=parsed, model=model, error=parse_err,
        )


# ── Prompt formatting ─────────────────────────────────────


def _format_user_prompt(setup: SetupPayload, context: Mapping[str, Any]) -> str:
    return (
        "## Trade setup\n"
        f"{json.dumps(setup.to_dict(), indent=2)}\n\n"
        "## Foundation Brain context\n"
        f"{json.dumps(dict(context), indent=2, default=str)}\n\n"
        "Return your verdict as a single JSON object matching the schema in "
        "your system prompt. Do not include any prose outside the JSON."
    )


def _format_synthesis_prompt(
    setup: SetupPayload,
    context: Mapping[str, Any],
    verdicts: list[AgentVerdict],
) -> str:
    panel = []
    for v in verdicts:
        panel.append({
            "role": v.role.value,
            "verdict": v.parsed,
            "error": v.error,
        })
    return (
        "## Trade setup\n"
        f"{json.dumps(setup.to_dict(), indent=2)}\n\n"
        "## Foundation Brain context\n"
        f"{json.dumps(dict(context), indent=2, default=str)}\n\n"
        "## Committee verdicts\n"
        f"{json.dumps(panel, indent=2)}\n\n"
        "Apply the hard rules in your system prompt and emit the final "
        "committee decision as a single JSON object. Do not include any "
        "prose outside the JSON."
    )


# ── JSON parsing (resilient to model wrapping) ────────────


_JSON_BLOCK_RE = re.compile(r"\{(?:[^{}]|\{[^{}]*\})*\}", re.DOTALL)


def _safe_parse_json(raw: str) -> tuple[dict[str, Any], str | None]:
    """Try hard to extract a JSON object from a model response.

    Returns ``(parsed, None)`` on success, or ``({}, error)`` when no
    object can be salvaged.
    """
    if not raw:
        return {}, "empty response"
    try:
        return json.loads(raw), None
    except json.JSONDecodeError:
        pass
    # Strip markdown fences.
    stripped = raw.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped.startswith("json"):
            stripped = stripped[4:].lstrip()
        try:
            return json.loads(stripped), None
        except json.JSONDecodeError:
            pass
    # Find first {...} block.
    match = _JSON_BLOCK_RE.search(raw)
    if match:
        try:
            return json.loads(match.group(0)), None
        except json.JSONDecodeError as exc:
            return {}, f"json_block_decode: {exc}"
    return {}, "no_json_object_found"
