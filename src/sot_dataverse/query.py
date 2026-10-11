"""Structured conditions, columns and expands validated against the schema and compiled to OData.

L2 never writes OData. It passes ``{column, op, value}`` conditions grouped with ``all``/``any``,
and this module checks every table, column, operator and value before anything reaches Dataverse.
Mistakes come back as ``ToolError`` with a "did you mean" hint the model can act on.
"""

from __future__ import annotations

import difflib
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

from .schema import Column, Schema, Table

OPERATORS = (
    "eq",
    "ne",
    "gt",
    "ge",
    "lt",
    "le",
    "contains",
    "startswith",
    "in",
    "null",
    "not_null",
    "on_or_after",
    "on_or_before",
    "last_n_days",
    "next_n_days",
)
TEXT_TYPES = frozenset({"String", "Memo"})
NUMBER_TYPES = frozenset({"Integer", "BigInt", "Decimal", "Double", "Money"})
CHOICE_TYPES = frozenset({"Picklist", "State", "Status"})
GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")


class ToolError(ValueError):
    """A request L2 can fix: the message says what is wrong and, when possible, what to use."""


def suggest(name: str, choices: Iterable[str]) -> str:
    options = list(choices)
    close = difflib.get_close_matches(name.lower(), options, n=3, cutoff=0.5)
    if not close:
        close = [option for option in options if name.lower() in option.lower()][:3]
    return f" Did you mean {', '.join(close)}?" if close else ""


def table(schema: Schema, name: str) -> Table:
    found = schema.tables.get(str(name).strip().lower())
    if found is None:
        raise ToolError(f"Unknown table '{name}'.{suggest(str(name), schema.tables)}")
    return found


def column(owner: Table, name: str) -> Column:
    key = str(name).strip().lower()
    found = owner.columns.get(key)
    if found is None:
        if key == owner.primary_id:
            return Column(owner.primary_id, "Uniqueidentifier", "Id")
        if key == "name" and owner.primary_name in owner.columns:
            # "cb_bikemodelid.name" on a custom table means its primary name (cb_name).
            return owner.columns[owner.primary_name]
        raise ToolError(
            f"Unknown column '{name}' on {owner.name}.{suggest(str(name), owner.columns)}"
        )
    return found


@dataclass(frozen=True, slots=True)
class ColumnRef:
    """A column on the queried table, or one lookup hop away (``cb_accountid.name``)."""

    owner: Table
    column: Column
    lookup: Column | None = None

    @property
    def odata(self) -> str:
        if self.lookup is not None:
            return f"{self.lookup.nav}/{self.column.wire_name}"
        return self.column.wire_name


def resolve(schema: Schema, owner: Table, path: str) -> ColumnRef:
    head, _, tail = str(path).strip().lower().partition(".")
    first = column(owner, head)
    if not tail:
        return ColumnRef(owner, first)
    if "." in tail:
        raise ToolError(
            f"'{path}' goes more than one lookup deep. Query the related table separately."
        )
    if not first.is_lookup:
        raise ToolError(f"'{head}' on {owner.name} is not a lookup, so '{path}' cannot follow it.")
    target = _lookup_target(schema, first)
    return ColumnRef(target, column(target, tail), first)


def _lookup_target(schema: Schema, lookup: Column) -> Table:
    targets = [schema.tables[t] for t in lookup.targets if t in schema.tables]
    if len(targets) != 1 or not lookup.nav:
        raise ToolError(f"Lookup '{lookup.name}' cannot be followed in a query.")
    return targets[0]


def choice_value(col: Column, value: Any) -> int:
    if isinstance(value, bool):
        raise ToolError(f"{col.name} takes one of: {_options(col)}.")
    if isinstance(value, int) and value in col.options:
        return value
    text = str(value).strip()
    if text.lstrip("-").isdigit() and int(text) in col.options:
        return int(text)
    for number, label in col.options.items():
        if label.casefold() == text.casefold():
            return number
    raise ToolError(f"'{value}' is not a valid {col.name}. Use one of: {_options(col)}.")


def _options(col: Column) -> str:
    return ", ".join(f"{label} ({number})" for number, label in col.options.items())


def _date_text(col: Column, value: Any) -> str:
    text = str(value).strip()
    try:
        if len(text) == 10:
            date.fromisoformat(text)
            return text
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as error:
        raise ToolError(
            f"{col.name} needs an ISO date such as 2026-11-17, not '{value}'."
        ) from error
    return text


