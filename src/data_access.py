"""Read-only, typed access to the starter data.

Replaces the starter's pandas loaders: pandas turns blank cells into NaN, which is truthy, so checks such as
`if row["manager_id"]:` silently passed for employees without a manager. Here blanks become None.
An optional overlay (used by eval fixtures and tests) adds or replaces records without touching the files.
"""

from __future__ import annotations

import csv
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from src.config import get_settings
from src.schemas import PurchaseRequest

SENIOR_LEVELS = ("director", "vp", "svp", "evp", "chief", "head")


class Employee(BaseModel):
    employee_id: str
    name: str
    department: str | None = None
    manager_id: str | None = None
    level: str | None = None
    country: str | None = None


class DepartmentBudget(BaseModel):
    department: str
    annual_software_budget_usd: float | None = None
    committed_usd: float | None = None
    available_usd: float | None = None

    @property
    def record_id(self) -> str:
        return f"BUDGET:{self.department}"


class CatalogItem(BaseModel):
    software_id: str
    product_name: str
    category: str | None = None
    vendor_name: str | None = None
    status: str | None = None
    annual_cost_usd: float | None = None
    licensed_seats: int | None = None
    scope: str | None = None
    notes: str | None = None


class VendorRecord(BaseModel):
    vendor_id: str
    vendor_name: str
    procurement_status: str | None = None
    security_status: str | None = None
    security_review_date: str | None = None
    legal_terms_status: str | None = None
    notes: str | None = None


class Purchase(BaseModel):
    purchase_id: str
    purchase_date: str | None = None
    department: str | None = None
    vendor_name: str | None = None
    product_name: str | None = None
    annual_amount_usd: float | None = None
    status: str | None = None
    notes: str | None = None


def norm(text: str | None) -> str:
    """Comparison key for names: case- and whitespace-insensitive."""
    return " ".join((text or "").split()).casefold()


def _clean(row: dict[str, Any]) -> dict[str, Any]:
    return {k.strip(): (v.strip() or None) if isinstance(v, str) else v for k, v in row.items() if k}


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8-sig", newline="") as f:
        return [_clean(r) for r in csv.DictReader(f)]


def _merge(base: list[dict], extra: list[dict] | None, key: str) -> list[dict]:
    if not extra:
        return base
    by_key = {norm(str(r.get(key))): r for r in base}
    for row in extra:
        by_key[norm(str(row.get(key)))] = row
    return list(by_key.values())


class Repository:
    """All business data the tools read. Construction parses everything once; lookups are dict-based."""

    def __init__(self, data_dir: Path | None = None, overlay: dict[str, Any] | None = None):
        self.data_dir = Path(data_dir or get_settings().data_dir)
        overlay = overlay or {}
        d = self.data_dir
        self.employees = {
            e.employee_id: e
            for e in (Employee(**r) for r in _merge(_read_csv(d / "employees.csv"), overlay.get("employees"), "employee_id"))
        }
        self.budgets = {
            norm(b.department): b
            for b in (DepartmentBudget(**r) for r in
                      _merge(_read_csv(d / "department_budgets.csv"), overlay.get("department_budgets"), "department"))
        }
        self.catalog = [CatalogItem(**r) for r in
                        _merge(_read_csv(d / "software_catalog.csv"), overlay.get("software_catalog"), "software_id")]
        self.vendors = {
            norm(v.vendor_name): v
            for v in (VendorRecord(**r) for r in _merge(_read_csv(d / "vendors.csv"), overlay.get("vendors"), "vendor_name"))
        }
        self.purchases = [Purchase(**r) for r in
                          _merge(_read_csv(d / "purchase_history.csv"), overlay.get("purchase_history"), "purchase_id")]
        raw_requests = json.loads((d / "requests.json").read_text(encoding="utf-8"))
        self.requests = {r["request_id"]: r for r in _merge(raw_requests, overlay.get("requests"), "request_id")}
        self.vendor_risk: dict[str, dict] = {
            **json.loads((d / "vendor_risk.json").read_text(encoding="utf-8")),
            **overlay.get("vendor_risk", {}),
        }

    # -- lookups
    def employee(self, employee_id: str | None) -> Employee | None:
        return self.employees.get((employee_id or "").strip())

    def budget(self, department: str | None) -> DepartmentBudget | None:
        return self.budgets.get(norm(department))

    def vendor(self, vendor_name: str | None) -> VendorRecord | None:
        return self.vendors.get(norm(vendor_name))

    def department_head(self, employee_id: str) -> Employee | None:
        """Nearest Director-or-above in the reporting line, excluding the requester (no self-approval)."""
        seen: set[str] = set()
        current = self.employee(employee_id)
        while current and current.manager_id and current.manager_id not in seen:
            seen.add(current.manager_id)
            current = self.employee(current.manager_id)
            if current and (current.level or "").casefold().startswith(SENIOR_LEVELS):
                return current
        return None

    def get_request(self, request_id: str) -> PurchaseRequest:
        if request_id not in self.requests:
            raise KeyError(f"Unknown request_id: {request_id}")
        return PurchaseRequest.from_raw(self.requests[request_id])

    def policy_text(self) -> str:
        return (self.data_dir / "procurement_policy.md").read_text(encoding="utf-8")


@lru_cache(maxsize=4)
def _default_repository(data_dir: str) -> Repository:
    return Repository(Path(data_dir))


def default_repository() -> Repository:
    """Repository for the configured data directory, parsed once per process."""
    return _default_repository(str(get_settings().data_dir))


# Starter-pack helpers kept for compatibility with existing scripts.
def load_requests() -> list[dict]:
    return list(default_repository().requests.values())


def get_request(request_id: str) -> dict:
    return default_repository().requests[request_id]


def load_policy_text() -> str:
    return default_repository().policy_text()
