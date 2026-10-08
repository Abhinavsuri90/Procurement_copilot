"""HTTP client for the vendor-risk service.

The starter client raised on any non-2xx response and had no retries, so one outage crashed the caller.
This one never raises: it returns a `VendorLookup` whose status is ok / not_found / unavailable, retries
timeouts, connection errors, 429 and 5xx with exponential backoff, and supports fault injection for demos
and evaluation (`down`, `slow`, `flaky`) without changing the normal code path.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import quote

import httpx

FAULTS = ("down", "slow", "flaky")


@dataclass
class VendorLookup:
    status: Literal["ok", "not_found", "unavailable"]
    record: dict[str, Any] | None = None
    error: str | None = None
    http_status: int | None = None
    attempts: int = 0
    attempt_log: list[str] = field(default_factory=list)


class VendorClient:
    def __init__(
        self,
        base_url: str,
        timeout_s: float = 3.0,
        retries: int = 2,
        http: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self.retries = retries
        self._http = http
        self._sleep = sleep

    def _client(self) -> httpx.Client:
        if self._http is None:
            self._http = httpx.Client(base_url=self.base_url, timeout=self.timeout_s)
        return self._http

    def _inject(self, fault: str | None, attempt: int) -> None:
        """Simulated failures, raised before any network I/O."""
        if fault == "down":
            raise httpx.ConnectError("simulated fault: vendor service down")
        if fault == "slow":
            self._sleep(self.timeout_s)
            raise httpx.ReadTimeout(f"simulated fault: no response within {self.timeout_s:.1f}s")
        if fault == "flaky" and attempt == 1:
            raise httpx.ConnectError("simulated fault: transient connection reset")

    def get_vendor_risk(self, vendor_name: str, fault: str | None = None) -> VendorLookup:
        path = f"/vendor-risk/{quote(vendor_name.strip(), safe='')}"
        log: list[str] = []
        last_error = "unknown error"
        last_status: int | None = None
        attempts = self.retries + 1
        for attempt in range(1, attempts + 1):
            try:
                self._inject(fault if fault in FAULTS else None, attempt)
                response = self._client().get(path, timeout=self.timeout_s)
                last_status = response.status_code
                if response.status_code == 200:
                    log.append(f"attempt {attempt}: 200")
                    return VendorLookup("ok", record=response.json(), http_status=200, attempts=attempt, attempt_log=log)
                if response.status_code == 404:
                    log.append(f"attempt {attempt}: 404")
                    return VendorLookup("not_found", error=_detail(response), http_status=404, attempts=attempt,
                                        attempt_log=log)
                last_error = f"HTTP {response.status_code}: {_detail(response)}"
                if response.status_code != 429 and response.status_code < 500:
                    log.append(f"attempt {attempt}: {last_error} (not retryable)")
                    break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                last_status = None
            except ValueError as exc:  # malformed JSON body
                last_error = f"invalid response body: {exc}"
            log.append(f"attempt {attempt}: {last_error}")
            if attempt < attempts:
                self._sleep(0.25 * 2 ** (attempt - 1) + random.uniform(0, 0.1))
        return VendorLookup("unavailable", error=last_error, http_status=last_status, attempts=len(log), attempt_log=log)

    def health(self) -> bool:
        try:
            return self._client().get("/health", timeout=1.0).status_code == 200
        except httpx.HTTPError:
            return False


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
        return str(body.get("detail", body)) if isinstance(body, dict) else str(body)
    except ValueError:
        return response.text[:200]
