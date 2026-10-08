"""Loader for rules.yaml (the encoded procurement policy)."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

RULES_PATH = Path(__file__).with_name("rules.yaml")


class Threshold(BaseModel):
    rule_id: str
    max_usd: float | None
    approvers: list[str]
    label: str


class RequiredField(BaseModel):
    field: str
    rule_id: str
    label: str


class Security(BaseModel):
    rule_ids: dict[str, str]
    data_classes: list[str]
    integration_keywords: list[str]
    assessment_validity_days: int


class Rules(BaseModel):
    policy_version: str
    source: str
    required_fields: list[RequiredField]
    thresholds: list[Threshold]
    senior_approver: str
    budget: dict[str, dict[str, str]]
    overlap: dict[str, str]
    security: Security
    non_sensitive_data_classes: list[str]
    privacy: dict[str, Any]
    legal: dict[str, Any]
    ai_tools: dict[str, Any]
    injection: dict[str, str]
    tool_failure: dict[str, str]
    blocks: dict[str, Any]
    human_authority: dict[str, str]
    flags: dict[str, str]

    def severity(self, code: str) -> str:
        return self.flags.get(code, "medium")

    def all_rule_ids(self) -> set[str]:
        """Every rule ID defined in the file (used to validate agent citations)."""
        ids: set[str] = set()

        def walk(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if isinstance(value, str) and (key == "rule_id" or value.startswith("POL-")):
                        ids.add(value)
                    walk(value)
            elif isinstance(node, list):
                for item in node:
                    walk(item)

        walk(self.model_dump())
        return {i for i in ids if i.startswith("POL-")}


@lru_cache(maxsize=1)
def load_rules(path: Path = RULES_PATH) -> Rules:
    return Rules.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
