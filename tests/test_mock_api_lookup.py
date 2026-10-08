"""Regression tests for the mock vendor-risk service lookup fixes (docs/STARTER_FIXES.md #2)."""

from urllib.parse import quote

from fastapi.testclient import TestClient

from mock_api.app import create_app

DATA = {
    "SignFlow": {"security_review_status": "approved"},
    "Acme/Corp": {"security_review_status": "approved"},
    "100% Tools": {"security_review_status": "approved"},
    "Down Inc": {"force_error": True, "error_message": "boom"},
}
client = TestClient(create_app(DATA))


def get(name: str):
    return client.get("/vendor-risk/" + quote(name, safe=""))


def test_lookup_ignores_case_and_surrounding_whitespace():
    r = get("  signflow ")
    assert r.status_code == 200
    assert r.json()["vendor_name"] == "SignFlow"


def test_names_with_slash_and_percent_resolve():
    assert get("Acme/Corp").status_code == 200
    assert get("100% Tools").status_code == 200


def test_unknown_vendor_is_404_and_forced_error_is_503():
    assert get("Nobody").status_code == 404
    assert get("Down Inc").status_code == 503
