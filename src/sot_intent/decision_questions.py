"""Per-word L1 question set for the Contoso Bikes environment.

Shared by the live L1 loop and the decision bench so both measure the same questions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from .decision import DecisionQuestion, DecisionResult

ARCHETYPES: dict[str, str] = {
    "query.list": "Show, list, find or search several records, including filtered or cross-table "
    "questions such as 'which customers have late orders'.",
    "query.count": "A number or a breakdown: how many, total, sum, average, minimum or maximum "
    "over records, also per group or drawn as a chart, such as 'cases per bike model as a bar "
    "chart' or 'plot the orders per month'.",
    "query.get": "One specific record or one fact about one named record, such as 'show me "
    "Nordic Rental Group' or 'what is the list price of the Cargo E-400'.",
    "data.create": "Create, add, register, raise or open a new record.",
    "data.update": "Change, set, mark, escalate, expedite, prioritise, approve, assign, close or "
    "otherwise modify values on existing records.",
    "data.delete": "Delete or remove records.",
    "data.export": "Export, download or save records to a file such as CSV or Excel.",
    "record.open": "Open a record in the business app, or take the operator to its form, such as "
    "'open Nordic Rental Group', 'open that order' or 'show me that record in the app'. A plain "
    "'show me Nordic Rental Group' asks for an answer instead (query.get).",
    "followup": "Refines or continues an earlier request without stating a new operation, such as "
    "'only the high severity ones', 'what about Berlin', 'sort them by value', a short addition "
    "such as 'and their contacts' or 'and the overdue ones', or an answer to a question the "
    "assistant asked.",
    "not_a_command": "Thinking aloud, filler, a reaction or a comment; not an instruction.",
    "cannot_tell": "Too little has been said to tell yet (for example only 'show me the' or "
    "'and'), or the request is not about reading or changing business records, such as deploying "
    "solutions or changing table design (adding, renaming or removing columns, fields or tables).",
}

TABLES: dict[str, str] = {
    "account": "Customers: bike fleets, rental firms, couriers, dealers, municipalities.",
    "cb_bikemodel": "Bike models in the product catalogue: list price, category, motor, warranty.",
    "cb_salesagreement": "Customer agreements: fleet leases, service contracts, dealer agreements, "
    "renewals, annual value.",
    "cb_salesorder": "Sales orders from customers (SO numbers): order value, status, delivery "
    "risk, priority, promised delivery.",
    "cb_salesorderline": "Lines on a sales order: bike model, quantity, unit price.",
    "cb_registeredbike": "Individual bikes delivered to customers: serial number, in service or "
    "in repair, warranty expiry.",
    "cb_warrantycase": "Warranty and service cases about failing bikes: severity, status, failure "
    "category, failed part, escalation, SLA.",
    "cb_returnauthorization": "Return authorizations (RMAs): parts returned to suppliers, credit "
    "amount.",
    "cb_supplier": "Suppliers and vendors: status, on-time delivery, performance rating, country.",
    "cb_part": "Parts, components and inventory: part number, stock on hand, safety stock, lead "
    "time, supplier.",
    "cb_purchaseorder": "Purchase orders sent to suppliers for parts: status, delays, value.",
    "cb_qualityincident": "Quality incidents on parts or suppliers: severity, root cause, "
    "corrective action.",
    "cb_productionline": "Production lines and plants: utilization, capacity, line status.",
    "cb_productionorder": "Production (manufacturing) orders: status, on hold, hold reason, "
    "missing part, planned dates.",
    "cb_bomline": "Bill of materials: which parts go into which bike model.",
}

NONE = "none"
MUTATING = frozenset({"data.create", "data.update", "data.delete"})
# A segment refers to the previous thread, or asks for a chart, at or above this probability.
REFERS_THRESHOLD = 0.5
CHART_THRESHOLD = 0.5


@dataclass(frozen=True, slots=True)
class OpenThread:
    number: int
    text: str
    archetype: str | None = None
    table: str | None = None
    mutation: bool = False


@dataclass(frozen=True, slots=True)
class WaitingQuestion:
    thread: int
    question: str


NEW_INSTRUCTION = "new_instruction"


def thread_option(number: int) -> str:
    return f"thread_{number}"


def build_state(
    text: str,
    threads: Sequence[OpenThread] = (),
    waiting: Sequence[WaitingQuestion] = (),
) -> str:
    lines = [
        "A business operator is speaking instructions to an assistant for Contoso Bikes, an e-bike "
        "manufacturer. The speech is transcribed live and may stop mid-sentence or mid-word.",
        f'Speech so far: "{text}"',
    ]
    if threads:
        lines.append(f'Previous request: "{threads[-1].text}"')
    if waiting:
        lines.append("Questions the assistant is waiting for the operator to answer:")
        lines += [f'  thread {w.thread}: "{w.question}"' for w in waiting]
    return "\n".join(lines)


def build_questions(
    *,
    involvement: bool = False,
    threads: Sequence[OpenThread] = (),
    waiting: Sequence[WaitingQuestion] = (),
) -> list[DecisionQuestion]:
    """The questions for one call. ``threads`` ends with the previous request, if there is one."""

    questions = [
        DecisionQuestion(
            "archetype",
            "choice",
            "Which kind of operation does the speech so far ask for?",
            dict(ARCHETYPES),
        ),
        DecisionQuestion(
            "complete",
            "noul",
            "Has the operator finished stating one complete instruction, with nothing essential "
            "missing? A short addition to an earlier request counts as complete when it makes "
            "sense together with that request, for example 'and their contacts' or 'only the "
            "overdue ones'. Answer false if the speech stops mid-word or mid-phrase, or obviously "
            "continues (for example it ends with 'the', 'for', 'in', 'of', 'to' or 'and').",
        ),
        DecisionQuestion(
            "primary_table",
            "choice",
            "Which table is the speech mainly about? Choose the records the operator wants to see, "
            "count, export or change.",
            {**TABLES, NONE: "No business table, or not possible to tell yet."},
        ),
        DecisionQuestion(
            "wants_chart",
            "noul",
            "Does the operator ask to see the result as a chart or graph, such as a bar chart, pie "
            "chart, line chart or trend? Answer false for plain lists, numbers and exports.",
        ),
    ]
    if involvement:
        questions += [
            DecisionQuestion(
                f"involves:{table}",
                "noul",
                f"Will answering this request need data from this table? {description}",
            )
            for table, description in TABLES.items()
        ]
    if threads:
        questions.append(
            DecisionQuestion(
                "refers_to_previous",
                "noul",
                "Does the speech continue or depend on the previous request? That includes its "
                "results ('those', 'them', 'that one', 'that record', 'the list'), anything it "
                "mentioned ('their', 'it', 'they', 'there', 'that incident'), and short additions "
                "such as 'and Velotech too' or 'as a pie chart'. Answer false for a new, "
                "self-contained request that names what it is about, even when it starts with "
                "'and' or is about the same table, such as 'and list the production lines' or "
                "'and when does Nordic Rental's lease renew'.",
            )
        )
    if waiting:
        questions.append(
            DecisionQuestion(
                "answers_question",
                "choice",
                "Is the speech a reply to one of the assistant's waiting questions? Short replies "
                "that pick an option, name a record or say yes or no usually are, for example "
                "'the second one', 'yes, go ahead' or a customer name.",
                {
                    **{thread_option(w.thread): f'Answers: "{w.question}"' for w in waiting},
                    NEW_INSTRUCTION: "No, it is a new instruction or something else.",
                },
            )
        )
    return questions


@dataclass(frozen=True, slots=True)
class Reading:
    """What L1 concludes from one decision call, after the rules applied in code."""

    archetype: str | None
    table: str | None
    mutation: bool
    ref: int | None
    answers: int | None = None
    chart: bool = False


def _thread_number(choice: str | None) -> int | None:
    if choice and choice.startswith("thread_"):
        return int(choice.removeprefix("thread_"))
    return None


def interpret(
    result: DecisionResult,
    threads: Sequence[OpenThread],
    waiting: Sequence[WaitingQuestion] = (),
) -> Reading:
    """Derive the routing fields that the model is not asked directly.

    A segment only ever refers to the previous thread (the last of ``threads``): when the model
    says it depends on it, or when it is a follow-up. Mutation follows from the archetype; a
    follow-up inherits it, and the table when the model names none, from the previous thread. A
    follow-up that asks for a chart is a read. A follow-up (or unclassifiable fragment) that refers
    to a thread waiting on a question answers that question: "the delivered one" right after
    "which order?" means the same either way.
    """

    def choice(key: str) -> str | None:
        answer = result.answers.get(key)
        return answer.choice if answer is not None else None

    def noul(key: str) -> float:
        answer = result.answers.get(key)
        return answer.probability if answer is not None and answer.probability is not None else 0.0

    archetype = choice("archetype")
    chart = noul("wants_chart") >= CHART_THRESHOLD
    answers = _thread_number(choice("answers_question"))
    previous = threads[-1] if threads else None
    refers = archetype == "followup" or noul("refers_to_previous") >= REFERS_THRESHOLD
    ref = answers or (previous.number if previous is not None and refers else None)
    fragment = archetype in ("followup", "cannot_tell")
    if answers is None and fragment and ref in {w.thread for w in waiting}:
        answers = ref
    referenced = previous if previous is not None and ref == previous.number else None
    table = choice("primary_table")
    if table in (None, NONE) and referenced is not None and referenced.table not in (None, NONE):
        table = referenced.table
    mutation = archetype in MUTATING or (
        archetype == "followup" and not chart and referenced is not None and referenced.mutation
    )
    return Reading(archetype, table, mutation, ref, answers, chart)
