"""Energy-based speech pause detection against an adaptive noise floor."""

from __future__ import annotations

import math
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

ActivityKind = Literal["paused", "resumed"]
ActivityListener = Callable[[ActivityKind, float, float], None]


@dataclass(frozen=True, slots=True)
class VadSettings:
    pause_ms: int = 400
    margin_db: float = 10.0
    min_threshold_db: float = -55.0
    onset_ms: int = 60
    frame_ms: int = 20
    floor_window_ms: int = 5_000
    floor_percentile: float = 0.1


class EnergyVad:
    """Track the room's noise floor continuously and report pauses while a clip is active.

    The floor is a low percentile of recent frame levels, so it follows the room within a few
    seconds while the gaps between words keep speech from dragging it up. A frame is speech when
    it is ``margin_db`` above the floor.
    A pause is reported once, after ``pause_ms`` of continuous non-speech that follows speech; a
    resume is reported when speech returns after a reported pause. The listener receives the kind,
    the silence length in milliseconds, and the audio position in milliseconds since activation.
    """

    def __init__(self, settings: VadSettings, sample_rate: int = 16_000) -> None:
        self.settings = settings
        self.sample_rate = sample_rate
        self.floor_db = -60.0
        self._levels: deque[float] = deque(
            maxlen=max(1, settings.floor_window_ms // settings.frame_ms)
        )
        self._frame_samples = max(1, sample_rate * settings.frame_ms // 1_000)
        self._pending: list[float] = []
        self._listener: ActivityListener | None = None
        self._audio_ms = 0.0
        self._speech_seen = False
        self._speech_run_ms = 0.0
        self._silence_ms = 0.0
        self._paused = False

    @property
    def threshold_db(self) -> float:
        return max(self.floor_db + self.settings.margin_db, self.settings.min_threshold_db)

    def activate(self, listener: ActivityListener) -> None:
        self._listener = listener
        self._audio_ms = 0.0
        self._speech_seen = False
        self._speech_run_ms = 0.0
        self._silence_ms = 0.0
        self._paused = False

    def deactivate(self) -> None:
        self._listener = None

    def process(self, samples: Any) -> None:
        self._pending.extend(float(value) for value in samples)
        frame = self._frame_samples
        while len(self._pending) >= frame:
            chunk = self._pending[:frame]
            del self._pending[:frame]
            self._frame(chunk)

    def _frame(self, chunk: list[float]) -> None:
        settings = self.settings
        rms = math.sqrt(sum(value * value for value in chunk) / len(chunk))
        level_db = 20 * math.log10(rms + 1e-10)
        speech = level_db >= self.threshold_db
        self._levels.append(level_db)
        ordered = sorted(self._levels)
        self.floor_db = ordered[int(settings.floor_percentile * (len(ordered) - 1))]

        listener = self._listener
        if listener is None:
            return
        self._audio_ms += settings.frame_ms
        if speech:
            self._speech_run_ms += settings.frame_ms
            if self._speech_run_ms < settings.onset_ms:
                return
            if self._paused:
                listener("resumed", self._silence_ms, self._audio_ms)
            self._speech_seen = True
            self._paused = False
            self._silence_ms = 0.0
            return
        self._speech_run_ms = 0.0
        if not self._speech_seen:
            return
        self._silence_ms += settings.frame_ms
        if not self._paused and self._silence_ms >= settings.pause_ms:
            self._paused = True
            listener("paused", self._silence_ms, self._audio_ms)
