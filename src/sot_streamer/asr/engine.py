"""Narrow runtime protocol used by the L0 orchestration layer."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Protocol

from ..audio.buffer import AudioBuffer


@dataclass(frozen=True, slots=True)
class EngineFragment:
    text: str
    audio_ms: float
    is_snapshot: bool = False


class AsrEngine(Protocol):
    model_id: str
    runtime_name: str
    device_name: str
    precision_name: str
    load_ms: float

    def load(self, lookahead: int, language: str, *, warmup: bool = True) -> None: ...

    def stream(
        self,
        audio: AudioBuffer,
        lookahead: int,
        language: str,
        cancel: threading.Event | None = None,
    ) -> Iterator[EngineFragment]: ...

    def transcribe(
        self,
        audio: AudioBuffer,
        lookahead: int,
        language: str,
        cancel: threading.Event | None = None,
    ) -> str: ...

    def close(self) -> None: ...
