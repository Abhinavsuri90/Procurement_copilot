"""Typed repository (regression for docs/STARTER_FIXES.md: blank CSV cells became truthy NaN under pandas)."""

from src.data_access import Repository, norm


def test_blank_cells_are_none_not_nan():
    repo = Repository()
    assert repo.employee("E010").manager_id is None
    assert repo.vendor("NimbusAI").security_review_date is None


def test_department_head_walks_reporting_line_and_never_returns_requester():
    repo = Repository()
    assert repo.department_head("E003").employee_id == "E007"  # Sales IC -> Go To Market director
    assert repo.department_head("E008").employee_id == "E010"  # a director's head is their manager
    assert repo.department_head("E010") is None


def test_lookups_are_case_and_whitespace_insensitive():
    repo = Repository()
    assert repo.vendor("  signalwatch ").vendor_id == "V005"
    assert repo.budget("customer success").available_usd == 7000
    assert norm(" A  b ") == "a b"


def test_overlay_adds_and_replaces_records():
    repo = Repository(overlay={"vendors": [{"vendor_id": "V900", "vendor_name": "NewCo", "procurement_status": "New"}],
                               "employees": [{"employee_id": "E001", "name": "Renamed", "department": "Sales"}]})
    assert repo.vendor("NewCo").vendor_id == "V900"
    assert repo.employee("E001").department == "Sales"
    assert len(repo.employees) == 10
