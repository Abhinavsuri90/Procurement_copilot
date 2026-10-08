"""Shared fixtures. Everything runs offline: the mock vendor service is mounted in-process."""

from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("LLM_API_KEY", "")
os.environ["LLM_MODE"] = "live"
os.environ["DB_PATH"] = "var/test.db"

from mock_api.app import create_app  # noqa: E402
from src.data_access import Repository  # noqa: E402
from src.runtime import make_context  # noqa: E402
from src.schemas import PurchaseRequest  # noqa: E402

BASE_REQUEST = {
    "request_id": "T-1", "requester_id": "E004", "product_name": "SignFlow Add-on", "vendor_name": "SignFlow",
    "category": "E-signature", "annual_cost_usd": 800, "user_count": 3,
    "business_justification": "Finance needs three additional signing identities.",
    "data_access_level": "internal_documents", "requested_integrations": [], "urgency": "normal",
}


@pytest.fixture
def make_ctx():
    """Build a run context for a request (dict overrides on BASE_REQUEST), optional data overlay and fault."""

    def build(overrides: dict | None = None, overlay: dict | None = None, fault: str | None = None,
              request_id: str | None = None):
        repo = Repository(overlay=overlay)
        raw = repo.requests[request_id] if request_id else {**BASE_REQUEST, **(overrides or {})}
        http = TestClient(create_app(repo.vendor_risk))
        ctx = make_context(PurchaseRequest.from_raw(raw), repo=repo, fault=fault, http=http)
        ctx.vendor._sleep = lambda s: None  # no real backoff in tests
        ctx.vendor.timeout_s = 0.01
        return ctx

    return build
