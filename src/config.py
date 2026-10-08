"""Runtime configuration, read from the environment (`.env` is loaded by `src/__init__.py`)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FALLBACK_AS_OF = date(2026, 9, 30)


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def _float(name: str, default: float | None) -> float | None:
    raw = _env(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def policy_reference_date(data_dir: Path) -> date:
    """The policy document defines the evaluation reference date; date checks must not use the wall clock."""
    try:
        text = (data_dir / "procurement_policy.md").read_text(encoding="utf-8")
    except OSError:
        return FALLBACK_AS_OF
    match = re.search(r"reference date[^0-9]*(\d{4}-\d{2}-\d{2})", text, flags=re.IGNORECASE)
    return date.fromisoformat(match.group(1)) if match else FALLBACK_AS_OF


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    as_of: date
    llm_base_url: str
    llm_api_key: str
    llm_model: str
    llm_temperature: float
    llm_timeout_s: float
    llm_max_retries: int
    llm_mode: str  # live | record | replay
    cassette_dir: Path
    vendor_service_url: str
    vendor_service_fault: str  # "" | down | slow | flaky
    vendor_timeout_s: float
    vendor_retries: int
    app_port: int
    db_path: Path
    price_in_per_1m: float | None
    price_out_per_1m: float | None

    @property
    def llm_configured(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)


def get_settings() -> Settings:
    data_dir = Path(_env("DATA_DIR", str(ROOT / "data")))
    as_of_raw = _env("AS_OF_DATE")
    db_path = Path(_env("DB_PATH", "var/app.db"))
    return Settings(
        data_dir=data_dir,
        as_of=date.fromisoformat(as_of_raw) if as_of_raw else policy_reference_date(data_dir),
        llm_base_url=_env("LLM_BASE_URL", "https://api.openai.com/v1"),
        llm_api_key=_env("LLM_API_KEY"),
        llm_model=_env("LLM_MODEL"),
        llm_temperature=_float("LLM_TEMPERATURE", 0.0) or 0.0,
        llm_timeout_s=_float("LLM_TIMEOUT_S", 60.0) or 60.0,
        llm_max_retries=int(_float("LLM_MAX_RETRIES", 3) or 0),
        llm_mode=_env("LLM_MODE", "live").lower(),
        cassette_dir=Path(_env("CASSETTE_DIR", str(ROOT / "evals" / "cassettes"))),
        # VENDOR_RISK_BASE_URL is the starter pack's name for the same setting.
        vendor_service_url=_env("VENDOR_SERVICE_URL", _env("VENDOR_RISK_BASE_URL", "http://127.0.0.1:8001")).rstrip("/"),
        vendor_service_fault=_env("VENDOR_SERVICE_FAULT").lower(),
        vendor_timeout_s=_float("VENDOR_TIMEOUT_S", 3.0) or 3.0,
        vendor_retries=int(_float("VENDOR_RETRIES", 2) or 0),
        app_port=int(_float("APP_PORT", 8000) or 8000),
        db_path=db_path if db_path.is_absolute() else ROOT / db_path,
        price_in_per_1m=_float("PRICE_PER_1M_INPUT_TOKENS", None),
        price_out_per_1m=_float("PRICE_PER_1M_OUTPUT_TOKENS", None),
    )
