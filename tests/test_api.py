"""API smoke tests: queue, create, analyze, human actions with override/exception rules, audit log."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from src.web import main


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DB_PATH", str(tmp_path / "app.db"))
    monkeypatch.setenv("VENDOR_RETRIES", "0")
    monkeypatch.setenv("VENDOR_SERVICE_FAULT", "down")
    monkeypatch.setenv("CASSETTE_DIR", str(tmp_path / "none"))
    main.store.cache_clear()
    yield TestClient(main.app)
    main.store.cache_clear()


def test_health_and_index(client):
    h = client.get("/api/health").json()
    assert h["app"] == "ok" and "configured" in h["llm"] and "ok" in h["vendor_service"]
    assert client.get("/").status_code == 200 and "Procurement Request Copilot" in client.get("/").text


def test_queue_create_and_detail(client):
    queue = client.get("/api/requests").json()
    assert len(queue) == 10 and {r["status"] for r in queue} == {"New"}
    created = client.post("/api/requests", json={"requester_id": "E002", "product_name": "Widget",
                                                 "vendor_name": "TaskFlow", "annual_cost_usd": 500}).json()
    assert created["request_id"] == "NEW-0001"
    assert client.get("/api/requests/NEW-0001").json()["status"] == "New"
    assert client.get("/api/requests/NOPE").status_code == 404
    assert client.post("/api/requests", json={"requester_id": "E002"}).status_code == 422


def test_analyze_then_human_actions_are_audited(client):
    assert client.post("/api/requests/REQ-1001/actions",
                       json={"action": "approve", "reviewer_role": "Manager"}).status_code == 409
    res = client.post("/api/requests/REQ-1001/analyze?arch=A").json()
    d = res["decision"]
    assert d["meta"]["deterministic_only"] and d["recommendation"] == "escalate_to_human"  # outage + no key
    assert client.get(f"/api/runs/{res['run_id']}").json()["trace"]
    assert client.post("/api/requests/REQ-1001/analyze?arch=Z").status_code == 400

    # approving an escalation needs a reason, and only the required approvers can sign off
    assert client.post("/api/requests/REQ-1001/actions",
                       json={"action": "approve", "reviewer_role": "Manager"}).status_code == 422
    assert client.post("/api/requests/REQ-1001/actions",
                       json={"action": "approve", "reviewer_role": "CFO", "reason": "x"}).status_code == 422
    ok = client.post("/api/requests/REQ-1001/actions", json={"action": "approve", "reviewer_role": "Manager",
                                                             "reason": "Vendor confirmed by phone"})
    assert ok.status_code == 201 and ok.json()["status"] == "Partially approved"
    assert ok.json()["signoffs"]["pending"] == ["Security"]
    entry = ok.json()["action"]
    assert entry["ai_recommendation"] == "escalate_to_human" and entry["architecture"] and entry["model"]
    assert entry["is_override"] == 1
    again = client.post("/api/requests/REQ-1001/actions", json={"action": "approve", "reviewer_role": "Manager",
                                                                "reason": "again"})
    assert again.status_code == 409
    done = client.post("/api/requests/REQ-1001/actions", json={"action": "approve", "reviewer_role": "Security",
                                                               "reason": "Manual assessment completed"})
    assert done.json()["status"] == "Approved"
    closed = client.post("/api/requests/REQ-1001/actions", json={"action": "reject", "reviewer_role": "Security",
                                                                 "reason": "changed my mind"})
    assert closed.status_code == 409
    assert client.post("/api/requests/REQ-1001/actions",
                       json={"action": "approve", "reviewer_role": "Wizard"}).status_code == 400
    audit = client.get("/api/requests/REQ-1001").json()["audit"]
    assert [a["action"] for a in audit] == ["approve", "approve"]


def test_single_manager_cannot_approve_a_multi_approver_request(client):
    client.post("/api/requests/REQ-1005/analyze?arch=R")
    r = client.post("/api/requests/REQ-1005/actions", json={"action": "approve", "reviewer_role": "Manager",
                                                            "reason": "looks fine"})
    assert r.status_code == 422 and "not a required approver" in r.json()["detail"]
    r = client.post("/api/requests/REQ-1005/actions", json={"action": "approve", "reviewer_role": "Finance",
                                                            "reason": "budget exception granted"})
    assert r.json()["status"] == "Partially approved"


def test_override_requires_reason_and_blocks_require_exception(client):
    client.post("/api/requests/REQ-1006/analyze?arch=R")  # request_more_info
    no_reason = client.post("/api/requests/REQ-1006/actions", json={"action": "approve", "reviewer_role": "Security"})
    assert no_reason.status_code == 422 and "reason" in no_reason.json()["detail"]
    with_reason = client.post("/api/requests/REQ-1006/actions",
                              json={"action": "approve", "reviewer_role": "Security", "reason": "Exec sponsor"})
    assert with_reason.status_code == 201 and with_reason.json()["action"]["is_override"] == 1

    run = main.store().latest_run("REQ-1006")
    main.store().add_run("blocked", "REQ-1006", "R", run["decision"], [], None, {"blocks": [{"rule_id": "X"}]})
    blocked = client.post("/api/requests/REQ-1006/actions",
                          json={"action": "approve", "reviewer_role": "Privacy", "reason": "r"})
    assert blocked.status_code == 422 and "exception" in blocked.json()["detail"]
    exc = client.post("/api/requests/REQ-1006/actions", json={"action": "approve", "reviewer_role": "Privacy",
                                                              "reason": "r", "exception_reason": "Board waiver"})
    assert exc.json()["action"]["is_exception"] == 1


def test_eval_summary_endpoint(client):
    assert client.get("/api/eval/summary").status_code in (200, 404)


def test_ui_assets_keep_hidden_authoritative_and_default_to_shipped_architecture(client):
    css = client.get("/static/styles.css").text
    assert "[hidden] { display: none !important; }" in css  # grid/flex rules must not un-hide views or forms
    html = client.get("/").text
    assert '<option value="B" selected>' in html


def test_oversized_input_is_rejected(client):
    too_long = client.post("/api/requests", json={"requester_id": "E001", "product_name": "X", "vendor_name": "Y",
                                                  "business_justification": "a" * 4001})
    assert too_long.status_code == 422
    too_many = client.post("/api/requests", json={"requester_id": "E001", "product_name": "X", "vendor_name": "Y",
                                                  "requested_integrations": ["SSO"] * 21})
    assert too_many.status_code == 422


def test_structured_log_lines_are_json(caplog):
    import json
    import logging

    from src.obs import JsonFormatter

    record = logging.LogRecord("procurement", logging.INFO, __file__, 1, "analysis_completed", None, None)
    record.fields = {"run_id": "abc", "recommendation": "recommend_approve"}
    line = json.loads(JsonFormatter().format(record))
    assert line["event"] == "analysis_completed" and line["run_id"] == "abc"
