"""The typed record tools L2 calls: query, count, chart, get, open, create, update, delete,
export, describe.

Every tool validates its arguments against the schema before calling Dataverse and returns a
JSON-safe dict. Problems L2 can fix come back as ``{"error": ...}`` instead of raising, so the
model can correct itself within the same run.
"""

from __future__ import annotations

import csv
import re
import uuid
from collections import defaultdict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from .query import (
    CHOICE_TYPES,
    FORMATTED,
    GUID,
    NUMBER_TYPES,
    ToolError,
    build_expand,
    build_filter,
    build_orderby,
    build_select,
    choice_value,
    column,
    literal,
    resolve,
    shape_row,
    table,
)
from .schema import Column, Schema, Table
from .web_api import BatchRequest, DataverseError, WebApi

QUERY_TOP = 50
QUERY_MAX = 200
MATCH_MAX = 500
HINT_NAMES = 5
AGGREGATES = ("count", "sum", "average", "min", "max")
GRAINS = ("month", "week")
CHART_TYPES = ("bar", "donut", "line")
CHART_ALIASES = {"pie": "donut", "column": "bar", "trend": "line"}
# Donuts with more slices than this, and bars with more bars, fold the smallest into "Other".
DONUT_SLICES = 6
BAR_BARS = 10


@dataclass(frozen=True, slots=True)
class RecordApp:
    """The model-driven app ``open_record`` opens records in, and the tables it has forms for."""

    name: str
    app_id: str
    tables: tuple[str, ...]


def _clamp(value: Any, default: int, maximum: int) -> int:
    try:
        number = int(value) if value is not None else default
    except (TypeError, ValueError):
        number = default
    return max(1, min(number, maximum))


