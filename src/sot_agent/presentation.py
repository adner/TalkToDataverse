"""Native L0/L1 presentation adapter with subscribe-then-snapshot watermarks."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

from .diagnostics import DiagnosticLog


class NativePresentationHub:
    def __init__(self, diagnostics: DiagnosticLog | None = None) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._diagnostics = diagnostics

    async def publish(self, source: str, event: dict[str, Any]) -> None:
        envelope = {"source": source, "event": event}
        if self._diagnostics is not None:
            event_type = str(event.get("type") or "native.event")
            is_error = event_type.endswith("error")
            is_cancel = event_type.endswith("cancelled")
            is_milestone = event_type in {
                "utterance_started",
                "utterance_ended",
                "utterance_finalized",
            } or (source.casefold() == "l1" and event.get("terminal") is True)
            self._diagnostics.record(
                (
                    "ERROR"
                    if is_error
                    else "WARNING"
                    if is_cancel
                    else "INFO"
                    if is_milestone
                    else "DEBUG"
                ),
                source,
                f"{source}.{event_type}",
                _native_message(source, event_type),
                utterance_id=str(event.get("utterance_id") or "") or None,
                data=event,
                payload_kind="intent" if source.casefold() == "l1" else "transcript",
            )
        for queue in tuple(self._subscribers):
            await queue.put(envelope)

    async def subscribe(self) -> AsyncGenerator[dict[str, Any], None]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)


def _native_message(source: str, event_type: str) -> str:
    readable = event_type.replace("_", " ")
    return f"{source.upper()} {readable}"
