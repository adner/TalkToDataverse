"""L0 microphone capture and the L1 per-word decision loop, feeding the native presentation hub."""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from sot_intent.decision import DecisionModel, DecisionPool, SystemOneClient, read_key
from sot_intent.hold import HoldLoop, HoldSettings
from sot_intent.threads import ThreadRegistry
from sot_streamer.audio.mic import MicrophoneCapture, list_wasapi_inputs
from sot_streamer.config import load_streamer_config
from sot_streamer.contracts import StreamerEvent, StreamerProjection
from sot_streamer.streamer import Streamer, StreamerState

from .config import AppConfig
from .diagnostics import DiagnosticLog
from .flights import QuestionBroker
from .presentation import NativePresentationHub


@dataclass(slots=True)
class InputRuntime:
    settings: AppConfig
    streamer_projection: StreamerProjection
    hub: NativePresentationHub
    diagnostics: DiagnosticLog | None = None
    threads: ThreadRegistry = field(default_factory=ThreadRegistry)
    # A cut that answers a waiting question resolves it instead of launching a flight.
    questions: QuestionBroker | None = None
    _streamer: Streamer | None = field(default=None, init=False, repr=False)
    _decision_model: DecisionModel | None = field(default=None, init=False, repr=False)
    _decision_clients: list[SystemOneClient] = field(default_factory=list, init=False)
    _holds: dict[str, HoldLoop] = field(default_factory=dict, init=False, repr=False)
    _hold_sessions: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _loop: asyncio.AbstractEventLoop | None = field(default=None, init=False, repr=False)
    _terminal: dict[str, asyncio.Future[dict[str, object]]] = field(
        default_factory=dict, init=False
    )
    _microphone_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _audio_device_id: int | None = field(default=None, init=False, repr=False)
    _audio_preroll_ms: int | None = field(default=None, init=False, repr=False)
    _audio_tail_flush_ms: int | None = field(default=None, init=False, repr=False)
    _audio_min_press_ms: int | None = field(default=None, init=False, repr=False)
    _press_started_at: dict[str, float] = field(default_factory=dict, init=False, repr=False)

    async def audio_devices(self) -> dict[str, object]:
        devices = await asyncio.to_thread(list_wasapi_inputs)
        return {"status": "ready", "devices": devices}

    async def audio_settings(self) -> dict[str, object]:
        config = load_streamer_config(Path("config/l0.toml"))
        preroll_ms = (
            self._audio_preroll_ms
            if self._audio_preroll_ms is not None
            else config.utterance.preroll_ms
        )
        tail_flush_ms = (
            self._audio_tail_flush_ms
            if self._audio_tail_flush_ms is not None
            else config.utterance.tail_flush_ms
        )
        min_press_ms = (
            self._audio_min_press_ms
            if self._audio_min_press_ms is not None
            else config.utterance.min_press_ms
        )
        capture = self._streamer.capture if self._streamer is not None else None
        state = self._streamer.state if self._streamer is not None else StreamerState.CLOSED
        return {
            "status": "ready",
            "initialized": self._streamer is not None,
            "armed": state == StreamerState.IDLE,
            "captureState": state.value,
            "settings": {
                "deviceId": self._audio_device_id,
                "prerollMs": preroll_ms,
                "tailFlushMs": tail_flush_ms,
                "minimumClipMs": min_press_ms,
                "blockMs": config.audio.block_ms,
                "targetSampleRate": config.audio.target_sr,
            },
            "device": {
                "id": capture.device_index if capture is not None else self._audio_device_id,
                "name": capture.device_name if capture is not None else None,
                "sampleRate": capture.native_sample_rate if capture is not None else None,
            },
            "bufferedPrerollMs": capture.buffered_preroll_ms if capture is not None else 0,
        }

    async def initialize_microphone(self, device_id: int | None = None) -> dict[str, object]:
        async with self._microphone_lock:
            if self._streamer is None:
                if device_id is not None:
                    self._audio_device_id = device_id
                await self._start_microphone()
            return await self.audio_settings()

    async def configure_microphone(
        self,
        *,
        device_id: int | None,
        preroll_ms: int,
        tail_flush_ms: int,
        min_press_ms: int,
    ) -> dict[str, object]:
        async with self._microphone_lock:
            if self._streamer is not None and self._streamer.state in (
                StreamerState.CAPTURING,
                StreamerState.FINALIZING,
            ):
                raise RuntimeError(
                    "Release the push-to-talk control before changing audio settings."
                )

            changed = (
                device_id != self._audio_device_id
                or preroll_ms != self._audio_preroll_ms
                or tail_flush_ms != self._audio_tail_flush_ms
                or min_press_ms != self._audio_min_press_ms
            )
            self._audio_device_id = device_id
            self._audio_preroll_ms = preroll_ms
            self._audio_tail_flush_ms = tail_flush_ms
            self._audio_min_press_ms = min_press_ms
            if self._streamer is None:
                await self._start_microphone()
            elif changed:
                await self._streamer.aclose()
                self._streamer = None
                await self._start_microphone()
            return await self.audio_settings()

    async def _start_microphone(self) -> None:
        started = time.perf_counter()
        if self.diagnostics is not None:
            self.diagnostics.record(
                "INFO", "L0", "l0.microphone.starting", "Opening the audio ring buffer"
            )
        config = load_streamer_config(Path("config/l0.toml"))
        utterance = config.utterance.model_copy(
            update={
                "preroll_ms": (
                    self._audio_preroll_ms
                    if self._audio_preroll_ms is not None
                    else config.utterance.preroll_ms
                ),
                "tail_flush_ms": (
                    self._audio_tail_flush_ms
                    if self._audio_tail_flush_ms is not None
                    else config.utterance.tail_flush_ms
                ),
                "min_press_ms": (
                    self._audio_min_press_ms
                    if self._audio_min_press_ms is not None
                    else config.utterance.min_press_ms
                ),
            }
        )
        config = config.model_copy(update={"utterance": utterance})
        capture = MicrophoneCapture(
            device_index=self._audio_device_id,
            device_match=config.audio.device_match,
            target_sample_rate=config.audio.target_sr,
            block_ms=config.audio.block_ms,
            preroll_ms=config.utterance.preroll_ms,
            vad=config.vad.settings(),
        )
        streamer = Streamer(config, capture=capture)
        self._loop = asyncio.get_running_loop()
        streamer.subscribe(self._on_streamer_event)
        await streamer.start()
        self._streamer = streamer
        self._audio_device_id = capture.device_index
        self._audio_preroll_ms = config.utterance.preroll_ms
        self._audio_tail_flush_ms = config.utterance.tail_flush_ms
        self._audio_min_press_ms = config.utterance.min_press_ms
        self.streamer_projection = streamer.projection
        if self.diagnostics is not None:
            self.diagnostics.record(
                "INFO",
                "L0",
                "l0.microphone.ready",
                "Audio ring buffer armed",
                duration_ms=(time.perf_counter() - started) * 1_000,
                data={
                    "deviceId": capture.device_index,
                    "deviceName": capture.device_name,
                    "nativeSampleRate": capture.native_sample_rate,
                    "targetSampleRate": config.audio.target_sr,
                    "prerollMs": config.utterance.preroll_ms,
                    "tailFlushMs": config.utterance.tail_flush_ms,
                    "minimumClipMs": config.utterance.min_press_ms,
                },
            )

    async def close(self) -> None:
        if self._streamer is not None:
            await self._streamer.aclose()
        for client in self._decision_clients:
            await client.aclose()

    def _decisions(self) -> DecisionModel:
        if self._decision_model is None:
            pools = self.settings.l1.decision_pools
            if not pools:
                raise RuntimeError("Decision mode needs at least one l1.decision_pools entry")
            self._decision_clients = [
                SystemOneClient(
                    endpoint=pool.endpoint,
                    model=pool.deployment,
                    api_key=read_key(Path(pool.key_file)),
                )
                for pool in pools
            ]
            self._decision_model = DecisionPool(
                [(client, self.settings.l1.decision_rpm) for client in self._decision_clients]
            )
        return self._decision_model

    def _hold_settings(self) -> HoldSettings:
        l1 = self.settings.l1
        return HoldSettings(
            complete_threshold=l1.complete_threshold,
            write_pause_ms=l1.write_pause_ms,
            schema_threshold=l1.schema_threshold,
            ui_threshold=l1.ui_threshold,
            reserve_s=l1.reserve_ms / 1_000,
            settle_ms=l1.settle_ms,
        )

    def _new_hold(self, utterance_id: str, session_id: str) -> HoldLoop:
        async def publish(event: dict[str, object]) -> None:
            await self.hub.publish("l1", event)
            segment = event.get("segment")
            if event.get("type") == "segment_cut" and isinstance(segment, dict):
                answers = segment.get("answers")
                if self.questions is not None and isinstance(answers, int):
                    self.questions.answer(session_id, answers, str(segment.get("text") or ""))

        hold = HoldLoop(
            utterance_id,
            session_id,
            self._decisions(),
            self.threads,
            publish,
            self._hold_settings(),
        )
        self._holds[utterance_id] = hold
        return hold

    async def _handle_decision_event(self, value: dict[str, object]) -> None:
        """Drive the per-word loop from L0 events."""

        event_type = value.get("type")
        utterance_id = str(value.get("utterance_id") or "")
        if event_type == "utterance_started":
            session_id = self._hold_sessions.pop(utterance_id, "default")
            self._new_hold(utterance_id, session_id)
            return
        hold = self._holds.get(utterance_id)
        if hold is None:
            return
        if event_type == "text_appended":
            hold.on_text(str(value.get("text") or ""), len(str(value["stable_text"]).split()))
        elif event_type == "speech_paused":
            hold.on_pause(float(str(value["silence_ms"])))
        elif event_type == "speech_resumed":
            hold.on_resume()
        elif event_type == "utterance_ended":
            text = str(value.get("streamed_text") or "")
            future = self._terminal.get(utterance_id)
            try:
                segments = await hold.release(text)
                if future is not None and not future.done():
                    future.set_result(
                        {
                            "utteranceId": utterance_id,
                            "text": text,
                            "segments": [segment.to_json() for segment in segments],
                        }
                    )
            except Exception as exc:
                if future is not None and not future.done():
                    future.set_exception(exc)
            finally:
                self._holds.pop(utterance_id, None)
        elif event_type == "utterance_cancelled":
            hold.cancel()
            self._holds.pop(utterance_id, None)

    async def press(self, session_id: str = "default") -> str:
        async with self._microphone_lock:
            if self._streamer is None:
                await self._start_microphone()
            utterance_id = self._streamer.begin_utterance()
            if not utterance_id:
                raise RuntimeError("Microphone capture could not start")
            # Stored before the loop can handle this utterance's utterance_started event.
            self._hold_sessions[utterance_id] = session_id
            self._terminal[utterance_id] = asyncio.get_running_loop().create_future()
            self._press_started_at[utterance_id] = time.perf_counter()
            if self.diagnostics is not None:
                self.diagnostics.record(
                    "INFO",
                    "L0",
                    "l0.capture.pressed",
                    "Push-to-talk capture started",
                    utterance_id=utterance_id,
                    data={"bufferedPrerollMs": self._streamer.capture.buffered_preroll_ms},
                )
            return utterance_id

    async def release(self, utterance_id: str) -> dict[str, object]:
        if self._streamer is None:
            raise RuntimeError("Microphone capture has not been initialized")
        future = self._terminal.get(utterance_id)
        pressed_at = self._press_started_at.pop(utterance_id, None)
        if future is None or pressed_at is None:
            raise RuntimeError("The utterance does not match the active capture")
        held_ms = round((time.perf_counter() - pressed_at) * 1_000)
        minimum_ms = self._streamer.config.utterance.min_press_ms
        if held_ms < minimum_ms:
            self._streamer.cancel_utterance()
            await self._streamer.wait_current_utterance()
            pending = self._terminal.pop(utterance_id, None)
            if pending is not None and not pending.done():
                pending.cancel()
            if self.diagnostics is not None:
                self.diagnostics.record(
                    "WARNING",
                    "L0",
                    "l0.capture.discarded",
                    "Clip discarded before L1",
                    utterance_id=utterance_id,
                    duration_ms=held_ms,
                    data={"reason": "too_short", "minimumMs": minimum_ms},
                )
            return {
                "utteranceId": utterance_id,
                "discarded": True,
                "reason": "too_short",
                "durationMs": held_ms,
                "minimumMs": minimum_ms,
            }
        self._streamer.end_utterance()
        if self.diagnostics is not None:
            self.diagnostics.record(
                "INFO",
                "L0",
                "l0.capture.released",
                "Push-to-talk capture released",
                utterance_id=utterance_id,
                duration_ms=held_ms,
            )
        # Segments are cut from the streamed text; the second pass never relaunches them.
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=30)
        finally:
            self._terminal.pop(utterance_id, None)

    def cancel(self) -> None:
        if self._streamer is not None:
            self._streamer.cancel_utterance()
        self._press_started_at.clear()

    async def typed(self, text: str, session_id: str = "default") -> dict[str, object]:
        """Typed input is one hold with no pauses: one decision on release, one segment."""

        utterance_id = str(uuid.uuid4())
        hold = self._new_hold(utterance_id, session_id)
        try:
            hold.on_text(text, 0)
            segments = await hold.release(text)
        finally:
            self._holds.pop(utterance_id, None)
        return {
            "utteranceId": utterance_id,
            "text": text,
            "segments": [segment.to_json() for segment in segments],
        }

    def _on_streamer_event(self, event: StreamerEvent) -> None:
        if self._loop is not None:
            asyncio.run_coroutine_threadsafe(self._handle_streamer_event(event), self._loop)

    async def _handle_streamer_event(self, event: StreamerEvent) -> None:
        value = event.to_json()
        await self.hub.publish("l0", value)
        await self._handle_decision_event(value)
