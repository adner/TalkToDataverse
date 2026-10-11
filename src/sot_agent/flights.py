"""L2 flights: gpt-6-luna executing one cut segment with the typed Contoso Bikes record tools.

One flight runs per ``segment_cut``. It gets L1's reading and the schemas of the involved tables
in its prompt, calls the record tools, and may ask the operator one question at a time. A spoken
answer is routed back by L1 (``answers_question``); a typed one comes from the flight card.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import nullcontext, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sot_dataverse.schema import Schema
from sot_dataverse.tools import RecordApp, RecordTools, describe
from sot_intent.decision_questions import ARCHETYPES, TABLES
from sot_intent.threads import ThreadRegistry

from .config import AgentConfig
from .thread_context import THREAD_CONTEXT_TOOL_NAME, ThreadContextBroker

MAX_SCHEMAS = 4
# Luna occasionally leaks a raw control token such as <|fim_suffix|> into its text.
CONTROL_TOKEN = re.compile(r"<\|[a-z_]+\|>")
CONTEXT_RESULT_CHARS = 6_000
CONTEXT_RECORDS = 60

INSTRUCTIONS = """\
You are L2 of a voice demo. An operator speaks requests about the Contoso Bikes Dataverse \
environment (an e-bike maker; money is in SEK). A fast intent model (L1) has already cut the \
speech into one request and guessed what it is about. You carry out that request with the \
tools, then answer.

Tables:
{tables}

How to work:
- Use the tools. Never invent records, ids, names or numbers.
- A where is one condition {{"column", "op", "value"}}, a list of conditions that must all hold, \
or {{"all": [...]}} / {{"any": [...]}}. Choice columns take their label. A lookup column accepts \
a record name or id. A column may follow one lookup, such as "cb_accountid.name".
- Select only the columns you need. Use count_records for how many, totals and averages.
- When the operator asks for a chart or graph, call show_chart once with the grouping that \
answers the request (group by a date column with a grain for trends over time). Use the type \
the operator named, otherwise leave it out. A chart of an earlier thread's records reuses its \
filter.
- Spoken names are often partial ("UrbanPost" for "UrbanPost Delivery"). If a result has a hint, \
follow it before answering; never report zero for a name you did not find.
- "Open" or "pending" means not in a final status: leave out records that are resolved, \
rejected, closed, cancelled, delivered, received or credited.
- Only when the request depends on an earlier thread ("those", "them", "that order", "thread \
2"), call get_thread_context for that earlier thread and reuse its filter or record ids. Its \
based_on holds the thread that one built on: "their" or "it" may point there. Never call it for \
the current thread.
- Writes are approved: do them directly. update_records and delete_records need ids or a where.
- Call ask_operator only when a missing or ambiguous fact changes the result, for example two \
customers match a name. Ask one short question and wait for the answer.
- If a tool returns an error, correct the arguments and try again.
- For exports use export_csv and give the file path.
- To open, show or take the operator to a record in the app, call open_record once. If several \
records match, ask which one.

