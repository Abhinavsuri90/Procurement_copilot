"""Mock vendor-risk service: the external API the `get_vendor_risk` tool calls over HTTP."""

from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import FastAPI, HTTPException

ROOT = Path(__file__).resolve().parents[1]


def _key(name: str) -> str:
    """Lookup key: case- and whitespace-insensitive, so ' signflow ' finds 'SignFlow'."""
    return " ".join(name.split()).casefold()


def load_risk_data() -> dict[str, dict]:
    data_dir = Path(os.getenv("DATA_DIR") or ROOT / "data")
    return json.loads((data_dir / "vendor_risk.json").read_text(encoding="utf-8"))


def create_app(data: dict[str, dict] | None = None) -> FastAPI:
    """Build the service. Tests and the eval harness pass their own data; uvicorn uses the data directory."""
    records = load_risk_data() if data is None else data
    index = {_key(name): (name, record) for name, record in records.items()}
    app = FastAPI(title="FDE Mock Vendor Risk API", version="1.1")

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "vendors": len(index)}

    # `:path` lets names containing "/" reach the handler; FastAPI has already percent-decoded the value once.
    @app.get("/vendor-risk/{vendor_name:path}")
    def vendor_risk(vendor_name: str) -> dict:
        hit = index.get(_key(vendor_name))
        if hit is None:
            raise HTTPException(status_code=404, detail=f"No vendor-risk record for '{vendor_name}'")
        name, record = hit
        if record.get("force_error"):
            raise HTTPException(status_code=503, detail=record.get("error_message", "Vendor-risk service unavailable"))
        return {"vendor_name": name, **record}

    return app


app = create_app()
