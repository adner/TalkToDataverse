"""Session-scoped thread numbering and waiting questions, shared by L1 and L2.

L1 numbers a thread when it cuts a segment, mid-hold, before L3 hears about it. L2 registers the
question a thread is waiting on so L1 can route a spoken answer back to it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .decision_questions import OpenThread, WaitingQuestion


@dataclass(slots=True)
class ThreadRecord:
    number: int
    text: str
    archetype: str | None
    table: str | None
    mutation: bool
    utterance_id: str
    question: str | None = None


@dataclass(slots=True)
class _Session:
    threads: list[ThreadRecord] = field(default_factory=list)


class ThreadRegistry:
    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}

    def open(
        self,
        session_id: str,
        *,
        text: str,
        archetype: str | None,
        table: str | None,
        mutation: bool,
        utterance_id: str,
    ) -> int:
        session = self._sessions.setdefault(session_id, _Session())
        number = len(session.threads) + 1
        session.threads.append(ThreadRecord(number, text, archetype, table, mutation, utterance_id))
        return number

    def threads(self, session_id: str, limit: int | None = None) -> list[OpenThread]:
        records = self._records(session_id)
        if limit is not None:
            records = records[-limit:]
        return [OpenThread(r.number, r.text, r.archetype, r.table, r.mutation) for r in records]

    def waiting(self, session_id: str) -> list[WaitingQuestion]:
        return [
            WaitingQuestion(r.number, r.question)
            for r in self._records(session_id)
            if r.question is not None
        ]

    def ask(self, session_id: str, number: int, question: str) -> None:
        self._record(session_id, number).question = question

    def answered(self, session_id: str, number: int) -> None:
        self._record(session_id, number).question = None

    def reset(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)

    def _records(self, session_id: str) -> list[ThreadRecord]:
        session = self._sessions.get(session_id)
        return list(session.threads) if session is not None else []

    def _record(self, session_id: str, number: int) -> ThreadRecord:
        for record in self._records(session_id):
            if record.number == number:
                return record
        raise KeyError(f"thread {number} is not open in session {session_id!r}")