Answer in one to three short sentences for a presenter to read aloud: the result first, with the \
key numbers and names, in plain text without Markdown. Use a short list only for up to five \
records. Do not offer follow-ups.\
"""

WHERE = {
    "description": (
        'A condition {"column": ..., "op": ..., "value": ...}, a list of conditions (all must '
        'hold), or {"all": [...]} / {"any": [...]} groups. Ops: eq, ne, gt, ge, lt, le, '
        "contains, startswith, in (value is a list), null, not_null, on_or_after, on_or_before, "
        "last_n_days, next_n_days."
    ),
    "anyOf": [{"type": "object"}, {"type": "array", "items": {"type": "object"}}],
}
TABLE = {"type": "string", "description": "Table logical name, such as cb_salesorder."}
COLUMNS = {"type": "array", "items": {"type": "string"}, "description": "Column logical names."}
EXPAND = {
    "type": "array",
    "items": {"type": "string"},
    "description": "Lookups to expand one level, such as cb_accountid(name,cb_customertier).",
}
IDS = {"type": "array", "items": {"type": "string"}, "description": "Record ids (GUIDs)."}
VALUES = {
    "type": "object",
    "description": "Column logical name to value. Choices take labels; lookups a name or id.",
}
GRAIN = {
    "type": "string",
    "enum": ["month", "week"],
    "description": "Bucket size when group_by is a date column; default month.",
}
ORDERBY = {
    "anyOf": [{"type": "string"}, {"type": "array", "items": {"type": "string"}}],
    "description": "'column asc' or 'column desc'.",
}

RECORD_TOOLS: dict[str, tuple[str, dict[str, Any], list[str]]] = {
    "query_records": (
        "List or search records (up to 200; matched gives the full count).",
        {
            "table": TABLE,
            "select": COLUMNS,
            "where": WHERE,
            "expand": EXPAND,
            "orderby": ORDERBY,
            "top": {"type": "integer", "description": "Rows to return, default 50."},
        },
        ["table"],
    ),
    "count_records": (
        "Count records, or sum/average/min/max a numeric column, optionally grouped by a column.",
        {
            "table": TABLE,
            "where": WHERE,
            "group_by": {"type": "string"},
            "measure": {"type": "string", "description": "Numeric column for sum/average/min/max."},
            "fn": {"type": "string", "enum": ["count", "sum", "average", "min", "max"]},
            "grain": GRAIN,
        },
        ["table"],
    ),
    "show_chart": (
        "Show a bar, donut (pie) or line chart on the flight card, built from a grouped count or "
        "sum/average/min/max. Omit type unless the operator named one; it then picks a line for "
        "dates, a donut for up to six groups and bars otherwise. Never ask which type.",
        {
            "table": TABLE,
            "group_by": {"type": "string", "description": "Column to group by (one per bar)."},
            "type": {"type": "string", "enum": ["bar", "donut", "pie", "line"]},
            "title": {"type": "string", "description": "A short chart title."},
            "where": WHERE,
            "measure": {"type": "string", "description": "Numeric column for sum/average/min/max."},
            "fn": {"type": "string", "enum": ["count", "sum", "average", "min", "max"]},
            "grain": GRAIN,
        },
        ["table", "group_by"],
    ),
    "get_record": (
        "Get one record by id, or by a where that should match exactly one record.",
        {"table": TABLE, "record_id": {"type": "string"}, "where": WHERE, "expand": EXPAND},
        ["table"],
    ),
    "open_record": (
        "Open one record in the business app, in a dialog on the operator's screen. Find it by id "
        "or by a where that matches exactly one record.",
        {"table": TABLE, "record_id": {"type": "string"}, "where": WHERE},
        ["table"],
    ),
    "create_record": (
        "Create one record.",
        {"table": TABLE, "values": VALUES},
        ["table", "values"],
    ),
    "update_records": (
        "Update one or many records, chosen by ids or by a where.",
        {"table": TABLE, "values": VALUES, "ids": IDS, "where": WHERE},
        ["table", "values"],
    ),
    "delete_records": (
        "Delete one or many records, chosen by ids or by a where.",
        {"table": TABLE, "ids": IDS, "where": WHERE},
        ["table"],
    ),
    "export_csv": (
        "Export matching records to a CSV file under exports/.",
        {
            "table": TABLE,
            "select": COLUMNS,
            "where": WHERE,
            "expand": EXPAND,
            "orderby": ORDERBY,
            "name": {"type": "string", "description": "A short file name."},
        },
        ["table"],
    ),
    "describe_table": (
        "Columns, types, choice labels and lookups of a table not described in the request.",
        {"table": TABLE},
        ["table"],
    ),
}


@dataclass(frozen=True, slots=True)
class ToolStarted:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ToolFinished:
    call_id: str
    name: str
    result: Any
    duration_ms: float


@dataclass(frozen=True, slots=True)
class TextDelta:
    delta: str


@dataclass(frozen=True, slots=True)
class QuestionAsked:
    question: str


@dataclass(frozen=True, slots=True)
class QuestionAnswered:
    answer: str | None


@dataclass(frozen=True, slots=True)
class FlightFinished:
    text: str
    tool_calls: list[dict[str, Any]]


FlightEvent = (
    ToolStarted | ToolFinished | TextDelta | QuestionAsked | QuestionAnswered | FlightFinished
)


@dataclass(slots=True)
class _Pending:
    question: str
    future: asyncio.Future[str]


class QuestionBroker:
    """Questions flights are waiting on, keyed by presentation session and thread number."""

    def __init__(
        self,
        threads: ThreadRegistry,
        *,
        timeout_s: float = 300,
        publish: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> None:
        self.threads = threads
        self.timeout_s = timeout_s
        self.publish = publish
        self._pending: dict[tuple[str, int], _Pending] = {}

    async def ask(self, session_id: str, thread: int, question: str) -> str | None:
        """Wait for the operator's answer; ``None`` when nobody answers in time."""

        key = (session_id, thread)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending[key] = _Pending(question, future)
        # Typed or scripted flights may have no L1 thread to route a spoken answer to.
        with suppress(KeyError):
            self.threads.ask(session_id, thread, question)
        await self._publish("question_asked", session_id, thread, question=question)
        try:
            return await asyncio.wait_for(future, timeout=self.timeout_s)
        except TimeoutError:
            return None
        finally:
            if self._pending.get(key) is not None and self._pending[key].future is future:
                del self._pending[key]
            with suppress(KeyError):
                self.threads.answered(session_id, thread)
            await self._publish("question_closed", session_id, thread)

    def answer(self, session_id: str, thread: int, text: str) -> bool:
        pending = self._pending.get((session_id, thread))
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(text)
        return True

    def question(self, session_id: str, thread: int) -> str | None:
        pending = self._pending.get((session_id, thread))
        return pending.question if pending is not None and not pending.future.done() else None

    def pending(self, session_id: str) -> list[dict[str, Any]]:
        return [
            {"thread": thread, "question": pending.question}
            for (session, thread), pending in self._pending.items()
            if session == session_id
        ]

    async def _publish(self, kind: str, session_id: str, thread: int, **data: Any) -> None:
        if self.publish is not None:
            await self.publish({"type": kind, "session_id": session_id, "thread": thread, **data})


