"""AG-UI adapter for flights mode: one run per cut segment, launched by L3 on ``segment_cut``.

L3 forwards the segment as ``forwardedProps.sotSegment``. Tool calls, questions and the answer
stream back as AG-UI events; a question keeps the run open until it is answered by voice
(routed by L1) or on the flight card (``POST /api/flights/answer``).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any, cast

from ag_ui.core import (
    BaseEvent,
    MessagesSnapshotEvent,
    RunErrorEvent,
    RunFinishedEvent,
    RunFinishedSuccessOutcome,
    RunStartedEvent,
    StateSnapshotEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from agent_framework_ag_ui import AgentFrameworkWorkflow

from .diagnostics import DiagnosticLevel, DiagnosticLog, PayloadKind
from .flights import (
    FlightFinished,
    FlightRuntime,
    QuestionAnswered,
    QuestionAsked,
    TextDelta,
    ToolFinished,
    ToolStarted,
)


@dataclass(slots=True)
class FlightRecord:
    thread_id: str
    segment: dict[str, Any]
    session_id: str
    model: str
    status: str = "running"
    phase: str = "planning"
    question: str | None = None
    tool_calls: int = 0
    messages: list[dict[str, Any]] = field(default_factory=list)

    def state(self) -> dict[str, Any]:
        return {
            "sot": {
                "schemaVersion": 2,
                "segment": self.segment,
                "execution": {
                    "status": self.status,
                    "phase": self.phase,
                    "requiresInput": self.question is not None,
                    "question": self.question,
                    "model": self.model,
                    "toolCalls": self.tool_calls,
                },
            }
        }


def _value(data: dict[str, Any], snake: str, camel: str) -> Any:
    return data.get(snake, data.get(camel))


class FlightWorkflow(AgentFrameworkWorkflow):
    def __init__(self, runtime: FlightRuntime, diagnostics: DiagnosticLog | None = None) -> None:
        super().__init__(
            name="speed-of-thought-flights", description="Contoso Bikes record flights"
        )
        self.runtime = runtime
        self.diagnostics = diagnostics
        self.flights: dict[str, FlightRecord] = {}

    async def run(self, input_data: dict[str, Any]) -> AsyncGenerator[BaseEvent, None]:
        thread_id = str(_value(input_data, "thread_id", "threadId") or "missing-thread")
        run_id = str(_value(input_data, "run_id", "runId") or "missing-run")
        forwarded = cast(
            dict[str, Any], _value(input_data, "forwarded_props", "forwardedProps") or {}
        )
        segment = forwarded.get("sotSegment")
        yield RunStartedEvent(thread_id=thread_id, run_id=run_id)

        existing = self.flights.get(thread_id)
        if existing is not None or not isinstance(segment, dict):
            # A reconnect replays the flight as it stands; a run without a segment has no work.
            if existing is not None:
                yield StateSnapshotEvent(snapshot=existing.state())
                yield MessagesSnapshotEvent(messages=existing.messages)  # type: ignore[arg-type]
                yield RunFinishedEvent(
                    thread_id=thread_id, run_id=run_id, outcome=RunFinishedSuccessOutcome()
                )
            else:
                yield RunErrorEvent(message="No segment to execute.", code="SOT_NO_SEGMENT")
            return

        session_id = str(
            forwarded.get("sotPresentationSessionId") or segment.get("session_id") or "default"
        )[:128]
        flight = FlightRecord(thread_id, segment, session_id, self.runtime.model)
        flight.messages.append(
            {"id": f"{thread_id}:user", "role": "user", "content": str(segment.get("text", ""))}
        )
        self.flights[thread_id] = flight
        started = time.perf_counter()
        self._record("INFO", "l2.flight.started", "L2 flight started", flight, run_id)
        yield StateSnapshotEvent(snapshot=flight.state())

        message_id = f"{run_id}:assistant"
        streamed = ""
        try:
            async for event in self.runtime.run(
                segment, session_id=session_id, operation_id=thread_id, run_id=run_id
            ):
                if isinstance(event, ToolStarted):
                    flight.phase = "executing"
                    flight.tool_calls += 1
                    yield ToolCallStartEvent(tool_call_id=event.call_id, tool_call_name=event.name)
                    yield ToolCallArgsEvent(
                        tool_call_id=event.call_id,
                        delta=json.dumps(event.arguments, ensure_ascii=False, default=str),
                    )
                    yield ToolCallEndEvent(tool_call_id=event.call_id)
                    yield StateSnapshotEvent(snapshot=flight.state())
                elif isinstance(event, ToolFinished):
                    content = json.dumps(event.result, ensure_ascii=False, default=str)
                    yield ToolCallResultEvent(
                        message_id=f"{run_id}:tool-result:{event.call_id}",
                        tool_call_id=event.call_id,
                        content=content,
                        role="tool",
                    )
                    self._record(
                        "INFO",
                        "l2.tool.completed",
                        f"{event.name} returned",
                        flight,
                        run_id,
                        duration_ms=event.duration_ms,
                        data={"tool": event.name, "result": event.result},
                        payload_kind="tool",
                    )
                elif isinstance(event, QuestionAsked):
                    flight.status, flight.question = "awaiting_input", event.question
                    yield StateSnapshotEvent(snapshot=flight.state())
                elif isinstance(event, QuestionAnswered):
                    flight.status, flight.question = "running", None
                    yield StateSnapshotEvent(snapshot=flight.state())
                elif isinstance(event, TextDelta):
                    if not streamed:
                        flight.phase = "reporting"
                        yield TextMessageStartEvent(message_id=message_id, role="assistant")
                    streamed += event.delta
                    yield TextMessageContentEvent(message_id=message_id, delta=event.delta)
                elif isinstance(event, FlightFinished):
                    if not streamed:
                        yield TextMessageStartEvent(message_id=message_id, role="assistant")
                        if event.text:
                            yield TextMessageContentEvent(message_id=message_id, delta=event.text)
                    elif event.text.startswith(streamed.strip()) and len(event.text) > len(
                        streamed.strip()
                    ):
                        yield TextMessageContentEvent(
                            message_id=message_id, delta=event.text[len(streamed.strip()) :]
                        )
                    yield TextMessageEndEvent(message_id=message_id)
                    flight.messages.append(
                        {"id": message_id, "role": "assistant", "content": event.text}
                    )
        except Exception as error:
            flight.status, flight.question = "failed", None
            self._record(
                "ERROR",
                "l2.flight.failed",
                str(error),
                flight,
                run_id,
                duration_ms=(time.perf_counter() - started) * 1_000,
                data={"errorType": type(error).__name__},
            )
            yield StateSnapshotEvent(snapshot=flight.state())
            yield RunErrorEvent(message=f"The flight failed: {error}", code="SOT_FLIGHT_FAILED")
            return

        flight.status, flight.phase = "succeeded", "reporting"
        self._record(
            "INFO",
            "l2.flight.completed",
            "L2 flight completed",
            flight,
            run_id,
            duration_ms=(time.perf_counter() - started) * 1_000,
            data={"toolCalls": flight.tool_calls, "response": flight.messages[-1]["content"]},
            payload_kind="response",
        )
        yield StateSnapshotEvent(snapshot=flight.state())
        yield MessagesSnapshotEvent(messages=flight.messages)  # type: ignore[arg-type]
        yield RunFinishedEvent(
            thread_id=thread_id, run_id=run_id, outcome=RunFinishedSuccessOutcome()
        )

    def _record(
        self,
        level: DiagnosticLevel,
        event: str,
        message: str,
        flight: FlightRecord,
        run_id: str,
        *,
        duration_ms: float | None = None,
        data: dict[str, Any] | None = None,
        payload_kind: PayloadKind = "metadata",
    ) -> None:
        if self.diagnostics is None:
            return
        self.diagnostics.record(
            level,
            "L2",
            event,
            message,
            utterance_id=str(flight.segment.get("utterance_id") or "") or None,
            operation_id=flight.thread_id,
            run_id=run_id,
            duration_ms=duration_ms,
            data={
                "thread": flight.segment.get("thread"),
                "sessionId": flight.session_id,
                **(data or {}),
            },
            payload_kind=payload_kind,
        )
