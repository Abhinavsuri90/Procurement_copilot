# Architecture decision memo

## Context
Both architectures share model (Gemini 2.5 Flash, temperature 0), tools, policy engine, guardrails and schema;
only orchestration differs.

## Options
- **A, single agent:** one tool-calling loop gathers evidence and submits the decision.
- **B, staged:** an Analyst builds an evidence pack; code attaches the policy result; a Reviewer without data tools
  critiques it and submits the decision.

## Pre-registered rule (committed before the first run)
Ship A unless B beats it on a safety-critical metric (under-escalation, raw policy adherence, injection resistance,
groundedness) or on recommendation accuracy by more than run-to-run noise, at an acceptable latency/cost increase.
Ties go to A.

## Evidence (37 labelled cases, 3 runs; `evals/results/summary.md`)

| Metric | A | B | Rules |
|---|---:|---:|---:|
| Accuracy | 90.1% ± 1.3 | **96.4% ± 2.5** | 88.9% |
| Accuracy per run | 92/89/89 | 100/95/95 | 89 |
| Existing-tool cases (7) | 57% | **81%** | 43% |
| Under-escalations | 0 | 0 | 0 |
| Injection resistance | 100% | 100% | 100% |
| Grounded evidence | 96.4% | 96.4% | n/a |
| Raw policy adherence | **99.1%** | 95.2% | n/a |
| Latency p50 | **9.7 s** | 20.6 s | <1 s |
| Output tokens | **893** | 1,397 | 0 |

## Decision
**Ship B, the staged analyst-reviewer pipeline.**

## Why
- Accuracy clause met: every B run beats every A run, and paired by case-run B is right where A is wrong 8 times,
  the reverse once.
- The gain is the judgement the AI exists for: does an approved tool already cover the need? Rules cannot make it
  (43%); A usually approves instead of redirecting (57%); B's critic stage reaches 81%.
- Safety-critical metrics: B wins none. Under-escalation, injection resistance and groundedness tie; raw adherence
  favours A. B's gap is redirect drafts that omit purchase approvals (4 of 5 cases). Guardrails restore them, so
  final approvals are 100% exact for both and no unsafe decision reached a human.
- Cost: +11 s median latency, +56% output tokens, +0.4 LLM calls. Approvals take hours to days; acceptable.

## Risks and mitigations
- **Small n:** the edge is 2-3 cases, mostly one category. Grow the labelled set from real requests.
- **Lower raw adherence:** monitor the override rate; promote it to a release gate.
- **Variance** (stability: B 95%, A 92%): humans review every decision.
- **Latency spikes** (B p95 48 s): analyse asynchronously when a request is submitted.
- **Brittle schema:** B's 3 unplanned fail-safes were null/object values where text was expected; coerce them.

## What would change the decision
- More cases where B's edge falls within noise: ship A.
- Any unsafe final decision traced to B's lower raw adherence, or B under-escalating: ship A.
- A single agent with an explicit fit-check step matching B at A's latency: prefer the simpler system.
