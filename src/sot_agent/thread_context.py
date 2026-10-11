"""Session-scoped, awaitable context exchange between concurrent L2 threads."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Literal

from .diagnostics import DiagnosticLog

THREAD_CONTEXT_TOOL_NAME = "get_thread_context"

ThreadTerminalStatus = Literal["succeeded", "failed", "cancelled"]


@dataclass(frozen=True, slots=True)
class ThreadContextScope:
    session_id: str
    thread_number: int
    operation_id: str
    run_id: str


@dataclass(slots=True)
class ThreadContextEntry:
    session_id: str
    thread_number: int
    thread_id: str | None = None
    registered: asyncio.Event = field(default_factory=asyncio.Event)
    terminal: asyncio.Event = field(default_factory=asyncio.Event)
    status: ThreadTerminalStatus | None = None
    context: dict[str, Any] | None = None
    error: str | None = None


_ACTIVE_SCOPE: ContextVar[ThreadContextScope | None] = ContextVar(
    "sot_thread_context_scope",
    default=None,
)


class ThreadContextBroker:
    """Resolve visible thread numbers without serializing unrelated L2 runs."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 120,
        registration_timeout_seconds: float | None = None,
        diagnostics: DiagnosticLog | None = None,
    ) -> None:
        self.timeout_seconds = timeout_seconds
        self.registration_timeout_seconds = registration_timeout_seconds or timeout_seconds
        self.diagnostics = diagnostics
        self._entries: dict[tuple[str, int], ThreadContextEntry] = {}

    def register(self, session_id: str, thread_number: int, thread_id: str) -> None:
        key = (session_id, thread_number)
        entry = self._entries.setdefault(
            key,
            ThreadContextEntry(session_id=session_id, thread_number=thread_number),
        )
        if entry.thread_id is not None and entry.thread_id != thread_id:
            raise ValueError(
                f"Thread {thread_number} is already registered in this presentation session."
            )
        entry.thread_id = thread_id
        entry.registered.set()

    def finish(
        self,
        session_id: str,
        thread_number: int,
        *,
        status: ThreadTerminalStatus,
        context: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        entry = self._entries.get((session_id, thread_number))
        if entry is None or entry.terminal.is_set():
            return
        entry.status = status
        entry.context = context
        entry.error = error
        entry.terminal.set()

    @contextmanager
    def activate(
        self,
        *,
        session_id: str,
        thread_number: int,
        operation_id: str,
        run_id: str,
    ) -> Iterator[None]:
        token = _ACTIVE_SCOPE.set(
            ThreadContextScope(
                session_id=session_id,
                thread_number=thread_number,
                operation_id=operation_id,
                run_id=run_id,
            )
        )
        try:
            yield
        finally:
            _ACTIVE_SCOPE.reset(token)

    async def get_thread_context(self, thread_number: int) -> dict[str, Any]:
        """Return an earlier thread's context, waiting for its terminal state when necessary."""

        scope = _ACTIVE_SCOPE.get()
        if scope is None:
            return self._problem(
                "scope_unavailable",
                thread_number,
                "The current presentation thread scope is unavailable.",
            )
        if thread_number < 1:
            return self._problem(
                "invalid_reference",
                thread_number,
                "Thread numbers start at 1.",
            )
        if thread_number >= scope.thread_number:
            return self._problem(
                "invalid_reference",
                thread_number,
                "Only an earlier thread can be referenced; current and future threads "
                "are rejected.",
            )

        started = time.perf_counter()
        entry = self._entries.setdefault(
            (scope.session_id, thread_number),
            ThreadContextEntry(session_id=scope.session_id, thread_number=thread_number),
        )
        self._record(
            "INFO",
            "l2.context.wait.started",
            f"Thread {scope.thread_number} requested context from thread {thread_number}",
            scope,
            data={"referencedThreadNumber": thread_number, "registered": entry.registered.is_set()},
        )

        try:
            await asyncio.wait_for(
                entry.registered.wait(),
                timeout=min(self.registration_timeout_seconds, self.timeout_seconds),
            )
        except TimeoutError:
            return self._wait_problem(
                "not_found",
                thread_number,
                "The referenced thread does not exist in this presentation session.",
                scope,
                started,
                "l2.context.wait.not_found",
            )

        remaining = max(0.001, self.timeout_seconds - (time.perf_counter() - started))
        try:
            await asyncio.wait_for(entry.terminal.wait(), timeout=remaining)
        except TimeoutError:
            return self._wait_problem(
                "timeout",
                thread_number,
                "The referenced thread did not complete before the context wait timed out.",
                scope,
                started,
                "l2.context.wait.timeout",
            )

        waited_ms = round((time.perf_counter() - started) * 1_000, 1)
        if entry.status != "succeeded" or entry.context is None:
            message = entry.error or f"The referenced thread ended with status {entry.status}."
            self._record(
                "WARNING",
                "l2.context.wait.unavailable",
                message,
                scope,
                duration_ms=waited_ms,
                data={
                    "referencedThreadNumber": thread_number,
                    "referencedThreadId": entry.thread_id,
                    "referencedStatus": entry.status,
                },
            )
            return {
                "status": "unavailable",
                "thread_number": thread_number,
                "thread_id": entry.thread_id,
                "thread_status": entry.status,
                "waited_ms": waited_ms,
                "message": message,
            }

        self._record(
            "INFO",
            "l2.context.wait.resolved",
            f"Context from thread {thread_number} is available",
            scope,
            duration_ms=waited_ms,
            data={
                "referencedThreadNumber": thread_number,
                "referencedThreadId": entry.thread_id,
            },
        )
        return {
            "status": "succeeded",
            "thread_number": thread_number,
            "thread_id": entry.thread_id,
            "waited_ms": waited_ms,
            "context": self._with_basis(scope.session_id, entry.context),
        }

    def _with_basis(self, session_id: str, context: dict[str, Any]) -> dict[str, Any]:
        """Add the context of the thread this one built on, one hop back.

        L1 only links a request to the previous one, so "set the renewal on *their* agreement"
        after "show me their latest order" must reach the customer two threads back through it.
        """

        basis_number = context.get("refers_to")
        if not isinstance(basis_number, int):
            return context
        basis = self._entries.get((session_id, basis_number))
        if basis is None or basis.status != "succeeded" or basis.context is None:
            return context
        return {**context, "based_on": basis.context}

    @staticmethod
    def _problem(status: str, thread_number: int, message: str) -> dict[str, Any]:
        return {"status": status, "thread_number": thread_number, "message": message}

    def _wait_problem(
        self,
        status: str,
        thread_number: int,
        message: str,
        scope: ThreadContextScope,
        started: float,
        event: str,
    ) -> dict[str, Any]:
        waited_ms = round((time.perf_counter() - started) * 1_000, 1)
        self._record(
            "WARNING",
            event,
            message,
            scope,
            duration_ms=waited_ms,
            data={"referencedThreadNumber": thread_number},
        )
        return {
            "status": status,
            "thread_number": thread_number,
            "waited_ms": waited_ms,
            "message": message,
        }

    def _record(
        self,
        level: Literal["INFO", "WARNING"],
        event: str,
        message: str,
        scope: ThreadContextScope,
        *,
        duration_ms: float | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        if self.diagnostics is None:
            return
        self.diagnostics.record(
            level,
            "L2",
            event,
            message,
            operation_id=scope.operation_id,
            run_id=scope.run_id,
            duration_ms=duration_ms,
            data=data,
        )
