PY ?= .venv/bin/python

.PHONY: start test lint typecheck e2e check eval eval-replay public-eval

start:
	python start.py

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check .

typecheck:
	$(PY) -m mypy

e2e:             ## browser tests: pip install -r requirements-e2e.txt && python -m playwright install chromium
	$(PY) -m pytest -m e2e tests/e2e -q

check: lint typecheck test eval-replay   ## everything CI runs except the browser job

eval:            ## live run (needs LLM_API_KEY); records cassettes and refreshes evals/results
	LLM_MODE=record $(PY) -m evals.run_eval --arch A B R --runs 3 --workers 10

eval-replay:     ## offline, no key: replays the committed cassettes and reproduces evals/results exactly
	LLM_MODE=replay $(PY) -m evals.run_eval --arch A B R --runs 3 --workers 8 --check

public-eval:     ## the starter pack's public harness through the handle_request adapter (needs the mock service)
	$(PY) evals/run_public_evals.py --architecture single
	$(PY) evals/run_public_evals.py --architecture staged