def literal(col: Column, value: Any) -> str:
    """Format a value for an OData comparison against ``col``."""

    if value is None:
        return "null"
    if col.type in TEXT_TYPES:
        return "'" + str(value).replace("'", "''") + "'"
    if col.type in NUMBER_TYPES:
        try:
            number = float(value)
        except (TypeError, ValueError) as error:
            raise ToolError(f"{col.name} is a number; '{value}' is not.") from error
        return str(int(number)) if number.is_integer() else repr(number)
    if col.type == "Boolean":
        if isinstance(value, bool):
            return "true" if value else "false"
        text = str(value).strip().casefold()
        if text in ("true", "yes", "1"):
            return "true"
        if text in ("false", "no", "0"):
            return "false"
        raise ToolError(f"{col.name} is yes/no; '{value}' is not.")
    if col.type in CHOICE_TYPES:
        return str(choice_value(col, value))
    if col.type == "DateTime":
        return _date_text(col, value)
    if col.is_lookup or col.type == "Uniqueidentifier":
        if not GUID.match(str(value)):
            raise ToolError(f"{col.name} needs a record id (GUID), not '{value}'.")
        return str(value).lower()
    raise ToolError(f"Column {col.name} of type {col.type} cannot be filtered.")


def build_filter(schema: Schema, owner: Table, where: Any) -> str | None:
    """Compile a ``where`` (one condition, a list meaning all, or an all/any group) to $filter."""

    if where is None or where == [] or where == {}:
        return None
    return _node(schema, owner, where)


def _node(schema: Schema, owner: Table, node: Any) -> str:
    if isinstance(node, list):
        node = {"all": node}
    if not isinstance(node, dict):
        raise ToolError("A where clause is a condition {column, op, value} or {all|any: [...]}.")
    for key, joiner in (("all", " and "), ("any", " or ")):
        if key in node:
            parts = node[key]
            if not isinstance(parts, list) or not parts:
                raise ToolError(f"'{key}' needs a non-empty list of conditions.")
            compiled = [_node(schema, owner, part) for part in parts]
            return compiled[0] if len(compiled) == 1 else "(" + joiner.join(compiled) + ")"
    return _condition(schema, owner, node)


def _condition(schema: Schema, owner: Table, condition: dict[str, Any]) -> str:
    if "column" not in condition and condition and not {"op", "value"} & set(condition):
        # Shorthand {"cb_rmastatus": "Requested", ...}: each key is a column compared for equality.
        shorthand = [
            {"column": name, "op": "in" if isinstance(value, list) else "eq", "value": value}
            for name, value in condition.items()
        ]
        return _node(schema, owner, {"all": shorthand})
    if "column" not in condition:
        raise ToolError(f"Condition {condition} has no column.")
    op = str(condition.get("op", "eq")).strip().lower()
    if op not in OPERATORS:
        raise ToolError(f"Unknown operator '{op}'. Use one of: {', '.join(OPERATORS)}.")
    ref = resolve(schema, owner, condition["column"])
    col, value = ref.column, condition.get("value")

    # A lookup compared with a name instead of an id matches the target's primary name.
    if col.is_lookup and op in ("eq", "ne", "contains", "startswith", "in"):
        values = value if isinstance(value, list) else [value]
        if values and not all(GUID.match(str(v)) for v in values):
            if ref.lookup is not None:
                raise ToolError(f"Compare {condition['column']} with a record id.")
            target = _lookup_target(schema, col)
            return _condition(
                schema,
                owner,
                {**condition, "column": f"{col.name}.{target.primary_name}"},
            )

    if op == "null":
        return f"{ref.odata} eq null"
    if op == "not_null":
        return f"{ref.odata} ne null"
    if op == "in":
        values = value if isinstance(value, list) else [value]
        if not values:
            raise ToolError("'in' needs a list of values.")
        parts = [f"{ref.odata} eq {literal(col, v)}" for v in values]
        return parts[0] if len(parts) == 1 else "(" + " or ".join(parts) + ")"
    if op in ("contains", "startswith"):
        if col.type not in TEXT_TYPES:
            raise ToolError(f"'{op}' works on text columns; {col.name} is {col.type}.")
        return f"{op}({ref.odata},{literal(col, value)})"
    if op in ("on_or_after", "on_or_before", "last_n_days", "next_n_days"):
        if col.type != "DateTime":
            raise ToolError(f"'{op}' works on date columns; {col.name} is {col.type}.")
        if ref.lookup is not None:
            raise ToolError(f"'{op}' cannot follow a lookup; query {ref.owner.name} directly.")
        if op in ("last_n_days", "next_n_days"):
            try:
                days = int(value)
            except (TypeError, ValueError) as error:
                raise ToolError(f"'{op}' needs a whole number of days.") from error
            function = "LastXDays" if op == "last_n_days" else "NextXDays"
            return (
                f"Microsoft.Dynamics.CRM.{function}(PropertyName='{col.name}',PropertyValue={days})"
            )
        function = "OnOrAfter" if op == "on_or_after" else "OnOrBefore"
        return (
            f"Microsoft.Dynamics.CRM.{function}(PropertyName='{col.name}',"
            f"PropertyValue='{_date_text(col, value)[:10]}')"
        )
    if value is None:
        raise ToolError(f"'{op}' on {col.name} needs a value; use 'null' to test for empty.")
    return f"{ref.odata} {op} {literal(col, value)}"


