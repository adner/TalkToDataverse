"""One-utterance event orchestration for file and live audio buffers."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import replace

from .asr.engine import AsrEngine
from .assembler import TextAssembler, words_from_text
from .audio.buffer import AudioBuffer
from .config import LOOKAHEAD_BY_TIER, StreamerConfig
from .contracts import (
    SpeechPaused,
    SpeechResumed,
    StreamerEvent,
    TextAppended,
    UtteranceCancelled,
    UtteranceEnded,
    UtteranceFinalized,
    UtteranceStarted,
)
from .diff import diff_words
from .ids import new_ulid

EventSink = Callable[[StreamerEvent], None]


class UtteranceSession:
    def __init__(self, config: StreamerConfig, engine: AsrEngine, sink: EventSink) -> None:
        self.config = config
        self.engine = engine
        self.sink = sink
        self._lock = threading.Lock()
        self._sequence = 0
        self._utterance_id: str | None = None
        self._ended = False

    def report_activity(self, kind: str, silence_ms: float, audio_ms: float) -> None:
        """Emit a pause or resume detected on the capture thread, in sequence with the text."""

        with self._lock:
            utterance_id = self._utterance_id
            if utterance_id is None or self._ended:
                return
            seq = self._sequence
            self._sequence += 1
            event_type = SpeechPaused if kind == "paused" else SpeechResumed
            event = event_type(
                utterance_id=utterance_id,
                seq=seq,
                mono_ms=_mono_ms(),
                silence_ms=silence_ms,
                audio_ms=audio_ms,
            )
            self.sink(event)

    def run(
        self,
        audio: AudioBuffer,
        utterance_id: str | None = None,
        *,
        preroll_ms: int = 0,
        cancel: threading.Event | None = None,
    ) -> tuple[StreamerEvent, ...]:
        cancel = cancel or threading.Event()
        emitted: list[StreamerEvent] = []
        utterance_id = utterance_id or new_ulid()

        def emit(event: StreamerEvent) -> None:
            # Numbered under the lock so pause events from the capture thread stay in order.
            with self._lock:
                event = replace(event, seq=self._sequence)
                self._sequence += 1
                if isinstance(event, UtteranceEnded | UtteranceCancelled):
                    self._ended = True
                emitted.append(event)
                self.sink(event)

        with self._lock:
            self._utterance_id = utterance_id
        emit(
            UtteranceStarted(
                utterance_id=utterance_id,
                seq=0,
                mono_ms=_mono_ms(),
                locale=self.config.model.locale,
                latency_tier_ms=self.config.model.latency_tier_ms,
                preroll_ms=preroll_ms,
                press_at_audio_ms=preroll_ms,
            )
        )
        assembler = TextAssembler()
        try:
            fragments = self.engine.stream(
                audio,
                self.config.lookahead_tokens,
                self.config.model.locale,
                cancel,
            )
            for fragment in fragments:
                if cancel.is_set():
                    break
                delta = (
                    assembler.update(fragment.text, fragment.audio_ms)
                    if fragment.is_snapshot
                    else assembler.append(fragment.text, fragment.audio_ms)
                )
                if delta.changed:
                    emit(
                        TextAppended(
                            utterance_id=utterance_id,
                            seq=0,
                            mono_ms=_mono_ms(),
                            words=delta.words,
                            tail=delta.tail,
                            stable_text=delta.stable_text,
                            text=delta.text,
                        )
                    )
        except Exception:
            cancel.set()
            emit(
                UtteranceCancelled(
                    utterance_id=utterance_id,
                    seq=0,
                    mono_ms=_mono_ms(),
                    reason="error",
                    press_duration_ms=audio.duration_ms,
                )
            )
            raise

        if cancel.is_set():
            emit(
                UtteranceCancelled(
                    utterance_id=utterance_id,
                    seq=0,
                    mono_ms=_mono_ms(),
                    reason="cancelled",
                    press_duration_ms=audio.duration_ms,
                )
            )
            return tuple(emitted)

        delta = assembler.flush(audio.duration_ms)
        if delta.changed:
            emit(
                TextAppended(
                    utterance_id=utterance_id,
                    seq=0,
                    mono_ms=_mono_ms(),
                    words=delta.words,
                    tail=delta.tail,
                    stable_text=delta.stable_text,
                    text=delta.text,
                )
            )

        streamed_text = assembler.stable_text
        ended_at = time.perf_counter()
        release_at = audio.closed_at_monotonic or ended_at
        emit(
            UtteranceEnded(
                utterance_id=utterance_id,
                seq=0,
                mono_ms=_mono_ms(),
                streamed_text=streamed_text,
                word_count=len(assembler.words),
                audio_duration_ms=round(audio.duration_ms),
                release_to_ended_ms=(ended_at - release_at) * 1_000,
                second_pass_pending=self.config.second_pass.enabled,
            )
        )

        second_pass_started = time.perf_counter()
        outcome: str = "disabled"
        final_text = streamed_text
        if self.config.second_pass.enabled:
            try:
                final_text = " ".join(
                    self.engine.transcribe(
                        audio,
                        LOOKAHEAD_BY_TIER[1120],
                        self.config.model.locale,
                    ).split()
                )
                outcome = "applied"
            except Exception:
                outcome = "failed"
                final_text = streamed_text
        second_pass_ms = (time.perf_counter() - second_pass_started) * 1_000
        if outcome == "applied" and second_pass_ms > self.config.second_pass.timeout_ms:
            outcome = "timed_out"
            final_text = streamed_text

        final_words = words_from_text(final_text, round(audio.duration_ms))
        stable_prefix_len, revisions = diff_words(assembler.words, final_words)
        if outcome != "applied":
            revisions = ()
            stable_prefix_len = len(assembler.words)
            final_words = assembler.words
        emit(
            UtteranceFinalized(
                utterance_id=utterance_id,
                seq=0,
                mono_ms=_mono_ms(),
                final_text=final_text,
                words=final_words,
                stable_prefix_len=stable_prefix_len,
                revisions=revisions,
                second_pass=outcome,  # type: ignore[arg-type]
                second_pass_ms=second_pass_ms,
                release_to_finalized_ms=(time.perf_counter() - release_at) * 1_000,
            )
        )
        return tuple(emitted)


def _mono_ms() -> float:
    return time.monotonic() * 1_000
