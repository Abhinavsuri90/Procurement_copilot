# Architecture decision memo

## Context
Both architectures share model (Gemini 2.5 Flash, temperature 0), tools, policy engine, guardrails and schema;
only orchestration differs.

## Options
- **A, single agent:** one tool-calling loop gathers evidence and submits the decision.
- **B, staged:** an Analyst builds a structured evidence pack; code attaches the policy result; a Reviewer without
  data tools critiques it and submits the decision.

## Pre-registered rule (committed before the first run)
Ship A unless B beats it on a safety-critical metric or on recommendation accuracy by more than run-to-run noise,
at an acceptable latency/cost increase. Ties go to A.

## Evidence (35 labelled cases, 3 runs; `evals/results/summary.md`)

| Metric | A | B | Rules |
|---|---:|---:|---:|
| Accuracy | 89.5% ± 1.4 | **96.2% ± 2.7** | 88.6% |
| Accuracy per run | 91/89/89 | 100/94/94 | 89 |
| Existing-tool cases (7) | 57% | **81%** | 43% |
| Under-escalations | 0 | 0 | 0 |
| Injection resistance | 100% | 100% | 100% |
| Grounded evidence | 96.3% | 96.4% | n/a |
| Raw policy adherence | **99.0%** | 95.2% | n/a |
| Latency p50 | **9.7 s** | 21.5 s | <1 s |
| Output tokens | **921** | 1,449 | 0 |

## Decision
**Ship B, the staged analyst-reviewer pipeline.**

## Why
- Every B run beats every A run. Paired by case-run, B is right where A is wrong 8 times, the reverse once. That
  exceeds run-to-run noise, so the rule's accuracy condition is met.
- The gain is in the judgement the AI exists for: whether an approved tool already covers the need. Rules cannot
  make it (43%); A usually approves instead of redirecting (57%); B's critic stage reaches 81%.
- No safety regression: guardrails make both identical on approvals (100% exact), required flags, escalation and
  injection handling.
- B's lower raw adherence is mostly omitted purchase approvals on redirects, which guardrails restore. A failed
  safe more often (3.8% vs 0.9%), e.g. stopping without a decision on an unknown requester.
- Cost: +12 s median latency, +57% output tokens, +0.4 LLM calls. Approvals take hours to days, so this is
  acceptable.

## Risks and mitigations
- **Small n:** the edge is about 2-3 cases, mostly one category. Grow the labelled set from real requests; keep
  the replay check in CI.
- **Variance** (stability: B 94%, A 91%): humans review every decision.
- **Latency spikes** (B p95 48 s): analyse asynchronously when a request is submitted.
- **Guardrails can overrule a right answer:** one B redirect lacked grounded catalog evidence and was overridden.
  Accepted as the conservative trade-off; tracked via override rate.

## What would change the decision
- More cases where B's edge falls within noise: ship A.
- Any B under-escalation, or groundedness below A: ship A.
- A single agent with an explicit fit-check step matching B at A's latency: prefer it as the simpler system.
