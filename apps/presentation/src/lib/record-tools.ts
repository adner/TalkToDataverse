// How a flight card shows L2's calls to the Contoso Bikes record tools (decision mode).

import { tableLabel } from "./hold-state";
import type { ToolActivity } from "./tool-activity";

export const TOOL_VERBS: Record<string, string> = {
  query_records: "Query",
  count_records: "Count",
  get_record: "Get",
  show_chart: "Chart",
  open_record: "Open",
  create_record: "Create",
  update_records: "Update",
  delete_records: "Delete",
  export_csv: "Export",
  describe_table: "Describe",
  ask_operator: "Ask",
  get_thread_context: "Thread",
};

const WRITES = new Set(["create_record", "update_records", "delete_records"]);

const OPERATORS: Record<string, string> = {
  eq: "=",
  ne: "≠",
  gt: ">",
  ge: "≥",
  lt: "<",
  le: "≤",
  contains: "contains",
  startswith: "starts with",
  in: "in",
  null: "is empty",
  not_null: "is set",
  on_or_after: "on or after",
  on_or_before: "on or before",
  last_n_days: "in last days",
  next_n_days: "in next days",
};

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function short(column: string): string {
  return column.replace(/(^|\.)cb_/g, "$1");
}

function literal(value: unknown): string {
  if (Array.isArray(value)) return value.map(literal).join(", ");
  if (typeof value === "string") return value;
  return value === undefined ? "" : JSON.stringify(value);
}

/** A where clause in a few words: "deliveryrisk = At Risk and accountid.name = Nordic". */
export function describeWhere(where: unknown): string {
  if (Array.isArray(where)) return where.map(describeWhere).filter(Boolean).join(" and ");
  const node = record(where);
  if (!node) return "";
  if (Array.isArray(node.all)) return describeWhere(node.all);
  if (Array.isArray(node.any)) {
    const parts = node.any.map(describeWhere).filter(Boolean);
    return parts.length > 1 ? `(${parts.join(" or ")})` : (parts[0] ?? "");
  }
  if (typeof node.column !== "string") {
    // Shorthand {"column": value}: each key is compared for equality.
    return Object.entries(node)
      .map(([column, value]) => `${short(column)} = ${literal(value)}`)
      .join(" and ");
  }
  const op = typeof node.op === "string" ? node.op : "eq";
  const symbol = OPERATORS[op] ?? op;
  const value = op === "null" || op === "not_null" ? "" : ` ${literal(node.value)}`;
  return `${short(node.column)} ${symbol}${value}`;
}

export function isWrite(activity: ToolActivity): boolean {
  return WRITES.has(activity.name);
}

/** What a call works on: the table and its condition, the question, or the thread. */
export function callTarget(activity: ToolActivity): string {
  const args = record(activity.arguments);
  if (!args) return activity.status === "running" ? "…" : "";
  if (activity.name === "ask_operator") return typeof args.question === "string" ? args.question : "";
  if (activity.name === "get_thread_context") {
    return typeof args.thread_number === "number"
      ? `Thread ${String(args.thread_number).padStart(2, "0")}`
      : "";
  }
  const table = typeof args.table === "string" ? tableLabel(args.table) : "";
  const where = describeWhere(args.where);
  const ids = Array.isArray(args.ids) ? `${args.ids.length} by id` : "";
  const values = record(args.values);
  const assignments = values
    ? Object.entries(values).map(([column, value]) => `${short(column)} = ${literal(value)}`).join(", ")
    : "";
  const set = assignments ? `${activity.name === "create_record" ? "with" : "set"} ${assignments}` : "";
  const grouped =
    activity.name === "show_chart" && typeof args.group_by === "string"
      ? `by ${short(args.group_by)}${typeof args.grain === "string" ? ` per ${args.grain}` : ""}`
      : "";
  return [table, where || ids, set, grouped].filter(Boolean).join(" · ");
}

/** The result in a few words, or the error L2 has to correct. */
export function callOutcome(activity: ToolActivity): string | null {
  const result = record(activity.result);
  if (!result) return null;
  if (typeof result.error === "string") return result.error;
  if (result.refused === true) return "Refused: no ids or condition";
  if (typeof result.hint === "string") return result.hint;
  switch (activity.name) {
    case "query_records":
      return `${String(result.matched ?? result.returned ?? 0)} matched`;
    case "count_records":
      return Array.isArray(result.groups)
        ? `${result.groups.length} groups`
        : `= ${typeof result.value === "number" ? result.value.toLocaleString("sv-SE") : String(result.value)}`;
    case "get_record":
      return result.record ? "1 record" : Array.isArray(result.candidates) ? `${result.candidates.length} candidates` : "No match";
    case "show_chart": {
      const chart = record(result.chart);
      return chart && Array.isArray(chart.points) ? `${String(chart.type)} · ${chart.points.length} points` : null;
    }
    case "open_record": {
      const opened = record(result.open);
      if (opened) return `Opened ${String(opened.name ?? "record")}`;
      return Array.isArray(result.candidates) ? `${result.candidates.length} candidates` : "No match";
    }
    case "create_record":
      return "Created";
    case "update_records":
      return `${String(result.updated ?? 0)} updated`;
    case "delete_records":
      return `${String(result.deleted ?? 0)} deleted`;
    case "export_csv":
      return `${String(result.rows ?? 0)} rows`;
    case "ask_operator":
      return typeof result.answer === "string" ? `“${result.answer}”` : "No answer";
    case "get_thread_context":
      return typeof result.status === "string" ? result.status.replaceAll("_", " ") : null;
    case "describe_table":
      return record(result.columns) ? `${Object.keys(record(result.columns) ?? {}).length} columns` : null;
    default:
      return null;
  }
}

export function exportPath(activity: ToolActivity): string | null {
  const result = record(activity.result);
  return activity.name === "export_csv" && typeof result?.path === "string" ? result.path : null;
}

export function callDurationMs(activity: ToolActivity, nowMs: number): number {
  return Math.max(0, (activity.finishedMs ?? nowMs) - activity.startedMs);
}
