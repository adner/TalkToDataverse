"""The Contoso Bikes table schema used by L2's handoff, tool validation and the reset script."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

CONTOSO_TABLES = (
    "account",
    "cb_bikemodel",
    "cb_salesagreement",
    "cb_salesorder",
    "cb_salesorderline",
    "cb_registeredbike",
    "cb_warrantycase",
    "cb_returnauthorization",
    "cb_supplier",
    "cb_part",
    "cb_purchaseorder",
    "cb_qualityincident",
    "cb_productionline",
    "cb_productionorder",
    "cb_bomline",
)

# Standard account columns the demo uses; every other table only exposes its custom columns.
ACCOUNT_COLUMNS = frozenset(
    {
        "name",
        "accountnumber",
        "telephone1",
        "emailaddress1",
        "websiteurl",
        "address1_city",
        "address1_country",
        "revenue",
        "numberofemployees",
        "industrycode",
        "customertypecode",
        "description",
        "parentaccountid",
    }
)
COMMON_COLUMNS = frozenset({"statecode", "statuscode", "createdon", "modifiedon"})
SCALAR_TYPES = frozenset(
    {
        "String",
        "Memo",
        "Integer",
        "BigInt",
        "Decimal",
        "Double",
        "Money",
        "Boolean",
        "DateTime",
        "Picklist",
        "State",
        "Status",
        "Lookup",
        "Customer",
    }
)


@dataclass(frozen=True, slots=True)
class Column:
    name: str
    type: str
    label: str
    description: str = ""
    create: bool = False
    update: bool = False
    targets: tuple[str, ...] = ()
    nav: str | None = None
    options: dict[int, str] = field(default_factory=dict)

    @property
    def is_lookup(self) -> bool:
        return self.type in ("Lookup", "Customer")

    @property
    def wire_name(self) -> str:
        """The name a Web API payload uses when reading this column."""

        return f"_{self.name}_value" if self.is_lookup else self.name


@dataclass(frozen=True, slots=True)
class Table:
    name: str
    entity_set: str
    primary_id: str
    primary_name: str
    label: str
    description: str
    columns: dict[str, Column]


@dataclass(slots=True)
class Schema:
    environment: str
    tables: dict[str, Table]

    def to_json(self) -> dict[str, Any]:
        return {
            "environment": self.environment,
            "tables": {
                name: {
                    **{k: v for k, v in asdict(table).items() if k != "columns"},
                    "columns": {
                        c.name: {
                            **asdict(c),
                            "targets": list(c.targets),
                            "options": {str(k): v for k, v in c.options.items()},
                        }
                        for c in table.columns.values()
                    },
                }
                for name, table in self.tables.items()
            },
        }

    @classmethod
    def from_json(cls, value: dict[str, Any]) -> Schema:
        tables = {}
        for name, raw in value["tables"].items():
            columns = {
                column_name: Column(
                    **{
                        **c,
                        "targets": tuple(c.get("targets", ())),
                        "options": {int(k): v for k, v in c.get("options", {}).items()},
                    }
                )
                for column_name, c in raw["columns"].items()
            }
            tables[name] = Table(**{**raw, "columns": columns})
        return cls(value["environment"], tables)

    @classmethod
    def load(cls, path: Path) -> Schema:
        return cls.from_json(json.loads(path.read_text(encoding="utf-8")))

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(self.to_json(), indent=1, ensure_ascii=False) + "\n", encoding="utf-8"
        )
