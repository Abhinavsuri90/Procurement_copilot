"""Deterministic prompt-injection scanner for untrusted business text (policy section 9).

It flags text that tries to change the copilot's rules, fabricate approval, suppress escalation or impersonate
the system. It never decides anything by itself: a hit adds `prompt_injection_detected` and forces a human handoff.
"""

from __future__ import annotations

import re
from typing import Any

PATTERNS: list[tuple[str, str]] = [
    ("ignore_instructions", r"\b(ignore|disregard|forget|bypass)\b[^.\n]{0,40}\b(instructions?|rules?|polic(y|ies)|"
                            r"controls?|guidelines?|checks?|procurement|previous|prior|above)\b"),
    ("fabricated_approval", r"\b(treat|consider|mark|record|log)\b[^.\n]{0,40}\bas\b[^.\n]{0,20}\b(pre-?)?approved\b"),
    ("fabricated_approval", r"\b(cfo|ceo|vp|director|management)[- ]+(has\s+)?(already\s+)?"
                            r"(pre-?)?approved\b"),
    ("fabricated_approval", r"\balready\s+(been\s+)?(pre-?)?approved\b|\bpre-?approved\b"),
    ("demand_approval", r"\bapprove\b[^.\n]{0,25}\b(immediately|now|automatically|without)\b"),
    ("demand_approval", r"\b(auto-?approve|rubber[- ]stamp)\b"),
    ("suppress_controls", r"\b(skip|no need for|waive|bypass)\b[^.\n]{0,30}\b(approvals?|approval process|"
                          r"(security|legal|privacy|procurement|manager) (reviews?|approvals?)|escalation)\b"),
    ("suppress_controls", r"\b(do not|don't|never)\s+(escalate|flag|review|ask|route|mention)\b"),
    ("role_hijack", r"\byou are now\b|\bact as\b[^.\n]{0,30}\b(admin|system|approver)\b|\bnew instructions\b"),
    ("system_impersonation", r"(^|\n|\s)(system|assistant|developer)\s*:\s|<\s*/?\s*(system|instructions?)\s*>"),
    ("exfiltration", r"\b(reveal|print|show|expose)\b[^.\n]{0,30}\b(system prompt|api key|secrets?|credentials?)\b"),
    ("hidden_markup", r"<!--|<script\b|[\u200b\u200c\u200d\u2060\ufeff]"),
]
_COMPILED = [(name, re.compile(rx, re.IGNORECASE)) for name, rx in PATTERNS]


def scan_text(text: str | None, location: str) -> list[dict[str, str]]:
    if not text:
        return []
    hits = []
    for name, rx in _COMPILED:
        match = rx.search(text)
        if match:
            start = max(0, match.start() - 20)
            hits.append({"location": location, "pattern": name,
                         "excerpt": text[start:match.end() + 20].strip().replace("\n", " ")})
    return hits


def scan_value(value: Any, location: str) -> list[dict[str, str]]:
    """Scan every string inside a JSON-like value."""
    if isinstance(value, str):
        return scan_text(value, location)
    hits: list[dict[str, str]] = []
    if isinstance(value, dict):
        for key, item in value.items():
            hits += scan_value(item, f"{location}.{key}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            hits += scan_value(item, f"{location}[{i}]")
    return hits


def dedupe(hits: list[dict[str, str]]) -> list[dict[str, str]]:
    seen, out = set(), []
    for h in hits:
        key = (h["location"], h["pattern"])
        if key not in seen:
            seen.add(key)
            out.append(h)
    return out
