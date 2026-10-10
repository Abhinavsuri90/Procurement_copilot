"""Browser end-to-end tests: the real launcher, the real UI, no API key (starter requests replay recorded runs).

Run with `pytest -m e2e` (needs `pip install playwright` and a Chromium: `python -m playwright install chromium`,
or a local Google Chrome, which is used automatically if the bundled browser is missing).
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.e2e
sync_api = pytest.importorskip("playwright.sync_api")
expect = sync_api.expect
ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    app_port, vendor_port = _free_port(), _free_port()
    env = {**os.environ, "APP_PORT": str(app_port), "VENDOR_SERVICE_URL": f"http://127.0.0.1:{vendor_port}",
           "DB_PATH": str(tmp_path_factory.mktemp("e2e") / "app.db"), "LLM_API_KEY": "", "LLM_MODE": "live",
           "VENDOR_SERVICE_FAULT": ""}
    proc = subprocess.Popen([sys.executable, "start.py", "--no-install"], cwd=ROOT, env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    url = f"http://127.0.0.1:{app_port}"
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url + "/api/health", timeout=1) as r:
                if r.status == 200:
                    break
        except OSError:
            time.sleep(0.3)
    else:
        proc.terminate()
        pytest.fail("app did not start")
    yield url
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def browser() -> Iterator[object]:
    with sync_api.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except sync_api.Error:  # bundled browser not installed: use the local Google Chrome
            b = p.chromium.launch(channel="chrome")
        yield b
        b.close()


@pytest.fixture
def page(browser, base_url: str):
    context = browser.new_context(viewport={"width": 1440, "height": 1000})
    pg = context.new_page()
    errors: list[str] = []
    pg.on("pageerror", lambda exc: errors.append(str(exc)))
    pg.goto(base_url)
    expect(pg.locator(".queue-item[data-id^='REQ-']")).to_have_count(10)
    yield pg
    context.close()
    assert not errors, f"JavaScript errors: {errors}"


def run(pg, request_id: str, arch: str = "B") -> None:
    pg.click(f"button.queue-item[data-id='{request_id}']")
    expect(pg.locator("#req-title")).to_contain_text(request_id)
    pg.select_option("#arch", arch)
    pg.click("#run-btn")
    expect(pg.locator("#live-title")).to_have_text("Run complete", timeout=60_000)
    expect(pg.locator("#decision-body .rec")).to_be_visible()


def test_header_shows_no_key_mode_and_healthy_vendor_service(page):
    expect(page.locator("#model-pill")).to_contain_text("no API key")
    expect(page.locator("#vendor-dot")).to_have_class("dot ok")
    expect(page.locator("#arch")).to_have_value("B")


def test_happy_path_live_progress_evidence_and_single_approver(page):
    run(page, "REQ-1001")
    expect(page.locator("#decision-body .rec")).to_have_text("Recommend approval")
    expect(page.locator("#live-log")).to_contain_text("get_vendor_risk")
    expect(page.locator("#live-log")).to_contain_text("Decision ready for human review")
    expect(page.locator("#workflow li").last).to_have_class("active")
    expect(page.locator(".banner.info")).to_contain_text("Replayed")
    page.locator("#decision-body .evidence button[data-call]").first.click()
    expect(page.locator("#raw-dialog")).to_be_visible()
    expect(page.locator("#raw-body")).to_contain_text('"call_id"')
    page.click("#raw-close")
    page.select_option("#reviewer-role", "Manager")
    page.click("#human-form [data-action='approve']")
    expect(page.locator("#req-status")).to_have_text("Approved")
    expect(page.locator("#human-form [data-action='approve']")).to_be_disabled()
    page.click(".subtab[data-tab='audit']")
    expect(page.locator("#tab-audit")).to_contain_text("Manager")


def test_escalation_needs_required_approvers_and_reasons(page):
    run(page, "REQ-1007")
    expect(page.locator("#decision-body .rec")).to_have_text("Escalate to human")
    expect(page.locator("#decision-body")).to_contain_text("conflicting_vendor_evidence")
    page.select_option("#reviewer-role", "Manager")
    page.click("#human-form [data-action='approve']")
    expect(page.locator("#form-msg")).to_contain_text("not a required approver")
    page.select_option("#reviewer-role", "Department Head")
    page.click("#human-form [data-action='approve']")
    expect(page.locator("#form-msg")).to_contain_text("reason is required")
    page.fill("#reason", "Security will re-assess before renewal")
    page.click("#human-form [data-action='approve']")
    expect(page.locator("#req-status")).to_have_text("Partially approved")
    expect(page.locator("#signoffs")).to_contain_text("1/4")


def test_injection_and_missing_information(page):
    run(page, "REQ-1006")
    expect(page.locator("#decision-body .rec")).to_have_text("Request more info")
    expect(page.locator("#decision-body")).to_contain_text("prompt_injection_detected")
    expect(page.locator("#decision-body")).to_contain_text("annual_cost_usd")
    expect(page.locator("#copy-q")).to_be_visible()


def test_vendor_outage_switch_degrades_to_escalation(page):
    page.check("#outage")
    run(page, "REQ-1010")
    expect(page.locator("#decision-body .rec")).to_have_text("Escalate to human")
    expect(page.locator("#decision-body")).to_contain_text("Vendor service unavailable")
    expect(page.locator("#decision-body")).to_contain_text("AI unavailable")


def test_new_request_without_key_gets_deterministic_checks(page):
    page.click("#new-btn")
    page.fill("#new-form [name=requester_id]", "E002")
    page.fill("#new-form [name=product_name]", "LogLens Search")
    page.fill("#new-form [name=vendor_name]", "TaskFlow")
    page.fill("#new-form [name=annual_cost_usd]", "900")
    page.fill("#new-form [name=user_count]", "5")
    page.select_option("#new-form [name=data_access_level]", "internal_documents")
    page.fill("#new-form [name=business_justification]", "Search across build logs.")
    page.click("#create-btn")
    expect(page.locator("#req-title")).to_contain_text("NEW-")
    page.click("#run-btn")
    expect(page.locator("#live-title")).to_have_text("Run complete", timeout=60_000)
    expect(page.locator("#decision-body")).to_contain_text("AI unavailable")


def test_trace_tab_lists_model_and_tool_calls(page):
    run(page, "REQ-1003", arch="A")
    page.click(".subtab[data-tab='trace']")
    expect(page.locator("#tab-trace")).to_contain_text("get_requester_profile")
    expect(page.locator("#tab-trace")).to_contain_text("LLM")


def test_evaluation_view_and_deep_link(page, base_url):
    page.click("#tab-eval")
    expect(page.locator("#view-eval")).to_be_visible()
    expect(page.locator("#view-review")).to_be_hidden()
    expect(page.locator("#eval-body")).to_contain_text("Recommendation accuracy (final decision)")
    page.goto(base_url + "/#REQ-1004")
    expect(page.locator("#req-title")).to_contain_text("REQ-1004")
