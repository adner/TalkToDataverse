"""Experimental shared-mode WASAPI capture with off-callback resampling."""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from typing import Any

from .buffer import LiveAudioBuffer
from .vad import ActivityListener, EnergyVad, VadSettings


class MicrophoneCapture:
    """Keep a WASAPI device open and feed pre-roll/active 16 kHz audio buffers."""

    def __init__(
        self,
        *,
        device_index: int | None = None,
        device_match: str = "",
        target_sample_rate: int = 16_000,
        block_ms: int = 10,
        preroll_ms: int = 300,
        vad: VadSettings | None = None,
    ) -> None:
        self.device_index = device_index
        self.device_match = device_match
        self.target_sample_rate = target_sample_rate
        self.block_ms = block_ms
        self.preroll_ms = preroll_ms
        self.device_name = ""
        self.native_sample_rate = 0
        self.reported_latency_ms = 0.0
        self.dropped_frames = 0
        self.status_flags: list[str] = []
        self._raw_queue: queue.Queue[Any] = queue.Queue(maxsize=512)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._active: LiveAudioBuffer | None = None
        self._last_audio: LiveAudioBuffer | None = None
        self._preroll: deque[float] = deque()
        self._stream: Any | None = None
        self._worker: threading.Thread | None = None
        self.vad = EnergyVad(vad or VadSettings(), target_sample_rate)

    def start(self) -> None:
        import sounddevice as sd

        device_index = _select_wasapi_input(sd, self.device_match, self.device_index)
        self.device_index = device_index
        device = sd.query_devices(device_index, "input")
        self.device_name = str(device["name"])
        self.native_sample_rate = int(device["default_samplerate"])
        max_preroll_samples = round(self.target_sample_rate * self.preroll_ms / 1_000)
        self._preroll = deque(maxlen=max_preroll_samples)
        block_size = max(1, round(self.native_sample_rate * self.block_ms / 1_000))

        def callback(indata: Any, frames: int, time_info: Any, status: Any) -> None:
            del time_info
            if status:
                self.status_flags.append(str(status))
            try:
                self._raw_queue.put_nowait(indata[:, 0].copy())
            except queue.Full:
                self.dropped_frames += frames

        self._stop.clear()
        self._worker = threading.Thread(
            target=self._resample_loop, name="sot-l0-resample", daemon=True
        )
        self._worker.start()
        self._stream = sd.InputStream(
            device=device_index,
            samplerate=self.native_sample_rate,
            channels=1,
            dtype="float32",
            blocksize=block_size,
            latency="low",
            callback=callback,
        )
        self._stream.start()
        self.reported_latency_ms = float(self._stream.latency) * 1_000

    def begin(self) -> tuple[LiveAudioBuffer, int]:
        import numpy as np

        with self._lock:
            if self._active is not None:
                raise RuntimeError("A microphone utterance is already active.")
            initial = np.asarray(self._preroll, dtype=np.float32)
            buffer = LiveAudioBuffer(self.target_sample_rate, initial)
            self._active = buffer
            self._last_audio = None
        preroll_ms = round(len(initial) * 1_000 / self.target_sample_rate)
        return buffer, preroll_ms

    def end(self, tail_flush_ms: int) -> None:
        time.sleep(tail_flush_ms / 1_000)
        with self._lock:
            buffer = self._active
            self._active = None
            self._last_audio = buffer
        if buffer is not None:
            buffer.close()

    def set_activity_listener(self, listener: ActivityListener | None) -> None:
        """Report speech pauses and resumes for the active clip; ``None`` stops reporting."""

        with self._lock:
            if listener is None:
                self.vad.deactivate()
            else:
                self.vad.activate(listener)

    def cancel(self) -> None:
        with self._lock:
            buffer = self._active
            self._active = None
        if buffer is not None:
            buffer.cancel()

    def last_audio(self) -> Any:
        """Return a copy of the last completed utterance for an opt-in test harness."""

        with self._lock:
            buffer = self._last_audio
        if buffer is None:
            raise RuntimeError("No completed microphone utterance is available.")
        return buffer.all_audio()

    @property
    def buffered_preroll_ms(self) -> int:
        """Return the amount of audio currently retained before the next press."""

        with self._lock:
            sample_count = len(self._preroll)
        return round(sample_count * 1_000 / self.target_sample_rate)

    def close(self) -> None:
        self.cancel()
        self._stop.set()
        stream = self._stream
        self._stream = None
        if stream is not None:
            stream.stop()
            stream.close()
        if self._worker is not None:
            self._worker.join(timeout=2)
            self._worker = None

    def _resample_loop(self) -> None:
        import numpy as np
        import soxr

        resampler = soxr.ResampleStream(
            self.native_sample_rate,
            self.target_sample_rate,
            1,
            dtype="float32",
            quality="HQ",
        )
        while not self._stop.is_set() or not self._raw_queue.empty():
            try:
                raw = self._raw_queue.get(timeout=0.1)
            except queue.Empty:
                continue
            output = np.asarray(resampler.resample_chunk(raw, last=False), dtype=np.float32)
            if not len(output):
                continue
            with self._lock:
                self._preroll.extend(output.tolist())
                active = self._active
            if active is not None:
                active.append(output)
            with self._lock:
                self.vad.process(output)


def list_wasapi_inputs(sd: Any | None = None) -> list[dict[str, object]]:
    """Return selectable WASAPI microphones without opening an audio stream."""

    if sd is None:
        import sounddevice as sd

    wasapi_index, hostapi, candidates = _wasapi_inputs(sd)
    del wasapi_index
    default_input = int(hostapi["default_input_device"])
    return [
        {
            "id": index,
            "name": str(device["name"]),
            "sampleRate": int(device["default_samplerate"]),
            "channels": int(device["max_input_channels"]),
            "isDefault": index == default_input,
        }
        for index, device in candidates
    ]


def _wasapi_inputs(sd: Any) -> tuple[int, Any, list[tuple[int, Any]]]:
    hostapis = sd.query_hostapis()
    wasapi_index = next(
        (index for index, item in enumerate(hostapis) if item["name"] == "Windows WASAPI"),
        None,
    )
    if wasapi_index is None:
        raise RuntimeError("Windows WASAPI is not available through PortAudio.")
    devices = sd.query_devices()
    candidates = [
        (index, device)
        for index, device in enumerate(devices)
        if device["hostapi"] == wasapi_index and device["max_input_channels"] > 0
    ]
    return wasapi_index, hostapis[wasapi_index], candidates


def _select_wasapi_input(sd: Any, device_match: str, device_index: int | None = None) -> int:
    _, hostapi, candidates = _wasapi_inputs(sd)
    candidate_indexes = {index for index, _ in candidates}
    if device_index is not None:
        if device_index not in candidate_indexes:
            raise ValueError(f"WASAPI input device {device_index} is not available.")
        return device_index
    if device_match:
        needle = device_match.casefold()
        matches = [
            index for index, device in candidates if needle in str(device["name"]).casefold()
        ]
        if len(matches) != 1:
            raise ValueError(
                f"Expected one WASAPI input matching {device_match!r}; found {len(matches)}."
            )
        return matches[0]
    default_input = int(hostapi["default_input_device"])
    if default_input < 0:
        raise RuntimeError("WASAPI has no default input device.")
    return default_input
