"""Per-run trace: every LLM and tool call, with sequential IDs so evidence can cite them and prompts stay
byte-stable across runs (no timestamps or random IDs reach the model)."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class ToolError(BaseModel):
    code: str
    message: str
    retryable: bool = False


class ToolResult(BaseModel):
    """The envelope every tool returns. Failures are data, never exceptions."""

    call_id: str
    tool: str
    args: dict[str, Any]
    ok: bool
    data: Any = None
    error: ToolError | None = None
    source: str
    latency_ms: float = 0.0
    caller: str = "agent"

    def for_llm(self) -> dict[str, Any]:
        # latency is excluded: it varies run to run and would break record/replay request hashes.
        return {"call_id": self.call_id, "tool": self.tool, "ok": self.ok, "data": self.data,
                "error": self.error.model_dump() if self.error else None, "source": self.source}


class LLMCall(BaseModel):
    call_id: str
    agent: str
    turn: int
    latency_ms: float
    tokens_in: int = 0
    tokens_out: int = 0
    ok: bool = True
    error: str | None = None
    tool_calls: list[str] = Field(default_factory=list)
    replayed: bool = False


class TraceEvent(BaseModel):
    kind: Literal["llm", "tool", "note"]
    payload: dict[str, Any]


class Trace:
    def __init__(self) -> None:
        self.events: list[TraceEvent] = []
        self.tool_results: dict[str, ToolResult] = {}
        self.llm_calls: list[LLMCall] = []
        self._tool_seq = 0
        self._llm_seq = 0

    def next_tool_id(self) -> str:
        self._tool_seq += 1
        return f"c{self._tool_seq}"

    def next_llm_id(self) -> str:
        self._llm_seq += 1
        return f"llm{self._llm_seq}"

    def add_tool(self, result: ToolResult) -> None:
        self.tool_results[result.call_id] = result
        self.events.append(TraceEvent(kind="tool", payload=result.model_dump()))

    def add_llm(self, call: LLMCall) -> None:
        self.llm_calls.append(call)
        self.events.append(TraceEvent(kind="llm", payload=call.model_dump()))

    def note(self, message: str, **extra: Any) -> None:
        self.events.append(TraceEvent(kind="note", payload={"message": message, **extra}))

    def find(self, tool: str, args: dict[str, Any]) -> ToolResult | None:
        """Most recent recorded call of `tool` with exactly these arguments."""
        for result in reversed(list(self.tool_results.values())):
            if result.tool == tool and result.args == args:
                return result
        return None

    # -- telemetry
    @property
    def tool_call_count(self) -> int:
        return len(self.tool_results)

    @property
    def tool_names(self) -> list[str]:
        return [r.tool for r in self.tool_results.values()]

    @property
    def tokens(self) -> tuple[int, int]:
        return sum(c.tokens_in for c in self.llm_calls), sum(c.tokens_out for c in self.llm_calls)

    def dump(self) -> list[dict[str, Any]]:
        return [e.model_dump() for e in self.events]
