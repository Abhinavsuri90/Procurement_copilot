"""Architecture A: one Procurement Agent with every tool and a terminal `submit_decision` tool."""

from __future__ import annotations

from typing import Any

from src.agents.llm import LLMSession
from src.agents.loop import run_loop
from src.agents.prompts import SINGLE_AGENT_SYSTEM, request_message
from src.schemas import AgentDecisionDraft
from src.tools import ALL_TOOLS
from src.tools.base import RunContext

SUBMIT_DECISION = ("submit_decision", AgentDecisionDraft,
                   "Submit the final structured recommendation. Call exactly once, after gathering evidence.")


def run_single_agent(ctx: RunContext, session: LLMSession) -> tuple[AgentDecisionDraft | None, dict[str, Any], str | None]:
    result = run_loop(ctx, session, agent="procurement_agent", system=SINGLE_AGENT_SYSTEM,
                      user=request_message(ctx.request, ctx.as_of.isoformat()), tool_names=ALL_TOOLS,
                      terminal=SUBMIT_DECISION)
    raw = {"decision": result.raw_output, "turns": result.turns, "repaired": result.repaired, "error": result.error}
    return result.output, raw, result.error  # type: ignore[return-value]
