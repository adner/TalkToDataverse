"""Versioned, serializable contracts for the L0 streaming transcription boundary."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Literal

SCHEMA_VERSION = 1


class JsonContract:
    """Dataclass JSON projection shared by every native L0 event."""

    def to_json(self) -> dict[str, object]:
        return asdict(self)  # type: ignore[arg-type]


@dataclass(frozen=True, slots=True)
class Word(JsonContract):
    i: int
    w: str
    cs: int
    ce: int
    t0: int | None = None
    t1: int | None = None
    conf: float | None = None


@dataclass(frozen=True, slots=True)
class StreamerReady(JsonContract):
    mono_ms: float
    model_id: str
    runtime: str
    locale: str
    latency_tier_ms: int
    device: str
    precision: str
    load_ms: float
    audio_device: str | None = None
    audio_rate: int | None = None
    timing_quality: Literal["model", "chunk_derived"] = "chunk_derived"
    confidence_available: bool = False
    schema_version: int = SCHEMA_VERSION
    type: Literal["streamer_ready"] = "streamer_ready"


@dataclass(frozen=True, slots=True)
class UtteranceStarted(JsonContract):
    utterance_id: str
    seq: int
    mono_ms: float
    locale: str
    latency_tier_ms: int
    preroll_ms: int
    press_at_audio_ms: int
    schema_version: int = SCHEMA_VERSION
    type: Literal["utterance_started"] = "utterance_started"


@dataclass(frozen=True, slots=True)
class TextAppended(JsonContract):
    utterance_id: str
    seq: int
    mono_ms: float
    words: tuple[Word, ...]
    tail: str
    stable_text: str
    text: str
    schema_version: int = SCHEMA_VERSION
    type: Literal["text_appended"] = "text_appended"


@dataclass(frozen=True, slots=True)
class SpeechPaused(JsonContract):
    """The speaker has been silent for ``silence_ms``; the silence began that long before."""

    utterance_id: str
    seq: int
    mono_ms: float
    silence_ms: float
    audio_ms: float
    schema_version: int = SCHEMA_VERSION
    type: Literal["speech_paused"] = "speech_paused"


@dataclass(frozen=True, slots=True)
class SpeechResumed(JsonContract):
    utterance_id: str
    seq: int
    mono_ms: float
    silence_ms: float
    audio_ms: float
    schema_version: int = SCHEMA_VERSION
    type: Literal["speech_resumed"] = "speech_resumed"


@dataclass(frozen=True, slots=True)
class UtteranceEnded(JsonContract):
    utterance_id: str
    seq: int
    mono_ms: float
    streamed_text: str
    word_count: int
    audio_duration_ms: int
    release_to_ended_ms: float
    second_pass_pending: bool
    schema_version: int = SCHEMA_VERSION
    type: Literal["utterance_ended"] = "utterance_ended"


@dataclass(frozen=True, slots=True)
class Revision(JsonContract):
    op: Literal["replace", "insert", "delete"]
    words: tuple[Word, ...] = ()
    start: int | None = None
    end: int | None = None
    at: int | None = None

    def to_json(self) -> dict[str, object]:
        result: dict[str, object] = {"op": self.op}
        if self.start is not None:
            result["from"] = self.start
        if self.end is not None:
            result["to"] = self.end
        if self.at is not None:
            result["at"] = self.at
        if self.op != "delete":
            result["words"] = [word.to_json() for word in self.words]
        return result


@dataclass(frozen=True, slots=True)
class UtteranceFinalized(JsonContract):
    utterance_id: str
    seq: int
    mono_ms: float
    final_text: str
    words: tuple[Word, ...]
    stable_prefix_len: int
    revisions: tuple[Revision, ...]
    second_pass: Literal["applied", "disabled", "failed", "timed_out"]
    second_pass_ms: float
    release_to_finalized_ms: float
    schema_version: int = SCHEMA_VERSION
    type: Literal["utterance_finalized"] = "utterance_finalized"

    def to_json(self) -> dict[str, object]:
        result = asdict(self)
        result["revisions"] = [revision.to_json() for revision in self.revisions]
        result["text"] = self.final_text
        return result


@dataclass(frozen=True, slots=True)
class UtteranceCancelled(JsonContract):
    utterance_id: str
    seq: int
    mono_ms: float
    reason: Literal["too_short", "cancelled", "error"]
    press_duration_ms: float
    schema_version: int = SCHEMA_VERSION
    type: Literal["utterance_cancelled"] = "utterance_cancelled"


@dataclass(frozen=True, slots=True)
class StreamerError(JsonContract):
    mono_ms: float
    severity: Literal["recoverable", "fatal"]
    code: str
    message: str
    utterance_id: str | None = None
    seq: int | None = None
    schema_version: int = SCHEMA_VERSION
    type: Literal["streamer_error"] = "streamer_error"


type StreamerEvent = (
    StreamerReady
    | UtteranceStarted
    | TextAppended
    | SpeechPaused
    | SpeechResumed
    | UtteranceEnded
    | UtteranceFinalized
    | UtteranceCancelled
    | StreamerError
)


@dataclass(frozen=True, slots=True)
class StreamerSnapshot(JsonContract):
    availability: Literal["ready", "unavailable", "degraded"] = "ready"
    press_state: Literal["idle", "pressed", "finalizing"] = "idle"
    utterance_id: str | None = None
    stable_text: str = ""
    tail: str = ""
    latest_seq: int | None = None
    error: str | None = None
    schema_version: int = SCHEMA_VERSION


@dataclass(slots=True)
class StreamerProjection:
    """In-memory L0 projection shared by fixture and real producers."""

    snapshot: StreamerSnapshot = field(default_factory=StreamerSnapshot)

    def current_snapshot(self) -> dict[str, object]:
        return self.snapshot.to_json()

    def apply(self, event: StreamerEvent) -> None:
        if isinstance(event, StreamerReady):
            self.snapshot = StreamerSnapshot(availability="ready")
            return
        if isinstance(event, StreamerError):
            self.snapshot = StreamerSnapshot(
                availability="unavailable" if event.severity == "fatal" else "degraded",
                press_state=self.snapshot.press_state,
                utterance_id=event.utterance_id or self.snapshot.utterance_id,
                stable_text=self.snapshot.stable_text,
                tail=self.snapshot.tail,
                latest_seq=event.seq if event.seq is not None else self.snapshot.latest_seq,
                error=event.message,
            )
            return

        press_state: Literal["idle", "pressed", "finalizing"] = self.snapshot.press_state
        stable_text = self.snapshot.stable_text
        tail = self.snapshot.tail
        if isinstance(event, UtteranceStarted):
            press_state, stable_text, tail = "pressed", "", ""
        elif isinstance(event, TextAppended):
            stable_text, tail = event.stable_text, event.tail
        elif isinstance(event, UtteranceEnded):
            press_state, stable_text, tail = "finalizing", event.streamed_text, ""
        elif isinstance(event, UtteranceFinalized):
            press_state, stable_text, tail = "idle", event.final_text, ""
        elif isinstance(event, UtteranceCancelled):
            press_state, stable_text, tail = "idle", "", ""
        self.snapshot = StreamerSnapshot(
            availability=self.snapshot.availability,
            press_state=press_state,
            utterance_id=event.utterance_id,
            stable_text=stable_text,
            tail=tail,
            latest_seq=event.seq,
            error=self.snapshot.error,
        )
