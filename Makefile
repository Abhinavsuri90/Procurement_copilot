PY ?= .venv/bin/python

.PHONY: start test lint eval eval-replay public-eval

start:
	python start.py

test:
	$(PY) -m pytest -q

lint:
	$(PY) -m ruff check .

eval:            ## live run (needs LLM_API_KEY); records cassettes and refreshes evals/results
	LLM_MODE=record $(PY) -m evals.run_eval --arch A B R --runs 3 --workers 6

eval-replay:     ## offline, no key: replays the committed cassettes and reproduces evals/results exactly
	LLM_MODE=replay $(PY) -m evals.run_eval --arch A B R --runs 3 --workers 6 --check

public-eval:     ## the starter pack's public harness through the handle_request adapter
	$(PY) evals/run_public_evals.py --architecture single
	$(PY) evals/run_public_evals.py --architecture staged
