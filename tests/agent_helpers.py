"""Scripted fake LLM helpers shared by the agent tests."""

from __future__ import annotations

import json
from typing import Any

from fastapi.testclient import TestClient

from mock_api.app import create_app
from src.agents.llm import LLMResponse
from src.data_access import Repository

REPO = Repository()
HTTP = TestClient(create_app(REPO.vendor_risk))


def call(name: str, **args: Any) -> dict[str, str]:
    return {"name": name, "arguments": json.dumps(args)}


def turn(*calls: dict[str, str], content: str | None = None) -> LLMResponse:
    return LLMResponse(content=content, tool_calls=list(calls), tokens_in=100, tokens_out=20, latency_ms=5.0)


def gather(request_id: str, employee: str, department: str, amount: float | None, vendor: str) -> LLMResponse:
    budget = {"department": department} | ({"amount_usd": amount} if amount is not None else {})
    return turn(call("get_requester_profile", employee_id=employee), call("check_budget", **budget),
                call("search_existing_tools", query="tool", vendor_name=vendor), call("get_vendor_risk", vendor_name=vendor),
                call("evaluate_policy", request_id=request_id))


def draft(recommendation: str = "recommend_approve", approvals: list[tuple[str, str]] | None = None,
          evidence: list[dict] | None = None, **extra: Any) -> dict[str, Any]:
    body = {
        "recommendation": recommendation,
        "summary": "Test summary. Second sentence.",
        "evidence": evidence if evidence is not None else [],
        "approvals_required": [{"role": r, "reason": "policy", "rule_id": rid} for r, rid in (approvals or [])],
        "missing_information": [],
        "risk_flags": [],
        "next_step": {"action": "route_for_approval", "owner_role": "Manager", "detail": "Send to manager"},
        "human_handoff": {"required": False, "assigned_role": "Procurement", "reasons": [], "decision_needed": "x"},
    }
    body.update(extra)
    return body


def submit(body: dict[str, Any]) -> LLMResponse:
    return turn(call("submit_decision", **body))
