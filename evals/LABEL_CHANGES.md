# Label changes

Labels in `evals/cases.yaml` were written from the policy before any evaluation run and committed first.
A label may change afterwards only if it is provably wrong per policy; every change is listed here.

| Date | Case | Field | Old | New | Justification |
|---|---|---|---|---|---|
| 2026-10-08 | S-BLK-01 (new) | case added | — | recommend_reject (or escalate) | Review found no policy-block case. Labelled and committed before it was run; the original 35 labels are unchanged. |
| 2026-10-08 | S-LLM-01 (new) | case added | — | escalate_to_human | Review found model failure was only unit-tested. Simulated provider outage (A and B only; R uses no model). Labelled and committed before it was run. |
