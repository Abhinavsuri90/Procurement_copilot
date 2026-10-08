"""Typed models: purchase requests, the decision contract, and the agents' structured outputs."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

APPROVER_ROLES = ["Manager", "Department Head", "Procurement", "Finance", "CFO", "Security", "Privacy", "Legal"]
OWNER_ROLES = ["Requester", *APPROVER_ROLES]
Severity = Literal["low", "medium", "high", "critical"]


class Recommendation(str, Enum):
    RECOMMEND_APPROVE = "recommend_approve"  # checks pass; route to the required approvers
    USE_EXISTING_TOOL = "use_existing_tool"  # an approved tool already covers the need
    REQUEST_MORE_INFO = "request_more_info"  # blocking information is missing
    ESCALATE_TO_HUMAN = "escalate_to_human"  # sensitive, exception, conflicting or degraded
    RECOMMEND_REJECT = "recommend_reject"  # hard policy block


# ---------------------------------------------------------------- request
class PurchaseRequest(BaseModel):
    """A purchase request. Fields are nullable because real requests arrive incomplete."""

    model_config = ConfigDict(extra="ignore")

    request_id: str
    requester_id: str | None = None
    product_name: str | None = None
    vendor_name: str | None = None
    category: str | None = None
    annual_cost_usd: float | None = None
    user_count: int | None = None
    business_justification: str | None = None
    data_access_level: str | None = None
    requested_integrations: list[str] | None = None
    urgency: str | None = None
    parse_warnings: list[str] = Field(default_factory=list)

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> PurchaseRequest:
        """Coerce field by field so one malformed value becomes a warning, not a crash."""
        data: dict[str, Any] = {"request_id": str(raw.get("request_id") or "UNKNOWN")}
        warnings: list[str] = []
        for key in ("requester_id", "product_name", "vendor_name", "category", "business_justification",
                    "data_access_level", "urgency"):
            value = raw.get(key)
            data[key] = str(value).strip() or None if value is not None else None
        for key, caster in (("annual_cost_usd", float), ("user_count", int)):
            value = raw.get(key)
            if value is None or value == "":
                data[key] = None
                continue
            try:
                number = caster(float(value)) if caster is int else caster(value)
                if number < 0:
                    raise ValueError("negative")
                data[key] = number
            except (TypeError, ValueError):
                data[key] = None
                warnings.append(f"{key} could not be parsed from {value!r}; treated as missing")
        integrations = raw.get("requested_integrations")
        if integrations is None:
            data["requested_integrations"] = None
        elif isinstance(integrations, str):
            data["requested_integrations"] = [s.strip() for s in integrations.split(",") if s.strip()]
        else:
            data["requested_integrations"] = [str(s).strip() for s in integrations if str(s).strip()]
        data["parse_warnings"] = warnings
        return cls(**data)

    def public_dict(self) -> dict[str, Any]:
        return self.model_dump(exclude={"parse_warnings"})


# ---------------------------------------------------------------- decision contract
class EvidenceItem(BaseModel):
    id: str
    claim: str
    source_tool: str
    call_id: str
    record_ids: list[str] = Field(default_factory=list)
    value: str = ""
    origin: Literal["agent", "deterministic"] = "agent"


class Approval(BaseModel):
    role: str
    reason: str
    rule_id: str
    source: Literal["policy_engine", "agent"] = "policy_engine"


class MissingInfo(BaseModel):
    field: str
    question_for_requester: str
    blocking: bool = True
    source: Literal["policy_engine", "agent"] = "policy_engine"


class RiskFlag(BaseModel):
    code: str
    severity: Severity
    detail: str
    source: Literal["deterministic", "agent"] = "deterministic"
    rule_id: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)


class NextStep(BaseModel):
    action: str
    owner_role: str
    detail: str


class HumanHandoff(BaseModel):
    required: bool
    assigned_role: str
    reasons: list[str] = Field(default_factory=list)
    decision_needed: str


class Override(BaseModel):
    field: str
    agent_value: Any
    final_value: Any
    reason: str


class DecisionMeta(BaseModel):
    architecture: str
    model: str
    llm_calls: int = 0
    tool_calls: int = 0
    tool_names: list[str] = Field(default_factory=list)
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    replayed: bool = False
    deterministic_only: bool = False
    warnings: list[str] = Field(default_factory=list)
    guardrails: dict[str, Any] = Field(default_factory=dict)


class Decision(BaseModel):
    request_id: str
    recommendation: Recommendation
    summary: str
    evidence: list[EvidenceItem]
    approvals_required: list[Approval]
    missing_information: list[MissingInfo]
    risk_flags: list[RiskFlag]
    next_step: NextStep
    human_handoff: HumanHandoff
    overrides: list[Override] = Field(default_factory=list)
    meta: DecisionMeta


# ---------------------------------------------------------------- agent outputs (tool-call arguments)
# Kept flat (no Optional/anyOf) so every OpenAI-compatible provider accepts the generated JSON Schema.
class DraftEvidence(BaseModel):
    id: str = Field(description="Short id such as E1, unique within this decision")
    claim: str = Field(description="One factual sentence taken from a tool result")
    source_tool: str = Field(description="Name of the tool whose output supports the claim")
    call_id: str = Field(description="call_id of that tool result, e.g. c3")
    record_ids: list[str] = Field(description="Record IDs from that tool output, e.g. E004, SW003, V010, POL-4.T2")
    value: str = Field(default="", description="Key value quoted exactly from the tool output, e.g. 15000")


class DraftApproval(BaseModel):
    role: str = Field(description="One of: " + ", ".join(APPROVER_ROLES))
    reason: str
    rule_id: str = Field(description="Policy rule ID from evaluate_policy that requires this approval")


class DraftMissingInfo(BaseModel):
    field: str = Field(description="Request field or topic that is missing or unclear")
    question_for_requester: str = Field(description="Ready-to-send question for the requester")
    blocking: bool = True


class DraftRiskFlag(BaseModel):
    code: str = Field(description="snake_case flag code, e.g. existing_tool_overlap, vendor_review_expired")
    severity: Severity
    detail: str
    evidence_ids: list[str] = Field(default_factory=list)


class DraftNextStep(BaseModel):
    action: str = Field(description="Short verb phrase, e.g. route_for_approval, request_information")
    owner_role: str = Field(description="Who acts next: " + ", ".join(OWNER_ROLES))
    detail: str


class DraftHandoff(BaseModel):
    required: bool = Field(description="True when a human reviewer must look at this beyond routine sign-off")
    assigned_role: str = Field(default="Procurement", description="Role that should review: " + ", ".join(APPROVER_ROLES))
    reasons: list[str] = Field(default_factory=list)
    decision_needed: str = Field(default="", description="The specific question the human must decide")


class AgentDecisionDraft(BaseModel):
    """Arguments of the terminal `submit_decision` tool."""

    recommendation: Recommendation
    summary: str = Field(description="At most 3 sentences, grounded in tool results")
    evidence: list[DraftEvidence]
    approvals_required: list[DraftApproval]
    missing_information: list[DraftMissingInfo] = Field(default_factory=list)
    risk_flags: list[DraftRiskFlag] = Field(default_factory=list)
    next_step: DraftNextStep
    human_handoff: DraftHandoff


class ToolCandidate(BaseModel):
    software_id: str
    product_name: str
    fit: Literal["full", "partial", "none"] = Field(description="Does this approved tool cover the stated need?")
    rationale: str
    call_id: str


class EvidencePack(BaseModel):
    """Arguments of the analyst's terminal `submit_evidence_pack` tool (architecture B, stage 1)."""

    need_summary: str = Field(description="What the requester actually needs, in one or two sentences")
    capabilities_needed: list[str] = Field(default_factory=list)
    existing_tool_candidates: list[ToolCandidate] = Field(default_factory=list)
    findings: list[DraftEvidence] = Field(description="Grounded findings with call_id and record_ids")
    open_questions: list[DraftMissingInfo] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list, description="Anything suspicious, conflicting or unverified")
