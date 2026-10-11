"""Snapshot and restore the Contoso Bikes demo data.

``sot-reset snapshot`` reads metadata and every record of the 15 demo tables and writes
``data/contoso/schema.json`` and ``data/contoso/baseline.json``. Rows keep only writable,
non-empty values, keyed by column logical name; lookups hold the target record's GUID.

``sot-reset restore`` puts the environment back to the baseline and touches only rows that
differ: records created since are deleted, deleted records are recreated with their original
GUIDs, and changed columns are set back. ``--dry-run`` prints the plan without writing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .schema import (
    ACCOUNT_COLUMNS,
    COMMON_COLUMNS,
    CONTOSO_TABLES,
    SCALAR_TYPES,
    Column,
    Schema,
    Table,
)
from .web_api import BatchRequest, WebApi

DATA_DIR = Path("data/contoso")


def _label(value: Any) -> str:
    localized = (value or {}).get("UserLocalizedLabel") or {}
    return str(localized.get("Label") or "")


def _wanted(table: str, attribute: dict[str, Any]) -> bool:
    name = attribute["LogicalName"]
    if attribute.get("AttributeOf") or attribute.get("AttributeType") not in SCALAR_TYPES:
        return False
    if name in COMMON_COLUMNS:
        return True
    if attribute["AttributeType"] == "Money" and name.endswith("_base"):
        return False
    if table == "account":
        return name in ACCOUNT_COLUMNS or name.startswith("cb_")
    return bool(attribute.get("IsCustomAttribute"))


async def _options(api: WebApi, table: str, cast: str) -> dict[str, dict[int, str]]:
    body = await api.get(
        f"EntityDefinitions(LogicalName='{table}')/Attributes/Microsoft.Dynamics.CRM.{cast}"
        "?$select=LogicalName&$expand=OptionSet($select=Options)"
    )
    return {
        attribute["LogicalName"]: {
            int(option["Value"]): _label(option.get("Label"))
            for option in (attribute.get("OptionSet") or {}).get("Options", [])
        }
        for attribute in body.get("value", [])
    }


async def fetch_table(api: WebApi, table: str) -> Table:
    definition = f"EntityDefinitions(LogicalName='{table}')"
    entity, attributes, lookups, relationships, *options = await asyncio.gather(
        api.get(
            f"{definition}?$select=EntitySetName,PrimaryIdAttribute,PrimaryNameAttribute,"
            "DisplayName,Description"
        ),
        api.get(
            f"{definition}/Attributes?$select=LogicalName,AttributeType,IsValidForCreate,"
            "IsValidForUpdate,IsCustomAttribute,AttributeOf,DisplayName,Description"
        ),
        api.get(
            f"{definition}/Attributes/Microsoft.Dynamics.CRM.LookupAttributeMetadata"
            "?$select=LogicalName,Targets"
        ),
        api.get(
            f"{definition}/ManyToOneRelationships?$select=ReferencingAttribute,"
            "ReferencedEntity,ReferencingEntityNavigationPropertyName"
        ),
        *(
            _options(api, table, cast)
            for cast in (
                "PicklistAttributeMetadata",
                "StateAttributeMetadata",
                "StatusAttributeMetadata",
            )
        ),
    )
    targets = {a["LogicalName"]: tuple(a.get("Targets") or ()) for a in lookups["value"]}
    navs = {
        r["ReferencingAttribute"]: r["ReferencingEntityNavigationPropertyName"]
        for r in relationships["value"]
    }
    choices = {name: values for found in options for name, values in found.items()}
    columns = {}
    for attribute in sorted(attributes["value"], key=lambda a: a["LogicalName"]):
        if not _wanted(table, attribute):
            continue
        name = attribute["LogicalName"]
        columns[name] = Column(
            name=name,
            type=attribute["AttributeType"],
            label=_label(attribute.get("DisplayName")),
            description=_label(attribute.get("Description")),
            create=bool(attribute.get("IsValidForCreate")),
            update=bool(attribute.get("IsValidForUpdate")),
            targets=targets.get(name, ()),
            nav=navs.get(name),
            options=choices.get(name, {}),
        )
    return Table(
        name=table,
        entity_set=entity["EntitySetName"],
        primary_id=entity["PrimaryIdAttribute"],
        primary_name=entity["PrimaryNameAttribute"],
        label=_label(entity.get("DisplayName")),
        description=_label(entity.get("Description")),
        columns=columns,
    )


def _writable(table: Table) -> list[Column]:
    return [c for c in table.columns.values() if c.create or c.update]


async def fetch_rows(api: WebApi, table: Table) -> dict[str, dict[str, Any]]:
    """Current writable, non-empty values of every row, by record id."""

    writable = _writable(table)
    select = ",".join([table.primary_id, *(c.wire_name for c in writable)])
    body = await api.get_all(f"{table.entity_set}?$select={select}")
    return {
        record[table.primary_id]: {
            c.name: record[c.wire_name] for c in writable if record.get(c.wire_name) is not None
        }
        for record in body["value"]
    }


async def snapshot(api: WebApi, out: Path) -> dict[str, int]:
    tables = dict(
        zip(
            CONTOSO_TABLES,
            await asyncio.gather(*(fetch_table(api, name) for name in CONTOSO_TABLES)),
            strict=True,
        )
    )
    schema = Schema(api.environment, tables)
    rows = dict(
        zip(
            CONTOSO_TABLES,
            await asyncio.gather(*(fetch_rows(api, tables[name]) for name in CONTOSO_TABLES)),
            strict=True,
        )
    )
    schema.save(out / "schema.json")
    baseline = {
        "environment": api.environment,
        "taken_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "tables": {
            name: [{"id": key, "values": values} for key, values in sorted(by_id.items())]
            for name, by_id in rows.items()
        },
    }
    (out / "baseline.json").write_text(
        json.dumps(baseline, indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return {name: len(by_id) for name, by_id in rows.items()}


@dataclass(slots=True)
class Plan:
    deletes: list[tuple[Table, str]] = field(default_factory=list)
    creates: list[tuple[Table, str, dict[str, Any]]] = field(default_factory=list)
    updates: list[tuple[Table, str, dict[str, Any]]] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.deletes or self.creates or self.updates)

    def summary(self) -> dict[str, int]:
        return {
            "deleted": len(self.deletes),
            "recreated": len(self.creates),
            "reverted": len(self.updates),
            "skipped": len(self.skipped),
        }


def _equal(column: Column, baseline: Any, current: Any) -> bool:
    numeric = column.type in ("Money", "Decimal", "Double")
    if numeric and baseline is not None and current is not None:
        return abs(float(baseline) - float(current)) < 1e-6
    if isinstance(baseline, str) and isinstance(current, str) and column.is_lookup:
        return baseline.lower() == current.lower()
    return bool(baseline == current)


def _payload(schema: Schema, column: Column, value: Any, payload: dict[str, Any]) -> None:
    if column.is_lookup:
        if len(column.targets) != 1 or not column.nav:
            raise ValueError(f"cannot bind lookup {column.name}")
        target = schema.tables[column.targets[0]]
        payload[f"{column.nav}@odata.bind"] = (
            f"/{target.entity_set}({value})" if value is not None else None
        )
    else:
        payload[column.name] = value


def plan_restore(
    schema: Schema,
    baseline: dict[str, list[dict[str, Any]]],
    current: dict[str, dict[str, dict[str, Any]]],
) -> Plan:
    """Compare current rows with the baseline and list the writes that restore it.

    Missing records are recreated with their scalar values only. Their lookups, state and status
    are set by the update pass, so records can point at each other in any order.
    """

    plan = Plan()
    for name in CONTOSO_TABLES:
        table = schema.tables[name]
        wanted = {row["id"]: row["values"] for row in baseline.get(name, [])}
        now = current.get(name, {})
        for record_id in sorted(set(now) - set(wanted)):
            plan.deletes.append((table, record_id))
        for record_id, values in wanted.items():
            existing = now.get(record_id)
            if existing is None:
                created = {table.primary_id: record_id}
                for column_name, value in values.items():
                    column = table.columns.get(column_name)
                    if (
                        column
                        and column.create
                        and not column.is_lookup
                        and column.type
                        not in (
                            "State",
                            "Status",
                        )
                    ):
                        created[column_name] = value
                plan.creates.append((table, record_id, created))
                existing = {k: v for k, v in created.items() if k != table.primary_id}
            patch: dict[str, Any] = {}
            for column in _writable(table):
                before, after = values.get(column.name), existing.get(column.name)
                if _equal(column, before, after):
                    continue
                if not column.update:
                    plan.skipped.append(f"{name}({record_id}).{column.name} is not updatable")
                    continue
                _payload(schema, column, before, patch)
            if "statecode" in patch or "statuscode" in patch:
                for column_name in ("statecode", "statuscode"):
                    if column_name in values:
                        patch[column_name] = values[column_name]
            if patch:
                plan.updates.append((table, record_id, patch))
    return plan


async def restore(api: WebApi, schema: Schema, baseline_path: Path, *, dry_run: bool) -> Plan:
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))["tables"]
    current = dict(
        zip(
            CONTOSO_TABLES,
            await asyncio.gather(
                *(fetch_rows(api, schema.tables[name]) for name in CONTOSO_TABLES)
            ),
            strict=True,
        )
    )
    plan = plan_restore(schema, baseline, current)
    if dry_run or plan.empty:
        return plan
    phases = [
        [BatchRequest("DELETE", f"{t.entity_set}({i})") for t, i in reversed(plan.deletes)],
        [BatchRequest("POST", t.entity_set, body) for t, _, body in plan.creates],
        [BatchRequest("PATCH", f"{t.entity_set}({i})", body) for t, i, body in plan.updates],
    ]
    failures = []
    for requests in phases:
        if not requests:
            continue
        responses = await api.batch(requests, atomic=False)
        failures += [
            f"{request.method} {request.path}: {response.error}"
            for request, response in zip(requests, responses, strict=False)
            if not response.ok
        ]
        if len(responses) < len(requests):
            failures.append(f"{len(requests) - len(responses)} requests got no response")
    if failures:
        raise RuntimeError("restore incomplete:\n  " + "\n  ".join(failures))
    return plan


def _print_plan(plan: Plan) -> None:
    for table, record_id in plan.deletes:
        print(f"delete   {table.name}({record_id})")
    for table, record_id, _ in plan.creates:
        print(f"recreate {table.name}({record_id})")
    for table, record_id, patch in plan.updates:
        print(f"revert   {table.name}({record_id}): {', '.join(sorted(patch))}")
    for line in plan.skipped:
        print(f"skip     {line}")


async def _main(args: argparse.Namespace) -> None:
    from sot_agent.config import load_config

    l2 = load_config().l2
    environment = args.environment or l2.dataverse_url
    if not environment:
        raise SystemExit("sot-reset needs [l2] dataverse_url in config/sot.toml, or --environment")
    api = WebApi(environment, tenant_id=l2.tenant_id)
    try:
        if args.command == "snapshot":
            counts = await snapshot(api, args.data)
            for name, count in counts.items():
                print(f"{name}: {count}")
            print(f"baseline: {sum(counts.values())} records in {len(counts)} tables")
            return
        schema = Schema.load(args.data / "schema.json")
        plan = await restore(api, schema, args.data / "baseline.json", dry_run=args.dry_run)
        _print_plan(plan)
        verb = "would restore" if args.dry_run else "restored"
        print(f"{verb}: {plan.summary()}" if not plan.empty else "already at baseline")
    finally:
        await api.close()


def main() -> None:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="sot-reset", description=__doc__)
    parser.add_argument("--environment", help="default: [l2] dataverse_url in config/sot.toml")
    parser.add_argument("--data", type=Path, default=DATA_DIR)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("snapshot", help="record the schema and every record as the baseline")
    restore_parser = sub.add_parser("restore", help="put changed rows back to the baseline")
    restore_parser.add_argument("--dry-run", action="store_true", help="print the plan only")
    asyncio.run(_main(parser.parse_args()))