def build_select(owner: Table, select: list[str] | None) -> list[Column]:
    if not select:
        return [c for c in owner.columns.values() if c.name not in ("createdon", "modifiedon")]
    return [column(owner, name) for name in select]


_EXPAND = re.compile(r"^\s*([a-z0-9_]+)\s*(?:\(([^)]*)\))?\s*$", re.IGNORECASE)


def build_expand(schema: Schema, owner: Table, expand: list[str] | None) -> list[str]:
    """Compile ``cb_accountid(name,cb_customertier)`` items to ``$expand`` clauses."""

    clauses = []
    for item in expand or []:
        match = _EXPAND.match(str(item))
        if match is None:
            raise ToolError(f"Expand '{item}' should look like lookup(column, column).")
        lookup = column(owner, match.group(1))
        if not lookup.is_lookup:
            raise ToolError(f"'{lookup.name}' on {owner.name} is not a lookup and cannot expand.")
        target = _lookup_target(schema, lookup)
        names = [n.strip() for n in (match.group(2) or "").split(",") if n.strip()]
        columns = [column(target, n) for n in names] or [column(target, target.primary_name)]
        clauses.append(f"{lookup.nav}($select={','.join(c.wire_name for c in columns)})")
    return clauses


def build_orderby(owner: Table, orderby: Any) -> str | None:
    if not orderby:
        return None
    items = orderby if isinstance(orderby, list) else [orderby]
    clauses = []
    for item in items:
        name, _, direction = str(item).strip().partition(" ")
        direction = direction.strip().lower() or "asc"
        if direction not in ("asc", "desc"):
            raise ToolError(f"Order '{item}' should be 'column asc' or 'column desc'.")
        clauses.append(f"{column(owner, name).wire_name} {direction}")
    return ",".join(clauses)


FORMATTED = "@OData.Community.Display.V1.FormattedValue"


def shape_row(
    schema: Schema, owner: Table, row: dict[str, Any], columns: list[Column] | None = None
) -> dict[str, Any]:
    """A compact record: labels for choices and lookups, raw values for everything else.

    Expanded lookups appear as ``<lookup>_record`` holding the selected target columns.
    """

    shaped: dict[str, Any] = {}
    if owner.primary_id in row:
        shaped["id"] = row[owner.primary_id]
    for col in columns if columns is not None else _returned(owner, row):
        value = row.get(col.wire_name)
        label = row.get(col.wire_name + FORMATTED)
        if col.is_lookup:
            shaped[col.name] = {"id": value, "name": label} if value else None
        elif col.type in CHOICE_TYPES and value is not None:
            shaped[col.name] = label or value
        else:
            shaped[col.name] = value
    for lookup in owner.columns.values():
        nested = row.get(lookup.nav or "")
        if lookup.is_lookup and isinstance(nested, dict):
            target = _lookup_target(schema, lookup)
            shaped[f"{lookup.name}_record"] = shape_row(schema, target, nested) if nested else None
    return shaped


def _returned(owner: Table, row: dict[str, Any]) -> list[Column]:
    return [c for c in owner.columns.values() if c.wire_name in row]