@dataclass(slots=True)
class _Scope:
    queue: asyncio.Queue[Any]
    session_id: str
    thread: int
    calls: list[dict[str, Any]] = field(default_factory=list)


_FLIGHT: ContextVar[_Scope | None] = ContextVar("sot_flight", default=None)
_DONE = object()


@dataclass(frozen=True, slots=True)
class _Failed:
    error: BaseException


def handoff_prompt(segment: dict[str, Any], schema: Schema) -> str:
    """The user message for one flight: the words, L1's reading and the likely tables' schemas."""

    thread = int(segment.get("thread") or 0)
    lines = [f'The operator said: "{segment.get("text", "")}"']
    archetype = str(segment.get("archetype") or "cannot_tell")
    if archetype in ARCHETYPES and archetype != "cannot_tell":
        p = (segment.get("archetype_p") or {}).get(archetype)
        reading = f"L1 reading (may be wrong): {archetype}"
        reading += f" (p {p:.2f})" if isinstance(p, int | float) else ""
        if segment.get("table"):
            reading += f", table {segment['table']}"
        reading += ", a write." if segment.get("mutation") else ", read-only."
        lines.append(reading)
    else:
        lines.append("L1 could not classify this request; work out what is meant.")
    if segment.get("ref"):
        lines.append(
            f"It refers to thread {segment['ref']}: call get_thread_context({segment['ref']}) "
            "before acting."
        )
    elif thread > 1:
        lines.append(
            f"This is thread {thread}. Earlier threads 1 to {thread - 1} exist; L1 found no "
            "reference to them."
        )
    else:
        lines.append("This is the first thread; there are no earlier threads.")
    names = [segment["table"]] if segment.get("table") else []
    involved = segment.get("involved") or {}
    names += sorted(involved, key=lambda name: -float(involved[name]))
    described = [
        describe(schema.tables[name]) for name in dict.fromkeys(names) if name in schema.tables
    ][:MAX_SCHEMAS]
    if described:
        lines.append("Schemas of the likely tables:")
        lines.append(json.dumps(described, ensure_ascii=False, separators=(",", ":")))
    return "\n".join(lines)


def _compact(result: Any) -> Any:
    """Bound a tool result for another thread's context, keeping record ids and names."""

    if len(json.dumps(result, default=str)) <= CONTEXT_RESULT_CHARS or not isinstance(result, dict):
        return result
    compact = dict(result)
    records = compact.get("records")
    if isinstance(records, list):
        compact["records"] = [
            {
                key: value
                for key, value in record.items()
                if key == "id" or not isinstance(value, dict | list)
            }
            for record in records[:CONTEXT_RECORDS]
        ]
        if len(json.dumps(compact, default=str)) > CONTEXT_RESULT_CHARS:
            compact["records"] = [{"id": r.get("id")} for r in records[:CONTEXT_RECORDS]]
        if len(records) > CONTEXT_RECORDS:
            compact["records_omitted"] = len(records) - CONTEXT_RECORDS
    return compact


