# Student submission checklist

Before submitting, confirm that:

- [x] `python verify_setup.py` passes in your project environment. — *also run in CI on every push*
- [x] The product can process a request end-to-end. — `python start.py` → http://localhost:8000; `tests/test_api.py`
- [x] Architecture A is a working single-agent baseline. — `src/agents/single_agent.py`
- [x] Architecture B is a lightweight staged / 2-agent variant. — `src/agents/staged.py` (analyst → code handoff → reviewer)
- [x] At least 3 tools are used; at least 1 tool/check is deterministic. — six tools, all deterministic (`src/tools/`); the policy engine is pure code
- [x] Recommendations are returned in the `ProcurementDecision` structure. — `src/solution.py::handle_request`; public harness 6/6 for both architectures (CI)
- [x] Important evidence is visible to the user. — decision panel evidence with tool/record-ID chips that open the raw tool output
- [x] Missing/conflicting/unavailable evidence is handled without fabrication. — `request_more_info`, `conflicting_vendor_evidence`, `vendor_risk_unavailable`; groundedness guardrail removes uncited evidence
- [x] Human approval is preserved for sensitive decisions. — every decision needs per-role human sign-off; escalation floor in `src/agents/guardrails.py`
- [x] Prompt injection inside business data does not override system behavior. — injection eval pairs: 100% resistance, approvals equal to clean controls
- [x] Date-based checks use the policy's data snapshot / reference date. — 2026-09-30 parsed from `data/procurement_policy.md` (`src/config.py`)
- [x] The same evaluation cases were run on both architectures. — `evals/cases.yaml` (37 cases) × 3 runs × A, B and rules-only
- [x] Latency and LLM/tool-call counts are reported. — `evals/results/summary.md`
- [x] The decision memo is <= 500 words and supported by evaluation evidence. — `docs/DECISION_MEMO.md` (length test-enforced)
- [x] Setup instructions work from a clean environment. — fresh-clone check; CI installs from scratch on Python 3.11 and 3.13
- [x] Any LLM/provider SDK you added is present in `requirements.txt`. — `openai` (any OpenAI-compatible endpoint)
- [x] `.env`, API keys, and other secrets are not committed. — `.env` git-ignored; `.env.example` only
