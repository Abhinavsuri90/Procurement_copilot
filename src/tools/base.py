"""Tool registry: typed arguments, a uniform result envelope, and tracing.

Each tool is a plain function `(ctx, args) -> data`. `execute` validates arguments with the tool's Pydantic
model, times the call, converts any failure into an error envelope, and records the result in the run trace.
Nothing a tool does can raise into the agent loop.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from pydantic import BaseModel, ValidationError

from src.config import Settings
from src.data_access import Repository
from src.schemas import PurchaseRequest
from src.trace import ToolError, ToolResult, Trace
from src.vendor_client import VendorClient


@dataclass
class RunContext:
    """Everything one analysis run needs. Built fresh per run, so runs never share state."""

    request: PurchaseRequest
    repo: Repository
    vendor: VendorClient
    settings: Settings
    as_of: date
    trace: Trace = field(default_factory=Trace)
    fault: str | None = None  # vendor-service fault injected for this run (demo switch / eval)


class ToolFailure(Exception):
    """Raised inside a tool to return a structured error; `data` may carry partial results."""

    def __init__(self, code: str, message: str, retryable: bool = False, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.retryable, self.data = code, message, retryable, data


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    args_model: type[BaseModel]
    parameters: dict[str, Any]  # JSON Schema shown to the model
    fn: Callable[[RunContext, BaseModel], Any]
    source: str
    deterministic: bool = True


REGISTRY: dict[str, ToolSpec] = {}


def register(name: str, description: str, args_model: type[BaseModel], parameters: dict[str, Any], source: str):
    def wrap(fn: Callable[[RunContext, Any], Any]) -> Callable[[RunContext, Any], Any]:
        REGISTRY[name] = ToolSpec(name, description, args_model, parameters, fn, source)
        return fn

    return wrap


def normalize_args(name: str, raw_args: dict[str, Any]) -> dict[str, Any]:
    """Validated, canonical form of a tool's arguments (used to match recorded calls)."""
    return REGISTRY[name].args_model.model_validate(raw_args).model_dump(exclude_none=True)


def execute(ctx: RunContext, name: str, raw_args: dict[str, Any] | None, caller: str = "agent") -> ToolResult:
    call_id = ctx.trace.next_tool_id()
    start = time.perf_counter()
    spec = REGISTRY.get(name)
    args: dict[str, Any] = dict(raw_args or {})
    try:
        if spec is None:
            raise ToolFailure("UNKNOWN_TOOL", f"No tool named '{name}'. Available: {', '.join(sorted(REGISTRY))}")
        try:
            parsed = spec.args_model.model_validate(args)
        except ValidationError as exc:
            raise ToolFailure("INVALID_ARGUMENTS", _short_errors(exc)) from exc
        args = parsed.model_dump(exclude_none=True)
        data = spec.fn(ctx, parsed)
        result = ToolResult(call_id=call_id, tool=name, args=args, ok=True, data=data, source=spec.source, caller=caller)
    except ToolFailure as exc:
        result = ToolResult(call_id=call_id, tool=name, args=args, ok=False, data=exc.data,
                            error=ToolError(code=exc.code, message=exc.message, retryable=exc.retryable),
                            source=spec.source if spec else "registry", caller=caller)
    except Exception as exc:  # a bug in a tool must degrade, not crash the run
        result = ToolResult(call_id=call_id, tool=name, args=args, ok=False,
                            error=ToolError(code="TOOL_ERROR", message=f"{type(exc).__name__}: {exc}"),
                            source=spec.source if spec else "registry", caller=caller)
    result.latency_ms = round((time.perf_counter() - start) * 1000, 2)
    ctx.trace.add_tool(result)
    return result


def _short_errors(exc: ValidationError) -> str:
    return "; ".join(f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()[:5])


def llm_tool_specs(names: list[str]) -> list[dict[str, Any]]:
    """OpenAI-style function definitions for the named tools."""
    return [
        {"type": "function",
         "function": {"name": REGISTRY[n].name, "description": REGISTRY[n].description,
                      "parameters": REGISTRY[n].parameters}}
        for n in names
    ]