class RecordTools:
    """The tools against one environment. ``dry_run`` validates and resolves writes but sends none.

    A dry run still reads, so updates and deletes report the records they would change, and a
    record "created" in it can be referred to by name later in the same run.
    """

    def __init__(
        self,
        api: WebApi,
        schema: Schema,
        exports_dir: Path,
        *,
        dry_run: bool = False,
        record_app: RecordApp | None = None,
    ) -> None:
        self.api = api
        self.schema = schema
        self.exports_dir = exports_dir
        self.dry_run = dry_run
        self.record_app = record_app
        self._dry_created: dict[tuple[str, str], str] = {}

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Run a tool by name, turning fixable mistakes into an ``error`` result."""

        tool: Callable[..., Awaitable[dict[str, Any]]] | None = getattr(self, name, None)
        if name.startswith("_") or name == "call" or tool is None:
            return {"error": f"Unknown tool '{name}'."}
        try:
            return await tool(**arguments)
        except ToolError as error:
            return {"error": str(error)}
        except DataverseError as error:
            return {"error": f"Dataverse rejected the request: {error.message}"}
        except TypeError as error:
            return {"error": f"Bad arguments for {name}: {error}"}

    async def describe_table(self, table_name: str) -> dict[str, Any]:
        return describe(table(self.schema, table_name))

    async def query_records(
        self,
        table_name: str,
        select: list[str] | None = None,
        where: Any = None,
        expand: list[str] | None = None,
        orderby: Any = None,
        top: int | None = None,
    ) -> dict[str, Any]:
        owner = table(self.schema, table_name)
        columns = build_select(owner, select)
        params = self._params(owner, columns, where, expand, orderby)
        params["$count"] = "true"
        limit = _clamp(top, QUERY_TOP, QUERY_MAX)
        params["$top"] = str(limit)
        body = await self.api.get(owner.entity_set, params)
        rows = body.get("value", [])
        result = {
            "table": owner.name,
            "matched": body.get("@odata.count", len(rows)),
            "returned": len(rows),
            "records": [shape_row(self.schema, owner, row, columns) for row in rows],
        }
        return await self._hinted(result, owner, where, not rows)

    async def get_record(
        self,
        table_name: str,
        record_id: str | None = None,
        where: Any = None,
        expand: list[str] | None = None,
    ) -> dict[str, Any]:
        owner = table(self.schema, table_name)
        columns = build_select(owner, None)
        if record_id:
            if not GUID.match(str(record_id)):
                raise ToolError(f"record_id must be a GUID, not '{record_id}'.")
            params = self._params(owner, columns, None, expand, None)
            row = await self.api.get(f"{owner.entity_set}({record_id})", params)
            return {"table": owner.name, "record": shape_row(self.schema, owner, row, columns)}
        if not where:
            raise ToolError("get_record needs a record_id or a where.")
        params = self._params(owner, columns, where, expand, None)
        params["$top"] = "6"
        rows = (await self.api.get(owner.entity_set, params)).get("value", [])
        if not rows:
            result = {"table": owner.name, "record": None, "note": "No record matches."}
            return await self._hinted(result, owner, where, True)
        if len(rows) > 1:
            return {
                "table": owner.name,
                "record": None,
                "note": f"{len(rows) if len(rows) < 6 else 'More than 5'} records match.",
                "candidates": [self._brief(owner, row) for row in rows[:5]],
            }
        return {"table": owner.name, "record": shape_row(self.schema, owner, rows[0], columns)}

    async def open_record(
        self, table_name: str, record_id: str | None = None, where: Any = None
    ) -> dict[str, Any]:
        """Find one record and return the app URL that L3 opens in a dialog."""

        app = self.record_app
        if app is None:
            raise ToolError("Opening records in the app is not configured.")
        owner = table(self.schema, table_name)
        if owner.name not in app.tables:
            raise ToolError(
                f"{app.name} has no form for {owner.name}; it opens {', '.join(app.tables)}. "
                "Say that this record cannot be opened."
            )
        found = await self.get_record(owner.name, record_id, where)
        record = found.get("record")
        if not isinstance(record, dict) or not record.get("id"):
            if "candidates" in found:
                found["note"] = f"{found.get('note', '')} Ask which one to open.".strip()
            return found
        environment = self.schema.environment.rstrip("/")
        url = (
            f"{environment}/main.aspx?appid={app.app_id}&pagetype=entityrecord"
            f"&etn={owner.name}&id={record['id']}&navbar=off&cmdbar=false"
        )
        return {
            "open": {
                "url": url,
                "app": app.name,
                "table": owner.name,
                "id": record["id"],
                "name": record.get(owner.primary_name),
            },
            "note": "The record is opening in a dialog on screen.",
        }

    async def count_records(
        self,
        table_name: str,
        where: Any = None,
        group_by: str | None = None,
        measure: str | None = None,
        fn: str = "count",
        grain: str | None = None,
    ) -> dict[str, Any]:
        owner = table(self.schema, table_name)
        fn = (fn or "count").lower()
        if fn not in AGGREGATES:
            raise ToolError(f"fn must be one of {', '.join(AGGREGATES)}.")
        measured: Column | None = None
        if fn != "count":
            if not measure:
                raise ToolError(f"'{fn}' needs a measure column.")
            measured = column(owner, measure)
            if measured.type not in NUMBER_TYPES:
                raise ToolError(f"Cannot {fn} {measured.name}; it is {measured.type}.")
        grouped = column(owner, _lookup_itself(owner, group_by)) if group_by else None
        condition = build_filter(self.schema, owner, where)
        if grouped is not None and grouped.type == "DateTime":
            grain = (grain or "month").lower()
            if grain not in GRAINS:
                raise ToolError(f"grain must be one of {', '.join(GRAINS)}.")
            return await self._count_in_code(owner, condition, grouped, measured, fn, grain)
        if grouped is not None and grouped.is_lookup:
            return await self._count_in_code(owner, condition, grouped, measured, fn)

        aggregate = "$count as value" if measured is None else f"{measured.name} with {fn} as value"
        steps = [f"filter({condition})"] if condition else []
        steps.append(
            f"groupby(({grouped.name}),aggregate({aggregate}))"
            if grouped is not None
            else f"aggregate({aggregate})"
        )
        rows = (await self.api.get(owner.entity_set, {"$apply": "/".join(steps)})).get("value", [])
        result: dict[str, Any] = {"table": owner.name, "fn": fn}
        if measured is not None:
            result["measure"] = measured.name
        if grouped is None:
            result["value"] = rows[0].get("value") if rows else 0
            return await self._hinted(result, owner, where, not result["value"])
        result["group_by"] = grouped.name
        result["groups"] = sorted(
            (
                {
                    "group": row.get(grouped.name + FORMATTED, row.get(grouped.name)),
                    "value": row.get("value"),
                }
                for row in rows
            ),
            key=lambda group: -(group["value"] or 0),
        )
        return result

    async def _count_in_code(
        self,
        owner: Table,
        condition: str | None,
        grouped: Column,
        measured: Column | None,
        fn: str,
        grain: str | None = None,
    ) -> dict[str, Any]:
        """Group in code: Dataverse cannot group custom lookups or date buckets in ``$apply``.

        Date buckets are the month (``2026-03``) or the Monday of the week (``2026-03-16``),
        returned oldest first with empty buckets in between counted as zero.
        """

        select = [grouped.wire_name, *([measured.wire_name] if measured else [])]
        params = {"$select": ",".join(select)}
        if condition:
            params["$filter"] = condition
        body = await self.api.get_all(owner.entity_set, params)
        buckets: dict[str, list[float]] = defaultdict(list)
        for row in body["value"]:
            if grain is not None:
                day = _day(row.get(grouped.wire_name))
                if day is None:
                    continue
                name = _bucket(day, grain)
            else:
                name = row.get(grouped.wire_name + FORMATTED) or "(none)"
            value = row.get(measured.wire_name) if measured else 1
            if value is not None:
                buckets[str(name)].append(float(value))
        reducers: dict[str, Callable[[list[float]], float]] = {
            "count": len,
            "sum": sum,
            "average": lambda values: sum(values) / len(values),
            "min": min,
            "max": max,
        }
        groups = [
            {"group": name, "value": round(reducers[fn](values), 2)}
            for name, values in buckets.items()
            if values
        ]
        result: dict[str, Any] = {
            "table": owner.name,
            "fn": fn,
            "group_by": grouped.name,
            "groups": sorted(groups, key=lambda group: -group["value"]),
        }
        if grain is not None:
            result["grain"] = grain
            result["groups"] = _timeline(groups, grain, zero=fn in ("count", "sum"))
        if measured is not None:
            result["measure"] = measured.name
        return result

    async def show_chart(
        self,
        table_name: str,
        group_by: str,
        type: str | None = None,
        title: str | None = None,
        where: Any = None,
        measure: str | None = None,
        fn: str = "count",
        grain: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate like ``count_records`` and return the points for a chart on the flight card.

        Every number comes from Dataverse. Without a requested type: a line for date buckets, a
        donut for up to six groups, otherwise bars.
        """

        counted = await self.count_records(table_name, where, group_by, measure, fn, grain)
        groups = counted["groups"]
        requested = (type or "").lower().strip()
        requested = CHART_ALIASES.get(requested, requested)
        if requested and requested not in CHART_TYPES:
            raise ToolError(f"type must be one of {', '.join(CHART_TYPES)} (or pie).")
        dated = "grain" in counted
        kind = requested or ("line" if dated else "donut" if len(groups) <= DONUT_SLICES else "bar")
        points = [{"label": str(g["group"]), "value": g["value"] or 0} for g in groups]
        omitted = 0
        limit = DONUT_SLICES if kind == "donut" else BAR_BARS if kind == "bar" else None
        if limit is not None and len(points) > limit:
            kept, rest = points[: limit - 1], points[limit - 1 :]
            omitted = len(rest)
            if fn in ("count", "sum"):
                other = round(sum(float(p["value"]) for p in rest), 2)
                points = [*kept, {"label": f"Other ({len(rest)})", "value": other}]
            else:
                points = points[:limit]
                omitted = len(rest) - 1
        chart: dict[str, Any] = {
            "type": kind,
            "title": (title or "").strip() or _chart_title(counted),
            "table": counted["table"],
            "fn": fn,
            "group_by": counted["group_by"],
            "points": points,
        }
        for key in ("measure", "grain"):
            if key in counted:
                chart[key] = counted[key]
        if omitted:
            chart["folded"] = omitted
        return {
            "chart": chart,
            "note": "The chart is shown on the flight card. Say what it shows in one sentence.",
        }

    async def create_record(self, table_name: str, values: dict[str, Any]) -> dict[str, Any]:
        owner = table(self.schema, table_name)
        payload = await self._values(owner, values, creating=True)
        if self.dry_run:
            record_id = str(uuid.uuid4())
            name = values.get(owner.primary_name)
            if name:
                self._dry_created[(owner.name, str(name).lower())] = record_id
        else:
            record_id = await self.api.create(owner.entity_set, payload)
        return {
            "table": owner.name,
            "created": record_id,
            "name": values.get(owner.primary_name),
            "mutation": {"kind": "create", "table": owner.name, "count": 1},
        }

    async def update_records(
        self,
        table_name: str,
        values: dict[str, Any],
        ids: list[str] | None = None,
        where: Any = None,
    ) -> dict[str, Any]:
        owner = table(self.schema, table_name)
        targets = await self._targets(owner, ids, where, "update")
        if isinstance(targets, dict):
            return targets
        payload = await self._values(owner, values, creating=False)
        if targets and not self.dry_run:
            await self.api.batch(
                [BatchRequest("PATCH", f"{owner.entity_set}({i})", payload) for i in targets],
                atomic=True,
            )
        result = {
            "table": owner.name,
            "updated": len(targets),
            "ids": targets[:20],
            "values": values,
            "mutation": {"kind": "update", "table": owner.name, "count": len(targets)},
        }
        return await self._hinted(result, owner, where, not targets)

    async def delete_records(
        self, table_name: str, ids: list[str] | None = None, where: Any = None
    ) -> dict[str, Any]:
        owner = table(self.schema, table_name)
        targets = await self._targets(owner, ids, where, "delete")
        if isinstance(targets, dict):
            return targets
        if targets and not self.dry_run:
            await self.api.batch(
                [BatchRequest("DELETE", f"{owner.entity_set}({i})") for i in targets],
                atomic=True,
            )
        result = {
            "table": owner.name,
            "deleted": len(targets),
            "ids": targets[:20],
            "mutation": {"kind": "delete", "table": owner.name, "count": len(targets)},
        }
        return await self._hinted(result, owner, where, not targets)

    async def export_csv(
        self,
        table_name: str,
        select: list[str] | None = None,
        where: Any = None,
        expand: list[str] | None = None,
        orderby: Any = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        owner = table(self.schema, table_name)
        columns = build_select(owner, select)
        params = self._params(owner, columns, where, expand, orderby)
        body = await self.api.get_all(owner.entity_set, params)
        records = [_flatten(shape_row(self.schema, owner, row, columns)) for row in body["value"]]
        stem = re.sub(r"[^a-z0-9]+", "-", (name or owner.name).lower()).strip("-") or owner.name
        path = self.exports_dir / f"{stem}-{datetime.now():%Y%m%d-%H%M%S}.csv"
        path.parent.mkdir(parents=True, exist_ok=True)
        headers = list(dict.fromkeys(key for record in records for key in record))
        with path.open("w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=headers, restval="")
            writer.writeheader()
            writer.writerows(records)
        result = {
            "table": owner.name,
            "path": path.as_posix(),
            "rows": len(records),
            "columns": headers,
        }
        return await self._hinted(result, owner, where, not records)

    async def _hinted(
        self, result: dict[str, Any], owner: Table, where: Any, empty: bool
    ) -> dict[str, Any]:
        """Add a hint when nothing matched a name the operator may have said only in part."""

        if not empty or not where:
            return result
        hints = []
        for target, value in self._name_conditions(owner, where):
            name = column(target, target.primary_name)
            params = {
                "$select": name.name,
                "$filter": f"contains({name.name},{literal(name, value)})",
                "$top": str(HINT_NAMES),
            }
            rows = (await self.api.get(target.entity_set, params)).get("value", [])
            close = [str(row[name.name]) for row in rows if row.get(name.name)]
            if close:
                hints.append(
                    f"No {target.label.lower()} is named exactly '{value}'; close matches: "
                    f"{', '.join(close)}. Retry with the exact name."
                )
        if hints:
            result["hint"] = " ".join(hints)
        return result

    def _name_conditions(self, owner: Table, where: Any) -> list[tuple[Table, str]]:
        """The ``eq`` conditions in a where that compare a record name with spoken text."""

        found: list[tuple[Table, str]] = []

        def walk(node: Any) -> None:
            if isinstance(node, list):
                for part in node:
                    walk(part)
                return
            if not isinstance(node, dict):
                return
            for key in ("all", "any"):
                if key in node:
                    walk(node[key])
                    return
            if "column" not in node:
                if not {"op", "value"} & set(node):
                    for name, value in node.items():
                        walk({"column": name, "value": value})
                return
            value = node.get("value")
            if str(node.get("op", "eq")).lower() != "eq" or not isinstance(value, str):
                return
            if GUID.match(value):
                return
            try:
                ref = resolve(self.schema, owner, node["column"])
            except ToolError:
                return
            if ref.column.is_lookup:
                targets = [t for t in ref.column.targets if t in self.schema.tables]
                if len(targets) == 1:
                    found.append((self.schema.tables[targets[0]], value))
            elif ref.column.name == ref.owner.primary_name:
                found.append((ref.owner, value))

        walk(where)
        return found

    def _params(
        self,
        owner: Table,
        columns: list[Column],
        where: Any,
        expand: list[str] | None,
        orderby: Any,
    ) -> dict[str, str]:
        params = {"$select": ",".join([owner.primary_id, *(c.wire_name for c in columns)])}
        condition = build_filter(self.schema, owner, where)
        if condition:
            params["$filter"] = condition
        expands = build_expand(self.schema, owner, expand)
        if expands:
            params["$expand"] = ",".join(expands)
        order = build_orderby(owner, orderby)
        if order:
            params["$orderby"] = order
        return params

    def _brief(self, owner: Table, row: dict[str, Any]) -> dict[str, Any]:
        return {"id": row.get(owner.primary_id), "name": row.get(owner.primary_name)}

    async def _targets(
        self, owner: Table, ids: list[str] | None, where: Any, verb: str
    ) -> list[str] | dict[str, Any]:
        """The record ids a bulk write applies to; a refusal dict when neither is given."""

        if not ids and not where:
            return {
                "refused": True,
                "question": (
                    f"Which {owner.label.lower()} records should I {verb}? "
                    "Give record ids or a condition; I will not change every record."
                ),
            }
        if ids:
            bad = [i for i in ids if not GUID.match(str(i))]
            if bad:
                raise ToolError(f"ids must be GUIDs; these are not: {', '.join(map(str, bad))}.")
            return [str(i).lower() for i in ids]
        condition = build_filter(self.schema, owner, where)
        params = {"$select": owner.primary_id, "$filter": condition or ""}
        body = await self.api.get_all(owner.entity_set, params, limit=MATCH_MAX + 1)
        if len(body["value"]) > MATCH_MAX:
            raise ToolError(f"More than {MATCH_MAX} records match; narrow the condition.")
        return [row[owner.primary_id] for row in body["value"]]

    async def _values(
        self, owner: Table, values: dict[str, Any], *, creating: bool
    ) -> dict[str, Any]:
        if not isinstance(values, dict) or not values:
            raise ToolError("values needs at least one column.")
        payload: dict[str, Any] = {}
        for name, value in values.items():
            col = column(owner, name)
            if not (col.create if creating else col.update):
                raise ToolError(f"{col.name} cannot be {'set' if creating else 'changed'}.")
            if col.is_lookup:
                target = table(self.schema, col.targets[0]) if col.targets else None
                if target is None or not col.nav:
                    raise ToolError(f"Lookup {col.name} cannot be set here.")
                record_id = None if value is None else await self._lookup_id(target, value)
                payload[f"{col.nav}@odata.bind"] = (
                    f"/{target.entity_set}({record_id})" if record_id else None
                )
            elif value is None:
                payload[col.name] = None
            elif col.type in CHOICE_TYPES:
                payload[col.name] = choice_value(col, value)
            elif col.type in NUMBER_TYPES:
                number = float(literal(col, value))
                payload[col.name] = int(number) if col.type in ("Integer", "BigInt") else number
            elif col.type == "Boolean":
                payload[col.name] = literal(col, value) == "true"
            elif col.type == "DateTime":
                payload[col.name] = literal(col, value)
            else:
                payload[col.name] = str(value)
        return payload

    async def _lookup_id(self, target: Table, value: Any) -> str:
        if GUID.match(str(value)):
            return str(value).lower()
        created = self._dry_created.get((target.name, str(value).lower()))
        if created is not None:
            return created
        name = column(target, target.primary_name)
        params = {
            "$select": f"{target.primary_id},{target.primary_name}",
            "$filter": f"{target.primary_name} eq {literal(name, value)}",
            "$top": "3",
        }
        rows = (await self.api.get(target.entity_set, params)).get("value", [])
        if len(rows) == 1:
            return str(rows[0][target.primary_id])
        if not rows:
            raise ToolError(f"No {target.name} is named '{value}'. Look it up first.")
        raise ToolError(f"Several {target.name} records are named '{value}'; pass the id.")


def _lookup_itself(owner: Table, group_by: str) -> str:
    """Grouping by ``cb_accountid.name`` means grouping by the lookup: groups carry its names."""

    base, _, rest = str(group_by).partition(".")
    found = owner.columns.get(base.strip().lower())
    return base if rest and found is not None and found.is_lookup else group_by


def _day(value: Any) -> date | None:
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def _bucket(day: date, grain: str) -> str:
    if grain == "week":
        return (day - timedelta(days=day.weekday())).isoformat()
    return f"{day.year:04d}-{day.month:02d}"


def _next_bucket(bucket: str, grain: str) -> str:
    if grain == "week":
        return (date.fromisoformat(bucket) + timedelta(days=7)).isoformat()
    year, month = (int(part) for part in bucket.split("-"))
    return f"{year + month // 12:04d}-{month % 12 + 1:02d}"


def _timeline(groups: list[dict[str, Any]], grain: str, *, zero: bool) -> list[dict[str, Any]]:
    """Date buckets oldest first; counts and sums fill the empty buckets in between with zero."""

    ordered = sorted(groups, key=lambda group: group["group"])
    if not zero or not ordered:
        return ordered
    values = {group["group"]: group["value"] for group in ordered}
    filled, bucket, last = [], ordered[0]["group"], ordered[-1]["group"]
    while bucket <= last and len(filled) < 520:
        filled.append({"group": bucket, "value": values.get(bucket, 0)})
        bucket = _next_bucket(bucket, grain)
    return filled


def _chart_title(counted: dict[str, Any]) -> str:
    what = (
        "Records"
        if counted["fn"] == "count"
        else f"{counted['fn'].title()} of {counted.get('measure')}"
    )
    by = counted["group_by"]
    return f"{what} by {by}" + (f" per {counted['grain']}" if "grain" in counted else "")


def describe(owner: Table) -> dict[str, Any]:
    """The schema of one table in the compact form L2 sees in its handoff."""

    columns = {}
    for col in owner.columns.values():
        entry: dict[str, Any] = {"type": col.type, "label": col.label}
        if col.is_lookup:
            entry["to"] = list(col.targets)
        if col.options:
            entry["options"] = list(col.options.values())
        if not col.create and not col.update:
            entry["read_only"] = True
        if col.description:
            entry["about"] = col.description
        columns[col.name] = entry
    return {
        "table": owner.name,
        "label": owner.label,
        "primary_name": owner.primary_name,
        **({"about": owner.description} if owner.description else {}),
        "columns": columns,
    }


def _flatten(record: dict[str, Any]) -> dict[str, Any]:
    """One CSV row: lookups as their name, expanded records as ``lookup_record.column``."""

    flat: dict[str, Any] = {}
    for key, value in record.items():
        if key == "id":
            continue
        if isinstance(value, dict) and set(value) <= {"id", "name"}:
            flat[key] = value.get("name") or ""
        elif isinstance(value, dict):
            for inner, nested in value.items():
                if inner != "id":
                    flat[f"{key}.{inner}"] = (
                        nested.get("name") if isinstance(nested, dict) else nested
                    )
        else:
            flat[key] = value
    return {key: "" if value is None else value for key, value in flat.items()}
