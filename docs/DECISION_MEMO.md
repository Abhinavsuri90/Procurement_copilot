# Architecture decision memo

## Pre-registered ship rule (written before the first evaluation run)
Ship A (simpler) unless B beats it on a safety-critical metric (under-escalation, raw policy adherence, injection
resistance, groundedness) or on recommendation accuracy by more than run-to-run noise, at an acceptable
latency/cost increase. Ties go to A.

*Evidence and decision are added after the evaluation run.*