@dataclass(slots=True)
class FlightRuntime:
    """Owns the Luna agent and runs flights concurrently, one per cut segment."""

    model: str
    endpoint: str
    api_key: str
    records: RecordTools
    questions: QuestionBroker
    thread_context: ThreadContextBroker | None = None
    reasoning_effort: str | None = "low"
    max_tool_calls: int = 16
    _agent: Any = field(default=None, init=False, repr=False)

    @property
    def schema(self) -> Schema:
        return self.records.schema

    async def close(self) -> None:
        await self.records.api.close()

    async def warm(self) -> None:
        """Build the agent and fetch the Dataverse token now, not on the first flight's call."""

        try:
            if self._agent is None:
                self._agent = self._build()
            await self.records.api.tokens.token()
        except Exception:  # noqa: BLE001 - the first flight reports the same failure
            return

    def _build(self) -> Any:
        from agent_framework import Agent, FunctionTool
        from agent_framework.openai import OpenAIChatClient

        tools = [
            FunctionTool(
                name=name,
                description=description,
                func=self._record_tool(name),
                approval_mode="never_require",
                input_model={
                    "type": "object",
                    "properties": properties,
                    "required": required,
                    "additionalProperties": False,
                },
            )
            for name, (description, properties, required) in RECORD_TOOLS.items()
        ]
        tools.append(
            FunctionTool(
                name="ask_operator",
                description=(
                    "Ask the operator one short question and wait for the spoken or typed answer."
                ),
                func=self._ask_operator,
                approval_mode="never_require",
                input_model={
                    "type": "object",
                    "properties": {"question": {"type": "string"}},
                    "required": ["question"],
                    "additionalProperties": False,
                },
            )
        )
        if self.thread_context is not None:
            tools.append(
                FunctionTool(
                    name=THREAD_CONTEXT_TOOL_NAME,
                    description=(
                        "The request, answer and tool calls (with their filters and record ids) "
                        "of an earlier numbered thread. Waits if that thread is still running."
                    ),
                    func=self._traced(THREAD_CONTEXT_TOOL_NAME, self._thread_context),
                    approval_mode="never_require",
                    input_model={
                        "type": "object",
                        "properties": {"thread_number": {"type": "integer", "minimum": 1}},
                        "required": ["thread_number"],
                        "additionalProperties": False,
                    },
                )
            )
        client = OpenAIChatClient(
            model=self.model,
            api_key=self.api_key,
            base_url=self.endpoint,
            default_headers={"api-key": self.api_key},
        )
        options: dict[str, Any] = {"max_tool_calls": self.max_tool_calls}
        if self.reasoning_effort is not None:
            options["reasoning"] = {"effort": self.reasoning_effort}
        catalogue = "\n".join(f"- {name}: {about}" for name, about in TABLES.items())
        return Agent(
            client,
            name="sot-l2",
            instructions=INSTRUCTIONS.format(tables=catalogue),
            tools=tools,
            default_options=options,
        )

    def _traced(
        self, name: str, func: Callable[..., Awaitable[Any]]
    ) -> Callable[..., Awaitable[Any]]:
        async def call(**arguments: Any) -> Any:
            scope = _FLIGHT.get()
            call_id = f"call_{uuid.uuid4().hex[:12]}"
            if scope is not None:
                await scope.queue.put(ToolStarted(call_id, name, arguments))
            started = time.perf_counter()
            result = await func(**arguments)
            elapsed = (time.perf_counter() - started) * 1_000
            if scope is not None:
                scope.calls.append({"tool": name, "arguments": arguments, "result": result})
                await scope.queue.put(ToolFinished(call_id, name, result, elapsed))
            return result

        return call

    def _record_tool(self, name: str) -> Callable[..., Awaitable[Any]]:
        async def run(**arguments: Any) -> dict[str, Any]:
            if "table" in arguments:
                arguments["table_name"] = arguments.pop("table")
            return await self.records.call(name, arguments)

        return self._traced(name, run)

    async def _thread_context(self, thread_number: int) -> dict[str, Any]:
        """Wait for an earlier thread, unless it is itself waiting on the operator."""

        assert self.thread_context is not None
        scope = _FLIGHT.get()
        waiting = asyncio.create_task(self.thread_context.get_thread_context(thread_number))
        while not waiting.done():
            question = self.questions.question(scope.session_id, thread_number) if scope else None
            if question is not None:
                waiting.cancel()
                return {
                    "status": "awaiting_input",
                    "thread_number": thread_number,
                    "question": question,
                    "message": "That thread is waiting for the operator's answer and has no "
                    "result yet. Do not guess; say what you are waiting for.",
                }
            await asyncio.wait({waiting}, timeout=0.2)
        return waiting.result()

    async def _ask_operator(self, question: str) -> dict[str, Any]:
        scope = _FLIGHT.get()
        if scope is None:
            return {"answer": None, "note": "No operator is attached to this run."}
        question = CONTROL_TOKEN.sub("", question).strip()
        await scope.queue.put(QuestionAsked(question))
        answer = await self.questions.ask(scope.session_id, scope.thread, question)
        await scope.queue.put(QuestionAnswered(answer))
        if answer is None:
            return {"answer": None, "note": "The operator did not answer. Say what you need."}
        return {"answer": answer}

    async def run(
        self,
        segment: dict[str, Any],
        *,
        session_id: str,
        operation_id: str = "",
        run_id: str = "",
    ) -> AsyncIterator[FlightEvent]:
        """Execute one segment, yielding tool, question and text events, then ``FlightFinished``."""

        if self._agent is None:
            self._agent = self._build()
        thread = int(segment.get("thread") or 0)
        prompt = handoff_prompt(segment, self.schema)
        queue: asyncio.Queue[Any] = asyncio.Queue()
        scope = _Scope(queue, session_id, thread)
        if self.thread_context is not None and thread:
            self.thread_context.register(session_id, thread, operation_id or str(uuid.uuid4()))
        context = (
            self.thread_context.activate(
                session_id=session_id,
                thread_number=thread,
                operation_id=operation_id,
                run_id=run_id,
            )
            if self.thread_context is not None and thread
            else nullcontext()
        )
        token = _FLIGHT.set(scope)
        try:
            with context:
                task = asyncio.create_task(self._drive(prompt, scope))
        finally:
            _FLIGHT.reset(token)
        status = "failed"
        final: FlightFinished | None = None
        error: str | None = None
        try:
            while (event := await queue.get()) is not _DONE:
                if isinstance(event, _Failed):
                    error = str(event.error)
                    raise event.error
                if isinstance(event, FlightFinished):
                    final = event
                yield event
            status = "succeeded"
        except (asyncio.CancelledError, GeneratorExit):
            status = "cancelled"
            raise
        finally:
            if not task.done():
                task.cancel()
            if self.thread_context is not None and thread:
                self.thread_context.finish(
                    session_id,
                    thread,
                    status=status,  # type: ignore[arg-type]
                    context={
                        "thread_number": thread,
                        "utterance": segment.get("text", ""),
                        "refers_to": segment.get("ref"),
                        "response": final.text if final else "",
                        "tool_calls": [
                            {**call, "result": _compact(call["result"])} for call in scope.calls
                        ],
                    },
                    error=error,
                )

    async def _drive(self, prompt: str, scope: _Scope) -> None:
        try:
            stream = self._agent.run(prompt, stream=True)
            async for update in stream:
                for content in getattr(update, "contents", []):
                    if str(getattr(content, "type", "")) == "text":
                        delta = CONTROL_TOKEN.sub("", str(getattr(content, "text", "") or ""))
                        if delta:
                            await scope.queue.put(TextDelta(delta))
            response = await stream.get_final_response()
            await scope.queue.put(
                FlightFinished(CONTROL_TOKEN.sub("", response.text).strip(), scope.calls)
            )
        except Exception as error:  # surfaced to the consumer of run()
            await scope.queue.put(_Failed(error))
        finally:
            await scope.queue.put(_DONE)


