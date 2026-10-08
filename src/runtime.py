"""Construction of per-run contexts (fresh state for every analysis)."""

from __future__ import annotations

import httpx

from src.config import Settings, get_settings
from src.data_access import Repository, default_repository
from src.schemas import PurchaseRequest
from src.tools.base import RunContext
from src.vendor_client import VendorClient


def make_context(request: PurchaseRequest, repo: Repository | None = None, settings: Settings | None = None,
                 fault: str | None = None, http: httpx.Client | None = None) -> RunContext:
    settings = settings or get_settings()
    vendor = VendorClient(settings.vendor_service_url, timeout_s=settings.vendor_timeout_s,
                          retries=settings.vendor_retries, http=http)
    return RunContext(request=request, repo=repo or default_repository(), vendor=vendor, settings=settings,
                      as_of=settings.as_of, fault=fault or settings.vendor_service_fault or None)
