"""Provider-neutral client for System One decision models (Microsoft-Decision-1, TypeSafe Jev).

Both providers accept the same request body: a ``state`` plus a map of typed questions, and return
calibrated probabilities per question. Only endpoint, auth header, and model name differ.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

QuestionType = Literal["choice", "noul", "score"]


@dataclass(frozen=True, slots=True)
class DecisionQuestion:
    id: str
    type: QuestionType
    instructions: str
    criteria: dict[str, str | None] | list[str] | None = None

    def to_json(self, rng: random.Random | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"type": self.type, "instructions": self.instructions}
        criteria = self.criteria
        if isinstance(criteria, dict) and self.type == "choice" and rng is not None:
            items = list(criteria.items())
            rng.shuffle(items)
            criteria = dict(items)
        if criteria is not None:
            body["criteria"] = criteria
        return body


@dataclass(frozen=True, slots=True)
class DecisionAnswer:
    type: QuestionType
    choice: str | None = None
    confidence: float | None = None
    probabilities: dict[str, float] = field(default_factory=dict)
    probability: float | None = None
    score: float | None = None

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> DecisionAnswer:
        kind = value.get("type", "choice")
        if kind == "noul":
            return cls(type="noul", probability=float(value["noul"]))
        return cls(
            type=kind,
            choice=value.get("choice"),
            confidence=value.get("confidence"),
            probabilities={str(k): float(v) for k, v in (value.get("probabilities") or {}).items()},
            score=value.get("score"),
        )


@dataclass(frozen=True, slots=True)
class DecisionResult:
    answers: dict[str, DecisionAnswer]
    latency_ms: float
    input_tokens: int | None = None
    source: str = ""


class DecisionModel(Protocol):
    async def decide(
        self, state: str | dict[str, Any], questions: list[DecisionQuestion]
    ) -> DecisionResult: ...


class SystemOneClient:
    """HTTP client for any ``/v1/systemone`` endpoint.

    Foundry uses ``api-key`` auth and the deployment name as ``model``; TypeSafe uses a bearer
    token and the model name.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        api_key: str,
        auth: Literal["api-key", "bearer"] = "api-key",
        timeout_s: float = 5.0,
        shuffle_options: bool = True,
        seed: int | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.model = model
        headers = (
            {"api-key": api_key} if auth == "api-key" else {"Authorization": f"Bearer {api_key}"}
        )
        self._client = httpx.AsyncClient(headers=headers, timeout=timeout_s)
        self._rng = random.Random(seed) if shuffle_options else None

    async def decide(
        self, state: str | dict[str, Any], questions: list[DecisionQuestion]
    ) -> DecisionResult:
        body = {
            "model": self.model,
            "state": state,
            "questions": {q.id: q.to_json(self._rng) for q in questions},
        }
        started = time.perf_counter()
        response = await self._client.post(self.endpoint, json=body)
        latency_ms = (time.perf_counter() - started) * 1_000
        response.raise_for_status()
        payload = response.json()
        answers = {k: DecisionAnswer.from_json(v) for k, v in payload["answers"].items()}
        usage = payload.get("usage") or {}
        return DecisionResult(answers, latency_ms, usage.get("input_tokens"), self.endpoint)

    async def aclose(self) -> None:
        await self._client.aclose()


@dataclass(slots=True)
class _PoolMember:
    model: DecisionModel
    interval_s: float
    next_at: float = 0.0


class DecisionPool:
    """Spreads calls across deployments, each with its own requests-per-minute budget.

    Each call goes to the member whose budget frees up first. A 429 parks that member for the
    ``retry-after`` period and is re-raised so the caller can retry on another member.
    """

    def __init__(self, members: Sequence[tuple[DecisionModel, float]]) -> None:
        if not members:
            raise ValueError("a decision pool needs at least one member")
        self._members = [_PoolMember(model, 60.0 / rpm) for model, rpm in members]
        self._lock = asyncio.Lock()

    def can_spare(self, reserve_s: float) -> bool:
        """Whether a routine call can go now and still leave a call free within ``reserve_s``.

        The live loop keeps that reserve for the decision it makes when the speaker pauses. With a
        single member there is nothing to hold back, so any free member is enough.
        """

        now = asyncio.get_running_loop().time()
        free = [m for m in self._members if m.next_at <= now]
        if not free:
            return False
        taken = min(free, key=lambda m: m.next_at)
        others = [m.next_at for m in self._members if m is not taken]
        return not others or min(others) - now <= reserve_s

    async def decide(
        self, state: str | dict[str, Any], questions: list[DecisionQuestion]
    ) -> DecisionResult:
        loop = asyncio.get_running_loop()
        async with self._lock:
            member = min(self._members, key=lambda m: m.next_at)
            wait = member.next_at - loop.time()
            if wait > 0:
                await asyncio.sleep(wait)
            member.next_at = loop.time() + member.interval_s
        try:
            return await member.model.decide(state, questions)
        except httpx.HTTPStatusError as error:
            if error.response.status_code == 429:
                retry_after = float(error.response.headers.get("retry-after", "10"))
                member.next_at = max(member.next_at, loop.time() + retry_after)
            raise


FOUNDRY_DECISION_DEPLOYMENT = "Decision-1"
TYPESAFE_ENDPOINT = "https://api.typesafe.ai/v1/systemone"


def read_key(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()
