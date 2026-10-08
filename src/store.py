"""SQLite persistence: user-created requests, analysis runs (decision + full trace) and the human audit log."""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, architecture TEXT NOT NULL, created_at TEXT NOT NULL,
    decision TEXT NOT NULL, trace TEXT NOT NULL, raw TEXT, policy TEXT);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT NOT NULL, run_id TEXT, action TEXT NOT NULL,
    reviewer_role TEXT NOT NULL, reason TEXT, ai_recommendation TEXT, architecture TEXT, model TEXT,
    is_override INTEGER NOT NULL DEFAULT 0, is_exception INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS runs_by_request ON runs(request_id, created_at);
CREATE INDEX IF NOT EXISTS actions_by_request ON actions(request_id, id);
"""

ACTION_STATUS = {"approve": "Approved", "reject": "Rejected", "request_info": "Info requested",
                 "escalate": "Escalated"}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def _all(self, sql: str, params: tuple = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    # -- requests created in the UI
    def add_request(self, payload: dict[str, Any]) -> None:
        self._exec("INSERT INTO requests VALUES (?, ?, ?)", (payload["request_id"], json.dumps(payload), now()))

    def created_requests(self) -> dict[str, dict[str, Any]]:
        return {r["request_id"]: json.loads(r["payload"]) for r in self._all("SELECT * FROM requests ORDER BY created_at")}

    def next_request_id(self) -> str:
        count = self._all("SELECT COUNT(*) AS n FROM requests")[0]["n"]
        return f"NEW-{count + 1:04d}"

    # -- runs
    def add_run(self, run_id: str, request_id: str, architecture: str, decision: dict, trace: list, raw: Any,
                policy: dict) -> None:
        self._exec("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (run_id, request_id, architecture, now(), json.dumps(decision), json.dumps(trace, default=str),
                    json.dumps(raw, default=str), json.dumps(policy, default=str)))

    def run(self, run_id: str) -> dict[str, Any] | None:
        rows = self._all("SELECT * FROM runs WHERE run_id = ?", (run_id,))
        return _decode_run(rows[0]) if rows else None

    def latest_run(self, request_id: str) -> dict[str, Any] | None:
        rows = self._all("SELECT * FROM runs WHERE request_id = ? ORDER BY created_at DESC, rowid DESC LIMIT 1",
                         (request_id,))
        return _decode_run(rows[0]) if rows else None

    def runs_for(self, request_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT run_id, architecture, created_at, json_extract(decision, '$.recommendation') AS "
                         "recommendation FROM runs WHERE request_id = ? ORDER BY created_at, rowid", (request_id,))

    # -- audit log
    def add_action(self, **fields: Any) -> dict[str, Any]:
        cols = ["request_id", "run_id", "action", "reviewer_role", "reason", "ai_recommendation", "architecture",
                "model", "is_override", "is_exception"]
        values = tuple(fields.get(c) for c in cols) + (now(),)
        cur = self._exec(f"INSERT INTO actions ({', '.join(cols)}, created_at) VALUES ({', '.join('?' * len(values))})",
                         values)
        return self._all("SELECT * FROM actions WHERE id = ?", (cur.lastrowid,))[0]

    def actions_for(self, request_id: str) -> list[dict[str, Any]]:
        return self._all("SELECT * FROM actions WHERE request_id = ? ORDER BY id", (request_id,))

    def status(self, request_id: str) -> str:
        actions = self.actions_for(request_id)
        if actions:
            return ACTION_STATUS[actions[-1]["action"]]
        run = self.latest_run(request_id)
        if run is None:
            return "New"
        return "Awaiting human" if run["decision"]["human_handoff"]["required"] else "Analyzed"


def _decode_run(row: dict[str, Any]) -> dict[str, Any]:
    for key in ("decision", "trace", "raw", "policy"):
        row[key] = json.loads(row[key]) if row.get(key) else None
    return row