def build_flights(
    l2: AgentConfig,
    *,
    threads: ThreadRegistry,
    thread_context: ThreadContextBroker | None = None,
    publish: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    dry_run: bool = False,
    exports_dir: Path | None = None,
    question_timeout_s: float | None = None,
) -> FlightRuntime:
    """The flights-mode L2 from ``[l2]`` settings, against ``dataverse_url`` (else the schema's).

    The bench overrides ``dry_run``, ``exports_dir`` and the question timeout.
    """

    from sot_dataverse.web_api import WebApi

    if not l2.endpoint or not l2.key_file:
        raise ValueError("L2 flights need [l2] endpoint and key_file")
    schema = Schema.load(Path(l2.schema_path))
    if l2.dataverse_url:
        schema.environment = l2.dataverse_url
    app = l2.record_app
    records = RecordTools(
        WebApi(schema.environment, tenant_id=l2.tenant_id),
        schema,
        exports_dir or Path(l2.exports_dir),
        dry_run=dry_run,
        record_app=RecordApp(app.name, app.app_id, tuple(app.tables)) if app else None,
    )
    timeout_s = question_timeout_s or l2.question_timeout_seconds
    return FlightRuntime(
        model=l2.model,
        endpoint=l2.endpoint,
        api_key=Path(l2.key_file).read_text(encoding="utf-8").strip(),
        records=records,
        questions=QuestionBroker(threads, timeout_s=timeout_s, publish=publish),
        thread_context=thread_context,
        reasoning_effort=l2.reasoning_effort,
        max_tool_calls=l2.max_tool_calls,
    )
