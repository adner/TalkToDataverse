"""Text-fragment assembly, word offsets, and degraded timing estimates."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .contracts import Word

_COMPLETE_WORD = re.compile(r"(\S+)\s+")


@dataclass(frozen=True, slots=True)
class AssemblyDelta:
    words: tuple[Word, ...]
    tail: str
    stable_text: str
    text: str
    changed: bool


@dataclass(slots=True)
class TextAssembler:
    frame_ms: int = 80
    _words: list[Word] = field(default_factory=list)
    _pending: str = ""
    _last_tail: str = ""

    @property
    def words(self) -> tuple[Word, ...]:
        return tuple(self._words)

    @property
    def stable_text(self) -> str:
        return " ".join(word.w for word in self._words)

    @property
    def tail(self) -> str:
        return self._pending.strip()

    @property
    def text(self) -> str:
        return _join_stable_tail(self.stable_text, self.tail)

    def append(self, fragment: str, audio_ms: float) -> AssemblyDelta:
        self._pending += fragment
        complete: list[str] = []
        consumed = 0
        for match in _COMPLETE_WORD.finditer(self._pending):
            complete.append(match.group(1))
            consumed = match.end()
        if consumed:
            self._pending = self._pending[consumed:]
        new_words = self._commit(complete, audio_ms)
        return self._delta(new_words)

    def update(self, snapshot: str, audio_ms: float) -> AssemblyDelta:
        """Apply a cumulative decoder snapshot while keeping completed words append-only."""

        complete = [match.group(1) for match in _COMPLETE_WORD.finditer(snapshot)]
        committed = [word.w for word in self._words]
        if complete[: len(committed)] != committed:
            # A decoder should not revise text after a word boundary. Preserve the public
            # append-only contract if a tokenizer nevertheless changes an older token.
            return self._delta(())

        new_words = self._commit(complete[len(committed) :], audio_ms)
        last_boundary = max((match.end() for match in _COMPLETE_WORD.finditer(snapshot)), default=0)
        self._pending = snapshot[last_boundary:]
        return self._delta(new_words)

    def flush(self, audio_ms: float) -> AssemblyDelta:
        pending = self._pending.strip()
        self._pending = ""
        new_words = self._commit([pending] if pending else [], audio_ms)
        return self._delta(new_words)

    def _commit(self, values: list[str], audio_ms: float) -> tuple[Word, ...]:
        if not values:
            return ()
        end_ms = max(self.frame_ms, math.ceil(audio_ms / self.frame_ms) * self.frame_ms)
        available_start = max(0, end_ms - self.frame_ms * len(values))
        stable = self.stable_text
        created: list[Word] = []
        for value in values:
            char_start = len(stable) + (1 if stable else 0)
            char_end = char_start + len(value)
            word = Word(
                i=len(self._words),
                w=value,
                cs=char_start,
                ce=char_end,
                t0=available_start,
                t1=min(end_ms, available_start + self.frame_ms),
                conf=None,
            )
            self._words.append(word)
            created.append(word)
            stable = _join_stable_tail(stable, value)
            available_start = word.t1 or available_start
        return tuple(created)

    def _delta(self, words: tuple[Word, ...]) -> AssemblyDelta:
        tail = self.tail
        changed = bool(words) or tail != self._last_tail
        self._last_tail = tail
        stable_text = self.stable_text
        return AssemblyDelta(
            words=words,
            tail=tail,
            stable_text=stable_text,
            text=_join_stable_tail(stable_text, tail),
            changed=changed,
        )


def words_from_text(text: str, audio_duration_ms: int, frame_ms: int = 80) -> tuple[Word, ...]:
    tokens = text.split()
    if not tokens:
        return ()
    duration = max(frame_ms, math.ceil(audio_duration_ms / frame_ms) * frame_ms)
    frames_per_word = max(1, duration // frame_ms // len(tokens))
    words: list[Word] = []
    stable = ""
    cursor_ms = 0
    for index, token in enumerate(tokens):
        char_start = len(stable) + (1 if stable else 0)
        char_end = char_start + len(token)
        t1 = (
            duration
            if index == len(tokens) - 1
            else min(duration, cursor_ms + frames_per_word * frame_ms)
        )
        words.append(
            Word(i=index, w=token, cs=char_start, ce=char_end, t0=cursor_ms, t1=t1, conf=None)
        )
        stable = _join_stable_tail(stable, token)
        cursor_ms = t1
    return tuple(words)


def _join_stable_tail(stable: str, tail: str) -> str:
    if not stable:
        return tail
    if not tail:
        return stable
    return f"{stable} {tail}"
