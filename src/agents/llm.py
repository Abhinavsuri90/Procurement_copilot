"""LLM access: one OpenAI-compatible backend, plus record/replay so evaluations are reproducible offline.

An `LLMSession` is one run's view of the model. It counts calls into the run trace, and in `record` mode saves
each exchange (request hash, response, latency, usage - never headers or keys) to a cassette; in `replay` mode it
serves the recorded responses back and fails loudly if the request no longer hashes the same (code drifted).
"""

from __future__ import annotations

import copy
import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from src.config import Settings
from src.trace import LLMCall, Trace

MAX_TOKENS = 8192


class LLMError(Exception):
    """The model could not be reached or returned something unusable (after retries)."""


class CassetteMismatch(LLMError):
    """Replay request differs from the recorded one - prompts, tools or data changed since recording."""


@dataclass
class LLMResponse:
    content: str | None
    tool_calls: list[dict[str, str]]  # [{"name": ..., "arguments": "<json string>"}]
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    finish_reason: str | None = None
    replayed: bool = False

    def to_json(self) -> dict[str, Any]:
        return {"content": self.content, "tool_calls": self.tool_calls, "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out, "finish_reason": self.finish_reason}


class Backend(Protocol):
    model: str

    def complete(self, body: dict[str, Any]) -> LLMResponse: ...


def build_body(model: str, messages: list[dict], tools: list[dict], temperature: float,
               tool_choice: str = "auto") -> dict[str, Any]:
    """Only widely supported parameters, so any OpenAI-compatible provider accepts the request."""
    return {"model": model, "messages": messages, "tools": tools, "tool_choice": tool_choice,
            "temperature": temperature, "max_tokens": MAX_TOKENS}


def body_hash(body: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(body, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class OpenAICompatBackend:
    """Chat Completions with function calling; retries 429/5xx/timeouts with exponential backoff."""

    def __init__(self, settings: Settings):
        from openai import OpenAI  # imported lazily so the app starts without the SDK configured

        self.model = settings.llm_model
        self.max_retries = settings.llm_max_retries
        self._client = OpenAI(base_url=settings.llm_base_url, api_key=settings.llm_api_key,
                              timeout=settings.llm_timeout_s, max_retries=0)

    def complete(self, body: dict[str, Any]) -> LLMResponse:
        import openai

        last: Exception | None = None
        for attempt in range(self.max_retries + 1):
            start = time.perf_counter()
            try:
                resp = self._client.chat.completions.create(**body)
                if not resp.choices:
                    raise LLMError(f"empty response: {getattr(resp, 'error', None) or resp}")
                return _normalize(resp, (time.perf_counter() - start) * 1000)
            except (openai.RateLimitError, openai.APIConnectionError, openai.APITimeoutError,
                    openai.InternalServerError) as exc:
                last = exc
            except openai.APIStatusError as exc:
                if exc.status_code < 500 and exc.status_code != 429:
                    raise LLMError(f"HTTP {exc.status_code}: {exc.message}") from exc
                last = exc
            except LLMError as exc:  # provider returned 200 with no choices (seen under load); retry
                last = exc
            if attempt < self.max_retries:
                time.sleep(min(20.0, 1.5 * 2 ** attempt) + random.uniform(0, 0.5))
        raise LLMError(f"model unavailable after {self.max_retries + 1} attempts: {type(last).__name__}: {last}")


def _normalize(resp: Any, latency_ms: float) -> LLMResponse:
    choice = resp.choices[0]
    msg = choice.message
    calls = [{"name": tc.function.name, "arguments": tc.function.arguments or "{}"}
             for tc in (msg.tool_calls or []) if getattr(tc, "function", None)]
    usage = getattr(resp, "usage", None)
    return LLMResponse(content=msg.content, tool_calls=calls,
                       tokens_in=getattr(usage, "prompt_tokens", 0) or 0,
                       tokens_out=getattr(usage, "completion_tokens", 0) or 0,
                       latency_ms=round(latency_ms, 1), finish_reason=choice.finish_reason)


@dataclass
class Cassette:
    """Recorded exchanges for one (architecture, case, run)."""

    path: Path
    calls: list[dict[str, Any]] = field(default_factory=list)
    run_latency_ms: float | None = None
    final_request: dict[str, Any] | None = None
    model: str | None = None
    cursor: int = 0

    @classmethod
    def for_key(cls, root: Path, architecture: str, case_id: str, run: int) -> Cassette:
        return cls(root / architecture / case_id / f"run{run}.json")

    def exists(self) -> bool:
        return self.path.is_file()

    def load(self) -> Cassette:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.calls, self.run_latency_ms = data["calls"], data.get("run_latency_ms")
        self.final_request, self.model = data.get("final_request"), data.get("model")
        return self

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"model": self.model, "run_latency_ms": self.run_latency_ms, "calls": self.calls,
                   "final_request": self.final_request}
        self.path.write_text(json.dumps(payload, indent=1, ensure_ascii=False), encoding="utf-8")


class LLMSession:
    """Per-run model access with call accounting and optional record/replay."""

    def __init__(self, backend: Backend | None, model: str, temperature: float, trace: Trace,
                 mode: str = "live", cassette: Cassette | None = None):
        if mode == "replay" and cassette is None:
            raise ValueError("replay mode needs a cassette")
        self.backend, self.model, self.temperature = backend, model, temperature
        self.trace, self.mode, self.cassette = trace, mode, cassette

    @property
    def replaying(self) -> bool:
        return self.mode == "replay"

    def chat(self, messages: list[dict], tools: list[dict], *, agent: str, turn: int) -> LLMResponse:
        body = build_body(self.model, messages, tools, self.temperature)
        digest = body_hash(body)
        call_id = self.trace.next_llm_id()
        self.trace.emit("llm_start", call_id=call_id, agent=agent, turn=turn, replay=self.replaying)
        try:
            if self.replaying:
                resp = self._replay(digest)
            else:
                if self.backend is None:
                    raise LLMError("no LLM backend configured")
                resp = self.backend.complete(body)
                if self.mode == "record" and self.cassette is not None:
                    self.cassette.model = self.model
                    self.cassette.calls.append({"hash": digest, "agent": agent, "turn": turn,
                                                "latency_ms": resp.latency_ms, "response": resp.to_json()})
                    self.cassette.final_request = body
        except LLMError as exc:
            if self.mode == "record" and self.cassette is not None and not isinstance(exc, CassetteMismatch):
                self.cassette.calls.append({"hash": digest, "agent": agent, "turn": turn, "error": str(exc)})
            self.trace.add_llm(LLMCall(call_id=call_id, agent=agent, turn=turn, latency_ms=0, ok=False,
                                       error=str(exc), replayed=self.replaying))
            raise
        self.trace.add_llm(LLMCall(call_id=call_id, agent=agent, turn=turn, latency_ms=resp.latency_ms,
                                   tokens_in=resp.tokens_in, tokens_out=resp.tokens_out, replayed=resp.replayed,
                                   tool_calls=[c["name"] for c in resp.tool_calls]))
        return resp

    def _replay(self, digest: str) -> LLMResponse:
        assert self.cassette is not None
        if self.cassette.cursor >= len(self.cassette.calls):
            raise CassetteMismatch(f"cassette {self.cassette.path.name} has no call #{self.cassette.cursor + 1}")
        entry = self.cassette.calls[self.cassette.cursor]
        self.cassette.cursor += 1
        if entry["hash"] != digest:
            raise CassetteMismatch(f"request hash mismatch at call #{self.cassette.cursor} "
                                   f"({self.cassette.path}) - prompts, tools or data changed since recording")
        if "error" in entry:  # the recorded run failed here; fail the same way
            raise LLMError(entry["error"])
        r = entry["response"]
        return LLMResponse(content=r["content"], tool_calls=r["tool_calls"], tokens_in=r["tokens_in"],
                           tokens_out=r["tokens_out"], latency_ms=entry["latency_ms"],
                           finish_reason=r.get("finish_reason"), replayed=True)


class ScriptedBackend:
    """Deterministic fake model for tests: returns queued responses (or calls a function of the request)."""

    def __init__(self, script: list[LLMResponse] | Any, model: str = "scripted-fake"):
        self.model = model
        self.script = script
        self.requests: list[dict[str, Any]] = []

    def complete(self, body: dict[str, Any]) -> LLMResponse:
        self.requests.append(copy.deepcopy(body))  # the loop keeps appending to the same message list
        if callable(self.script):
            return self.script(body)
        if not self.script:
            raise LLMError("script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item
