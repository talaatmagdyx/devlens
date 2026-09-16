"""Run context: the record of what an investigation actually did.

Every provider call goes through :meth:`RunContext.tool`, which records a
:class:`ToolCall` and emits progress events *as the work happens*. That single
mechanism supplies four things that were previously missing or faked:

* a live progress stream, rather than a timeline replayed after completion;
* reproducibility — arguments, durations and result digests for every call;
* an audit trail of what a run touched;
* per-run cost accounting.

Arguments are redacted before they are stored.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from contextlib import asynccontextmanager
from typing import Any, Protocol
from uuid import uuid4

from devlens.agent.audit import redact
from devlens.domain import AgentEvent, ToolCall, digest


class Recorder(Protocol):
    """Sink for run events and tool calls."""

    def event(self, run_id: str, event: AgentEvent) -> None: ...
    def tool_call(self, call: ToolCall) -> None: ...


class NullRecorder:
    """Discards everything. Used by the CLI and by synchronous API calls."""

    def event(self, run_id: str, event: AgentEvent) -> None:
        return None

    def tool_call(self, call: ToolCall) -> None:
        return None


class RunContext:
    """Threaded through every workflow; the only place progress is produced."""

    def __init__(
        self,
        run_id: str | None = None,
        recorder: Recorder | None = None,
        *,
        capabilities: Iterator[str] | list[str] | None = None,
        model: tuple[str | None, str | None] = (None, None),
    ):
        self.run_id = run_id or str(uuid4())
        self.recorder = recorder or NullRecorder()
        self.capabilities = sorted(capabilities or [])
        self.model_provider, self.model_name = model
        self.events: list[AgentEvent] = []
        self.calls: list[ToolCall] = []
        self.started_at = time.time()

    # -- progress ---------------------------------------------------------- #

    def event(self, kind: str, message: str, **data: Any) -> AgentEvent:
        record = AgentEvent(
            ts=time.time(), kind=kind, message=message[:2000], data=redact(data)
        )
        self.events.append(record)
        self.recorder.event(self.run_id, record)
        return record

    # -- tool calls -------------------------------------------------------- #

    @asynccontextmanager
    async def tool(self, name: str, **arguments: Any):
        """Record one call. Emits ToolStarted before and ToolCompleted after."""
        call = ToolCall(
            id=str(uuid4()),
            run_id=self.run_id,
            tool=name,
            arguments=redact(arguments),
            started_at=time.time(),
        )
        self.calls.append(call)
        self.event("ToolStarted", f"{name}", tool=name, call_id=call.id, **arguments)
        box: dict[str, Any] = {}
        try:
            yield box
        except Exception as exc:
            call.finished_at = time.time()
            call.status = "denied" if type(exc).__name__ == "AccessDenied" else "error"
            call.error = f"{type(exc).__name__}: {exc}"[:500]
            self.recorder.tool_call(call)
            self.event(
                "ToolFailed",
                f"{name} failed: {type(exc).__name__}",
                tool=name,
                call_id=call.id,
            )
            raise
        call.finished_at = time.time()
        call.status = "ok"
        result = box.get("result")
        if result is not None:
            call.result_digest = digest(_summarise(result))
            call.result_bytes = _size(result)
        self.recorder.tool_call(call)
        self.event(
            "ToolCompleted",
            f"{name} ({call.duration_ms} ms)",
            tool=name,
            call_id=call.id,
            bytes=call.result_bytes,
        )

    def observation(self, message: str, **data: Any) -> None:
        self.event("ObservationCreated", message, **data)

    def hypothesis(self, message: str, **data: Any) -> None:
        self.event("HypothesisCreated", message, **data)

    def completed(self, message: str = "completed") -> None:
        self.event("RunCompleted", message)


def _summarise(result: Any) -> Any:
    if isinstance(result, (str, bytes)):
        return len(result)
    if isinstance(result, (list, tuple, set, dict)):
        return len(result)
    return str(type(result).__name__)


def _size(result: Any) -> int:
    if isinstance(result, bytes):
        return len(result)
    if isinstance(result, str):
        return len(result.encode("utf-8", errors="ignore"))
    if isinstance(result, (list, tuple, set, dict)):
        return len(result)
    return 0


def emitter(context: RunContext | None) -> Callable[..., Any]:
    """A no-op-safe event callback for code that may run without a context."""
    if context is None:
        return lambda *args, **kwargs: None
    return context.event
