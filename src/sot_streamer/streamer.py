"""Public L0 facade for lifecycle, controls, event delivery, and audio transcription."""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from enum import StrEnum
from pathlib import Path

from .asr.engine import AsrEngine
from .asr.transformers_engine import TransformersNemotronEngine
from .audio.buffer import AudioBuffer
from .audio.file import load_audio_file
from .audio.mic import MicrophoneCapture
from .config import StreamerConfig
from .contracts import (
    StreamerError,
    StreamerEvent,
    StreamerProjection,
    StreamerReady,
    StreamerSnapshot,
)
from .ids import new_ulid
from .session import UtteranceSession


class StreamerState(StrEnum):
    LOADING = "loading"
    IDLE = "idle"
    CAPTURING = "capturing"
    FINALIZING = "finalizing"
    ERROR = "error"
    CLOSED = "closed"


class Streamer:
    def __init__(
        self,
        config: StreamerConfig,
        engine: AsrEngine | None = None,
        capture: MicrophoneCapture | None = None,
        *,
        warmup: bool = True,
    ) -> None:
        self.config = config
        self.engine = engine or TransformersNemotronEngine(
            model_id=config.model.model_id,
            device=config.model.device,
            precision=config.model.precision,
        )
        self.capture = capture
        self.warmup = warmup
        self.projection = StreamerProjection(
            StreamerSnapshot(availability="unavailable", press_state="idle")
        )
        self.state = StreamerState.CLOSED
        self._state_lock = threading.Lock()
        self._subscribers: set[Callable[[StreamerEvent], None]] = set()
        self._event_queues: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[StreamerEvent]]] = (
            set()
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._active_task: asyncio.Task[tuple[StreamerEvent, ...]] | None = None
        self._active_cancel: threading.Event | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        with self._state_lock:
            if self.state in (
                StreamerState.IDLE,
                StreamerState.CAPTURING,
                StreamerState.FINALIZING,
            ):
                return
            self.state = StreamerState.LOADING
        try:
            await asyncio.to_thread(
                self.engine.load,
                self.config.lookahead_tokens,
                self.config.model.locale,
                warmup=self.warmup,
            )
            if self.capture is not None:
                await asyncio.to_thread(self.capture.start)
            with self._state_lock:
                self.state = StreamerState.IDLE
            self._publish(
                StreamerReady(
                    mono_ms=time.monotonic() * 1_000,
                    model_id=self.engine.model_id,
                    runtime=self.engine.runtime_name,
                    locale=self.config.model.locale,
                    latency_tier_ms=self.config.model.latency_tier_ms,
                    device=self.engine.device_name,
                    precision=self.engine.precision_name,
                    load_ms=self.engine.load_ms,
                    audio_device=self.capture.device_name if self.capture else None,
                    audio_rate=self.capture.native_sample_rate if self.capture else None,
                )
            )
        except Exception as exc:
            with self._state_lock:
                self.state = StreamerState.ERROR
            self._publish_error("model_load_failed", str(exc), fatal=True)
            raise

    async def aclose(self) -> None:
        if self._active_task is not None and not self._active_task.done():
            self.cancel_utterance()
            with suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(asyncio.shield(self._active_task), timeout=5)
        if self.capture is not None:
            await asyncio.to_thread(self.capture.close)
        await asyncio.to_thread(self.engine.close)
        with self._state_lock:
            self.state = StreamerState.CLOSED

    async def transcribe_file(
        self, path: Path, utterance_id: str | None = None
    ) -> tuple[StreamerEvent, ...]:
        audio = await asyncio.to_thread(load_audio_file, path, self.config.audio.target_sr)
        return await self.transcribe_buffer(audio, utterance_id)

    async def transcribe_buffer(
        self,
        audio: AudioBuffer,
        utterance_id: str | None = None,
        *,
        preroll_ms: int = 0,
        cancel: threading.Event | None = None,
    ) -> tuple[StreamerEvent, ...]:
        with self._state_lock:
            valid = self.state == StreamerState.IDLE
            if valid:
                self.state = StreamerState.CAPTURING
        if not valid:
            self._publish_error(
                "invalid_state",
                f"Cannot start an utterance while streamer is {self.state}.",
                fatal=False,
            )
            return ()
        session = UtteranceSession(self.config, self.engine, self._publish)
        return await self._run_session(
            session,
            audio,
            utterance_id,
            preroll_ms=preroll_ms,
            cancel=cancel,
        )

    def begin_utterance(self, utterance_id: str | None = None) -> str:
        if self.capture is None or self._loop is None:
            self._publish_error(
                "capture_not_attached",
                "Use transcribe_buffer() or attach a microphone capture source.",
                fatal=False,
            )
            return ""
        with self._state_lock:
            valid = self.state == StreamerState.IDLE
            if valid:
                self.state = StreamerState.CAPTURING
        if not valid:
            self._publish_error(
                "invalid_state",
                f"Cannot begin an utterance while streamer is {self.state}.",
                fatal=False,
            )
            return ""

        utterance_id = utterance_id or new_ulid()
        try:
            audio, preroll_ms = self.capture.begin()
        except Exception as exc:
            with self._state_lock:
                self.state = StreamerState.IDLE
            self._publish_error("capture_begin_failed", str(exc), fatal=False)
            return ""
        cancel = threading.Event()
        self._active_cancel = cancel
        session = UtteranceSession(self.config, self.engine, self._publish)
        self.capture.set_activity_listener(session.report_activity)

        def schedule() -> None:
            if self._loop is None:
                return
            self._active_task = self._loop.create_task(
                self._run_session(
                    session,
                    audio,
                    utterance_id,
                    preroll_ms=preroll_ms,
                    cancel=cancel,
                )
            )

        self._loop.call_soon_threadsafe(schedule)
        return utterance_id

    def end_utterance(self) -> None:
        if self.capture is None or self._loop is None:
            self._publish_error(
                "capture_not_attached", "No facade capture source is attached.", fatal=False
            )
            return
        with self._state_lock:
            valid = self.state == StreamerState.CAPTURING
            if valid:
                self.state = StreamerState.FINALIZING
        if not valid:
            self._publish_error(
                "invalid_state",
                f"Cannot end an utterance while streamer is {self.state}.",
                fatal=False,
            )
            return
        self._loop.call_soon_threadsafe(
            lambda: asyncio.create_task(
                asyncio.to_thread(self.capture.end, self.config.utterance.tail_flush_ms)
            )
        )

    def cancel_utterance(self) -> None:
        if self.capture is None:
            self._publish_error(
                "capture_not_attached", "No facade capture source is attached.", fatal=False
            )
            return
        with self._state_lock:
            valid = self.state in (StreamerState.CAPTURING, StreamerState.FINALIZING)
        if not valid:
            self._publish_error(
                "invalid_state",
                f"Cannot cancel an utterance while streamer is {self.state}.",
                fatal=False,
            )
            return
        if self._active_cancel is not None:
            self._active_cancel.set()
        self.capture.cancel()

    async def wait_current_utterance(self) -> tuple[StreamerEvent, ...]:
        while self._active_task is None:
            await asyncio.sleep(0)
        return await asyncio.shield(self._active_task)

    def events(self) -> AsyncIterator[StreamerEvent]:
        async def iterate() -> AsyncIterator[StreamerEvent]:
            loop = asyncio.get_running_loop()
            queue: asyncio.Queue[StreamerEvent] = asyncio.Queue()
            registration = (loop, queue)
            self._event_queues.add(registration)
            try:
                while True:
                    yield await queue.get()
            finally:
                self._event_queues.discard(registration)

        return iterate()

    def subscribe(self, callback: Callable[[StreamerEvent], None]) -> Callable[[], None]:
        self._subscribers.add(callback)

        def unsubscribe() -> None:
            self._subscribers.discard(callback)

        return unsubscribe

    def current_snapshot(self) -> dict[str, object]:
        return self.projection.current_snapshot()

    async def _run_session(
        self,
        session: UtteranceSession,
        audio: AudioBuffer,
        utterance_id: str | None,
        *,
        preroll_ms: int,
        cancel: threading.Event | None,
    ) -> tuple[StreamerEvent, ...]:
        try:
            return await asyncio.to_thread(
                session.run,
                audio,
                utterance_id,
                preroll_ms=preroll_ms,
                cancel=cancel,
            )
        finally:
            if self.capture is not None:
                self.capture.set_activity_listener(None)
            with self._state_lock:
                if self.state != StreamerState.ERROR:
                    self.state = StreamerState.IDLE

    def _publish(self, event: StreamerEvent) -> None:
        self.projection.apply(event)
        for callback in tuple(self._subscribers):
            callback(event)
        for loop, queue in tuple(self._event_queues):
            loop.call_soon_threadsafe(queue.put_nowait, event)

    def _publish_error(self, code: str, message: str, *, fatal: bool) -> None:
        self._publish(
            StreamerError(
                mono_ms=time.monotonic() * 1_000,
                severity="fatal" if fatal else "recoverable",
                code=code,
                message=message,
            )
        )
