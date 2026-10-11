"""Structured, correlation-first diagnostic traces for local end-to-end debugging."""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from threading import RLock
from typing import Any, Literal

from .config import DiagnosticsConfig
from .safety import sanitize

DiagnosticLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
PayloadKind = Literal["metadata", "transcript", "intent", "tool", "response"]

_LEVELS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
}
_TRANSCRIPT_KEYS = frozenset(
    {"content", "stable_text", "streamed_text", "tail", "text", "transcript", "w"}
)
_RESPONSE_KEYS = frozenset({"content", "response", "result", "text"})
_TOOL_KEYS = frozenset({"arguments", "args", "content", "result"})
_INTENT_KEYS = frozenset({"basis", "execution_profile", "semantic_frame"})


class DiagnosticLog:
    """Bounded in-memory trace with optional rotating JSONL and console sinks.

    Logging is deliberately best-effort. A broken diagnostics sink must never alter
    transcription, intent routing, tool execution, or the authoritative audit record.
    """

    def __init__(self, settings: DiagnosticsConfig) -> None:
        self._lock = RLock()
        self._settings = settings
        self._events: deque[dict[str, Any]] = deque(maxlen=settings.memory_events)
        self._next_id = 1
        self._started_mono = time.monotonic()
        self._logger = logging.getLogger(f"sot.diagnostics.{id(self)}")
        self._logger.propagate = False
        self._logger.handlers.clear()
        self._configure_handlers()

    def settings(self) -> dict[str, Any]:
        with self._lock:
            value = self._settings.model_dump(mode="json")
        return _camelize(value)

    def configure(self, updates: Mapping[str, Any]) -> dict[str, Any]:
        """Validate and apply settings without restarting the application."""

        snake_updates = {_snake_key(str(key)): value for key, value in updates.items()}
        with self._lock:
            updated = self._settings.model_copy(update=snake_updates)
            updated = DiagnosticsConfig.model_validate(updated.model_dump())
            if updated.memory_events != self._settings.memory_events:
                self._events = deque(self._events, maxlen=updated.memory_events)
            self._settings = updated
            self._configure_handlers()
        self.record(
            "INFO",
            "SYSTEM",
            "diagnostics.settings.updated",
            "Diagnostic logging settings updated",
            data={"level": updated.level, "console": updated.console, "file": updated.file},
        )
        return self.settings()

    def record(
        self,
        level: DiagnosticLevel,
        layer: str,
        event: str,
        message: str,
        *,
        utterance_id: str | None = None,
        operation_id: str | None = None,
        run_id: str | None = None,
        tool_call_id: str | None = None,
        duration_ms: float | int | None = None,
        data: Any = None,
        payload_kind: PayloadKind = "metadata",
    ) -> dict[str, Any] | None:
        """Record a sanitized event if its level is enabled."""

        try:
            with self._lock:
                settings = self._settings
                if not settings.enabled or _LEVELS[level] < _LEVELS[settings.level]:
                    return None
                event_id = self._next_id
                self._next_id += 1
                payload = _payload(data, payload_kind, settings)
                item: dict[str, Any] = {
                    "id": event_id,
                    "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
                    "sinceStartMs": round((time.monotonic() - self._started_mono) * 1_000, 1),
                    "level": level,
                    "layer": layer.upper(),
                    "event": event,
                    "message": message,
                }
                if utterance_id:
                    item["utteranceId"] = utterance_id
                if operation_id:
                    item["operationId"] = operation_id
                if run_id:
                    item["runId"] = run_id
                if tool_call_id:
                    item["toolCallId"] = tool_call_id
                if duration_ms is not None:
                    item["durationMs"] = round(float(duration_ms), 1)
                if payload is not None:
                    item["data"] = payload
                self._events.append(item)
                self._logger.log(_LEVELS[level], json.dumps(item, separators=(",", ":")))
                return item
        except Exception:
            # Observability must remain strictly outside the execution failure domain.
            return None

    def query(
        self,
        *,
        after: int = 0,
        limit: int = 500,
        level: str | None = None,
        layer: str | None = None,
        trace: str | None = None,
        search: str | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            events = list(self._events)
            settings = self.settings()
        level_value = level.upper() if level else None
        layer_value = layer.upper() if layer else None
        search_value = search.casefold().strip() if search else None

        def matches(item: dict[str, Any]) -> bool:
            if int(item["id"]) <= after:
                return False
            if level_value and item.get("level") != level_value:
                return False
            if layer_value and item.get("layer") != layer_value:
                return False
            if trace and trace not in {
                item.get("utteranceId"),
                item.get("operationId"),
                item.get("runId"),
                item.get("toolCallId"),
            }:
                return False
            return not (
                search_value and search_value not in json.dumps(item, default=str).casefold()
            )

        filtered = [item for item in events if matches(item)]
        selected = filtered[-limit:]
        return {
            "schemaVersion": 1,
            "events": selected,
            "latestId": events[-1]["id"] if events else 0,
            "matched": len(filtered),
            "retained": len(events),
            "capacity": settings["memoryEvents"],
        }

    def export(self, trace: str | None = None) -> dict[str, Any]:
        result = self.query(limit=self._settings.memory_events, trace=trace)
        return {
            "schemaVersion": 1,
            "exportedAt": datetime.now(UTC).isoformat(timespec="seconds"),
            "filter": {"trace": trace},
            "settings": self.settings(),
            "events": result["events"],
        }

    def clear(self) -> int:
        with self._lock:
            count = len(self._events)
            self._events.clear()
            return count

    def close(self) -> None:
        with self._lock:
            for handler in tuple(self._logger.handlers):
                handler.close()
                self._logger.removeHandler(handler)

    def _configure_handlers(self) -> None:
        for handler in tuple(self._logger.handlers):
            handler.close()
            self._logger.removeHandler(handler)
        self._logger.setLevel(_LEVELS[self._settings.level])
        formatter = logging.Formatter("%(message)s")
        if self._settings.console:
            console = logging.StreamHandler()
            console.setFormatter(formatter)
            self._logger.addHandler(console)
        if self._settings.file:
            path = Path(self._settings.file_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                path,
                maxBytes=self._settings.max_file_bytes,
                backupCount=self._settings.backup_count,
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            self._logger.addHandler(file_handler)
        if not self._logger.handlers:
            self._logger.addHandler(logging.NullHandler())


def _payload(data: Any, kind: PayloadKind, settings: DiagnosticsConfig) -> Any:
    if data is None:
        return None
    allowed = {
        "metadata": True,
        "transcript": settings.include_transcripts,
        "intent": settings.include_intent_payloads,
        "tool": settings.include_tool_payloads,
        "response": settings.include_responses,
    }[kind]
    if not allowed:
        return {"omitted": True, "payloadKind": kind}
    sanitized = sanitize(data)
    if not settings.include_transcripts:
        sanitized = _redact_keys(sanitized, _TRANSCRIPT_KEYS)
    if not settings.include_responses:
        sanitized = _redact_keys(sanitized, _RESPONSE_KEYS)
    if not settings.include_tool_payloads:
        sanitized = _redact_keys(sanitized, _TOOL_KEYS)
    if not settings.include_intent_payloads:
        sanitized = _redact_keys(sanitized, _INTENT_KEYS)
    return sanitized


def _redact_keys(value: Any, keys: frozenset[str]) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[OMITTED]" if str(key).casefold() in keys else _redact_keys(item, keys)
            for key, item in value.items()
        }
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_redact_keys(item, keys) for item in value]
    return value


def _camelize(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {_camel_key(str(key)): _camelize(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_camelize(item) for item in value]
    return value


def _camel_key(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


def _snake_key(value: str) -> str:
    result = ""
    for char in value:
        result += f"_{char.lower()}" if char.isupper() else char
    return result
