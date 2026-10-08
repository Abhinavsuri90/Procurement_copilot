# Starter pack fixes

Every change to starter code is a reviewable diff against the first commit (`chore: import starter pack as provided`).
Intentionally messy *data* (expired/conflicting vendors, the 503 vendor, injected text, missing fields) was left
untouched and is handled in code — see `DATA_NOTES.md`.

How the scaffold was checked: `python verify_setup.py` (passed), bare `pytest` (collection error), `python -m pytest`
(10 passed), `python evals/run_public_evals.py` (stops: adapter not implemented), plus probing the mock API and the
data loaders with edge inputs.

| # | Area | Symptom | Root cause | Fix | Test |
|---|---|---|---|---|---|
| 1 | Tests | `pytest` fails during collection: `ModuleNotFoundError: No module named 'mock_api'` | pytest's default rootdir insertion puts `tests/` (no `__init__.py`) on `sys.path`, not the project root; only `python -m pytest` worked | `pyproject.toml` sets `pythonpath = ["."]` | whole suite runs with bare `pytest` (also in CI) |
| 2 | Mock API | `GET /vendor-risk/signflow` or `/vendor-risk/%20SignFlow` → 404 for a known vendor; names containing `/` → generic route 404; names were percent-decoded twice | exact-match dict lookup; `{vendor_name}` path converter rejects `/`; extra `unquote()` on an already-decoded path value | normalised (case/whitespace-insensitive) index, `{vendor_name:path}`, no second decode; `create_app(data)` factory so tests/eval can mount it in-process; honours `DATA_DIR` | `tests/test_mock_api_lookup.py` |
| 3 | Data access | Blank CSV cells (e.g. E010's `manager_id`, NimbusAI's review date) loaded as `NaN`, which is truthy, so `if row["manager_id"]:` passes for an employee without a manager | pandas `read_csv` default NA handling | typed repository on `csv` + Pydantic: blanks → `None`, numbers parsed, case-insensitive lookups, data overlays for fixtures; pandas dropped | `tests/test_data_access.py` |
| 4 | Vendor client | Any 404, 503 or timeout raised `HTTPError`/`Timeout` into the caller; no retries; one outage would crash an analysis | `raise_for_status()` with no handling; single attempt | `VendorClient` never raises: returns `ok` / `not_found` / `unavailable`, retries timeouts, connection errors, 429 and 5xx (2 retries, exponential backoff + jitter, 3 s timeout), fault injection (`down`/`slow`/`flaky`) | `tests/test_tools.py` (`test_vendor_client_retries_5xx_and_not_404`, fault tests) |
| 5 | Dependencies | Starlette ≥ 1.x warns on the httpx-based `TestClient` and on its per-request `timeout` argument | upstream deprecation | warnings filtered in pytest config; runtime HTTP uses plain `httpx` | — |
