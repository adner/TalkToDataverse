"""The live L1 loop for one push-to-talk hold: paced decisions, pause-based cuts, handoffs.

L0 feeds the loop transcript updates, speech pauses and the release. The loop asks the decision
model about the text since the last cut, at most one call at a time, folding words that arrive
during a call into the next one. A pause cuts a segment when the latest decision says the
instruction is complete; writes need a longer pause. Release cuts whatever is left.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, Protocol

from .decision import DecisionModel, DecisionResult
from .decision_questions import Reading, build_questions, build_state, interpret
from .threads import ThreadRegistry

DecisionKind = Literal["word", "pause", "release"]
Publish = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class HoldSettings:
    complete_threshold: float = 0.7
    write_pause_ms: int = 1_000
    schema_threshold: float = 0.5
    ui_threshold: float = 0.7
    reserve_s: float = 0.3
    settle_ms: int = 150


class _Spare(Protocol):
    def can_spare(self, reserve_s: float) -> bool: ...


@dataclass(frozen=True, slots=True)
class Decision:
    kind: DecisionKind
    start: int
    end: int
    text: str
    result: DecisionResult
    reading: Reading

    def noul(self, key: str) -> float | None:
        answer = self.result.answers.get(key)
        return answer.probability if answer is not None else None

    def involved(self, threshold: float) -> dict[str, float]:
        return {
            key.split(":", 1)[1]: round(answer.probability, 3)
            for key, answer in self.result.answers.items()
            if key.startswith("involves:")
            and answer.probability is not None
            and answer.probability >= threshold
        }


@dataclass(frozen=True, slots=True)
class CutSegment:
    """One instruction cut from a hold: the L1→L2 handoff."""

    utterance_id: str
    session_id: str
    index: int
    thread: int
    text: str
    start: int
    end: int
    reason: Literal["pause", "release"]
    archetype: str
    archetype_p: dict[str, float]
    table: str | None
    mutation: bool
    involved: dict[str, float]
    ref: int | None
    answers: int | None
    complete: float | None
    chart: bool = False

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(slots=True)
class _Pause:
    """One pause. A resume freezes the span it decides on; new words go to the next segment."""

    started: float
    resumed_end: int | None = None
    resumed_at: float | None = None
    resumed: asyncio.Event = field(default_factory=asyncio.Event)

    def silence_s(self) -> float:
        return (self.resumed_at or time.monotonic()) - self.started


@dataclass(slots=True)
class HoldLoop:
    utterance_id: str
    session_id: str
    model: DecisionModel
    threads: ThreadRegistry
    publish: Publish
    settings: HoldSettings = field(default_factory=HoldSettings)
    segments: list[CutSegment] = field(default_factory=list)
    _words: list[str] = field(default_factory=list)
    _stable: int = 0
    _cut: int = 0
    _called_end: int = 0
    _latest: Decision | None = None
    _calling: asyncio.Lock = field(default_factory=asyncio.Lock)
    _routine: asyncio.Task[None] | None = None
    _pause: asyncio.Task[None] | None = None
    _pause_state: _Pause | None = None
    _paused_at: float | None = None
    _seq: int = 0
    _closed: bool = False
    _cutting: bool = False
    _retry: bool = False

    # --- inputs from L0 ------------------------------------------------------------------------

    def on_text(self, text: str, stable_words: int) -> None:
        if self._closed:
            return
        self._words = text.split()
        self._stable = min(stable_words, len(self._words))
        self._pump()

    def on_pause(self, silence_ms: float) -> None:
        if self._closed:
            return
        self._paused_at = time.monotonic() - silence_ms / 1_000
        state = _Pause(self._paused_at)
        self._pause_state = state
        self._pause = asyncio.create_task(self._pause_flow(state, self._pause))

    def on_resume(self) -> None:
        self._paused_at = None
        self._resume_pause()

    def _resume_pause(self) -> None:
        """Freeze the current pause at the words heard so far; a write's long wait ends here."""

        state = self._pause_state
        if state is not None and state.resumed_end is None:
            state.resumed_end = len(self._words)
            state.resumed_at = time.monotonic()
            state.resumed.set()

    async def release(self, text: str) -> list[CutSegment]:
        """Cut everything not yet cut, using L0's streamed text, and close the loop."""

        self._closed = True
        self._paused_at = None
        self._resume_pause()
        self._words = text.split()
        self._stable = len(self._words)
        for task in (self._pause, self._routine):
            if task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        if len(self._words) > self._cut:
            decision = await self._final_decision("release")
            await self._cut_segment(decision, "release")
        return list(self.segments)

    def cancel(self) -> None:
        self._closed = True
        if self._pause is not None and not self._pause.done() and not self._cutting:
            self._pause.cancel()
        if self._routine is not None:
            self._routine.cancel()

    # --- pacing --------------------------------------------------------------------------------

    def _pump(self) -> None:
        """Start a routine call on the newest stable words if the budget allows it."""

        if self._closed or self._calling.locked() or self._paused_at is not None:
            return
        if self._routine is not None and not self._routine.done():
            return  # created but not yet holding the lock
        if self._stable <= max(self._cut, self._called_end):
            return
        spare = getattr(self.model, "can_spare", None)
        if spare is not None and not spare(self.settings.reserve_s):
            if not self._retry:
                self._retry = True
                asyncio.get_running_loop().call_later(0.1, self._retry_pump)
            return
        self._routine = asyncio.create_task(self._routine_call())

    def _retry_pump(self) -> None:
        self._retry = False
        self._pump()

    async def _routine_call(self) -> None:
        async with self._calling:
            await self._decide("word", self._cut, self._stable)
        asyncio.get_running_loop().call_soon(self._pump)

    # --- pauses --------------------------------------------------------------------------------

    async def _pause_flow(self, state: _Pause, previous: asyncio.Task[None] | None) -> None:
        if previous is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await previous  # the earlier pause may still cut its own span first
        await asyncio.sleep(self.settings.settle_ms / 1_000)
        for _ in range(3):
            # Until speech resumes, late ASR text still belongs to this pause; after, it does not.
            end = state.resumed_end if state.resumed_end is not None else len(self._words)
            if end <= self._cut:
                return
            decision = await self._final_decision("pause", end)
            if decision is None:
                return
            if state.resumed_end is None and decision.end < len(self._words):
                continue  # late ASR text arrived during the call; decide again on all of it
            if state.resumed_end is not None and decision.end != state.resumed_end:
                continue  # speech resumed during the call; decide on the words before it
            if decision.reading.archetype == "not_a_command":
                await self._cut_segment(decision, "pause")
                return
            complete = decision.noul("complete") or 0.0
            # A reply to a waiting question ("the bigger one") is complete by nature.
            if complete < self.settings.complete_threshold and decision.reading.answers is None:
                return
            if decision.reading.mutation:
                # A write needs the longer silence. Speaking again (or releasing) before it is
                # reached cancels the cut; a decision that merely arrives late does not.
                needed = self.settings.write_pause_ms / 1_000
                wait = needed - state.silence_s()
                if wait > 0 and state.resumed_end is None:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(state.resumed.wait(), timeout=wait)
                if state.silence_s() < needed - 0.01:
                    return
                if state.resumed_end is None and len(self._words) != decision.end:
                    continue
            await self._cut_segment(decision, "pause")
            return

    async def _final_decision(self, kind: DecisionKind, end: int | None = None) -> Decision | None:
        """A decision on the words since the cut (up to ``end``), reusing the latest if it fits."""

        async with self._calling:
            end = len(self._words) if end is None else min(end, len(self._words))
            latest = self._latest
            if latest is not None and latest.start == self._cut and latest.end == end:
                return latest
            if end <= self._cut:
                return None
            return await self._decide(kind, self._cut, end)

    # --- decisions and cuts --------------------------------------------------------------------

    async def _decide(self, kind: DecisionKind, start: int, end: int) -> Decision | None:
        text = " ".join(self._words[start:end])
        # Only the previous thread is offered: a follow-up always continues the latest request.
        threads = self.threads.threads(self.session_id, 1)
        waiting = self.threads.waiting(self.session_id)
        questions = build_questions(involvement=True, threads=threads, waiting=waiting)
        self._called_end = max(self._called_end, end)
        try:
            result = await self.model.decide(build_state(text, threads, waiting), questions)
        except Exception as error:  # noqa: BLE001 - a skipped decision never stops capture
            await self._emit(
                {
                    "type": "decision_skipped",
                    "kind": kind,
                    "span": [start, end],
                    "error": f"{type(error).__name__}: {error}"[:300],
                }
            )
            return None
        decision = Decision(kind, start, end, text, result, interpret(result, threads, waiting))
        if start == self._cut:
            self._latest = decision
        await self._emit(self._decision_event(decision))
        return decision

    def _decision_event(self, decision: Decision) -> dict[str, Any]:
        archetype = decision.result.answers.get("archetype")
        reading = decision.reading
        return {
            "type": "decision",
            "kind": decision.kind,
            "span": [decision.start, decision.end],
            "text": decision.text,
            "archetype": reading.archetype,
            "archetype_p": _top(archetype.probabilities if archetype else {}, 4),
            "table": reading.table,
            "involved": decision.involved(self.settings.schema_threshold),
            "lit": sorted(decision.involved(self.settings.ui_threshold)),
            "mutation": reading.mutation,
            "chart": reading.chart,
            "complete": _round(decision.noul("complete")),
            "ref": reading.ref,
            "answers": reading.answers,
            "latency_ms": round(decision.result.latency_ms, 1),
            "source": decision.result.source,
        }

    async def _cut_segment(
        self, decision: Decision | None, reason: Literal["pause", "release"]
    ) -> None:
        self._cutting = True
        try:
            await self._cut_segment_now(decision, reason)
        finally:
            self._cutting = False
        self._pump()

    async def _cut_segment_now(
        self, decision: Decision | None, reason: Literal["pause", "release"]
    ) -> None:
        start = self._cut
        # A pause cuts at its decision's span; words spoken after the pause start the next one.
        end = decision.end if reason == "pause" and decision is not None else len(self._words)
        text = " ".join(self._words[start:end])
        self._cut = end
        self._latest = None
        if decision is not None and decision.reading.archetype == "not_a_command":
            await self._emit({"type": "segment_dropped", "span": [start, end], "text": text})
            return
        if decision is None or decision.start != start or decision.end != end:
            reading = Reading("cannot_tell", None, False, None)
            archetype_p: dict[str, float] = {}
            involved: dict[str, float] = {}
            complete = None
        else:
            reading = decision.reading
            answer = decision.result.answers.get("archetype")
            archetype_p = _top(answer.probabilities if answer else {}, 4)
            involved = decision.involved(self.settings.schema_threshold)
            complete = _round(decision.noul("complete"))
        thread = reading.answers
        if thread is not None:
            try:
                self.threads.answered(self.session_id, thread)
            except KeyError:
                thread = None
        if thread is None:
            thread = self.threads.open(
                self.session_id,
                text=text,
                archetype=reading.archetype,
                table=reading.table,
                mutation=reading.mutation,
                utterance_id=self.utterance_id,
            )
        segment = CutSegment(
            utterance_id=self.utterance_id,
            session_id=self.session_id,
            index=len(self.segments),
            thread=thread,
            text=text,
            start=start,
            end=end,
            reason=reason,
            archetype=reading.archetype or "cannot_tell",
            archetype_p=archetype_p,
            table=reading.table,
            mutation=reading.mutation,
            involved=involved,
            ref=reading.ref,
            answers=reading.answers,
            complete=complete,
            chart=reading.chart,
        )
        self.segments.append(segment)
        await self._emit({"type": "segment_cut", "segment": segment.to_json()})

    async def _emit(self, event: dict[str, Any]) -> None:
        event = {
            "schema_version": 1,
            "utterance_id": self.utterance_id,
            "seq": self._seq,
            "mono_ms": time.monotonic() * 1_000,
            **event,
        }
        self._seq += 1
        await self.publish(event)


def _top(probabilities: dict[str, float], n: int) -> dict[str, float]:
    ranked = sorted(probabilities.items(), key=lambda item: item[1], reverse=True)[:n]
    return {key: round(value, 3) for key, value in ranked}


def _round(value: float | None) -> float | None:
    return round(value, 3) if value is not None else None
