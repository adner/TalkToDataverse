"""Thread-safe absolute-window audio buffers for file and live streaming."""

from __future__ import annotations

import threading
import time
from typing import Any, Protocol


class AudioBuffer(Protocol):
    sample_rate: int

    @property
    def duration_ms(self) -> float: ...

    @property
    def closed_at_monotonic(self) -> float | None: ...

    def window(self, start: int, end: int) -> Any | None: ...

    def all_audio(self) -> Any: ...


class ArrayAudioBuffer:
    def __init__(self, audio: Any, sample_rate: int = 16_000) -> None:
        import numpy as np

        self._audio = np.asarray(audio, dtype=np.float32)
        self.sample_rate = sample_rate

    @property
    def closed_at_monotonic(self) -> float | None:
        return None

    @property
    def duration_ms(self) -> float:
        return len(self._audio) * 1_000 / self.sample_rate

    def window(self, start: int, end: int) -> Any | None:
        import numpy as np

        if start >= len(self._audio):
            return None
        left_padding = max(0, -start)
        chunk = self._audio[max(0, start) : min(end, len(self._audio))]
        right_padding = max(0, end - len(self._audio))
        if left_padding or right_padding:
            chunk = np.pad(chunk, (left_padding, right_padding))
        return chunk

    def all_audio(self) -> Any:
        return self._audio.copy()


class LiveAudioBuffer:
    """Append-only live buffer that blocks inference until a requested window exists."""

    def __init__(self, sample_rate: int = 16_000, initial: Any | None = None) -> None:
        import numpy as np

        self.sample_rate = sample_rate
        self._chunks: list[Any] = []
        self._length = 0
        self._closed = False
        self._cancelled = False
        self._closed_at_monotonic: float | None = None
        self._condition = threading.Condition()
        if initial is not None and len(initial):
            chunk = np.asarray(initial, dtype=np.float32).copy()
            self._chunks.append(chunk)
            self._length = len(chunk)

    @property
    def duration_ms(self) -> float:
        with self._condition:
            return self._length * 1_000 / self.sample_rate

    @property
    def closed_at_monotonic(self) -> float | None:
        with self._condition:
            return self._closed_at_monotonic

    def append(self, audio: Any) -> None:
        import numpy as np

        chunk = np.asarray(audio, dtype=np.float32).reshape(-1).copy()
        if not len(chunk):
            return
        with self._condition:
            if self._closed:
                return
            self._chunks.append(chunk)
            self._length += len(chunk)
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._closed_at_monotonic = time.perf_counter()
            self._condition.notify_all()

    def cancel(self) -> None:
        with self._condition:
            self._cancelled = True
            self._closed = True
            self._closed_at_monotonic = time.perf_counter()
            self._condition.notify_all()

    def window(self, start: int, end: int) -> Any | None:
        import numpy as np

        with self._condition:
            while self._length < end and not self._closed:
                self._condition.wait(timeout=0.1)
            if self._cancelled:
                return None
            if start >= self._length and self._closed:
                return None
            audio = np.concatenate(self._chunks) if self._chunks else np.empty(0, dtype=np.float32)
            left_padding = max(0, -start)
            chunk = audio[max(0, start) : min(end, self._length)]
            right_padding = max(0, end - self._length) if self._closed else 0
            if left_padding or right_padding:
                chunk = np.pad(chunk, (left_padding, right_padding))
            return chunk

    def all_audio(self) -> Any:
        import numpy as np

        with self._condition:
            return np.concatenate(self._chunks) if self._chunks else np.empty(0, dtype=np.float32)
