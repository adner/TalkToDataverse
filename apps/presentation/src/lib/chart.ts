// The chart a flight card draws from L2's `show_chart` result, and the record `open_record` opens.
//
// Every number comes from the tool result: L2 picks the grouping, the backend aggregates.

import type { ToolActivity } from "./tool-activity";

export type ChartType = "bar" | "donut" | "line";

export type ChartPoint = { label: string; value: number };

export type ChartSpec = {
  type: ChartType;
  title: string;
  table: string | null;
  points: ChartPoint[];
  fn: string;
  measure: string | null;
  grain: "month" | "week" | null;
  folded: number;
};

export type OpenedRecord = { url: string; app: string; table: string; id: string; name: string };

function record(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

export function parseChart(result: unknown): ChartSpec | null {
  const chart = record(record(result)?.chart);
  if (!chart || !Array.isArray(chart.points)) return null;
  const type = chart.type === "donut" || chart.type === "line" ? chart.type : "bar";
  const points = chart.points
    .map((point) => record(point))
    .filter((point): point is Record<string, unknown> => point !== null)
    .map((point) => ({ label: String(point.label ?? ""), value: Number(point.value) || 0 }));
  return {
    type,
    title: typeof chart.title === "string" ? chart.title : "",
    table: typeof chart.table === "string" ? chart.table : null,
    points,
    fn: typeof chart.fn === "string" ? chart.fn : "count",
    measure: typeof chart.measure === "string" ? chart.measure : null,
    grain: chart.grain === "month" || chart.grain === "week" ? chart.grain : null,
    folded: typeof chart.folded === "number" ? chart.folded : 0,
  };
}

/** The latest chart a flight drew; `activities` are oldest first. */
export function latestChart(activities: ToolActivity[]): ChartSpec | null {
  for (let index = activities.length - 1; index >= 0; index -= 1) {
    const activity = activities[index];
    if (activity.name !== "show_chart") continue;
    const chart = parseChart(activity.result);
    if (chart) return chart;
  }
  return null;
}

export function parseOpened(activity: ToolActivity): OpenedRecord | null {
  if (activity.name !== "open_record") return null;
  const opened = record(record(activity.result)?.open);
  if (!opened || typeof opened.url !== "string" || !opened.url.startsWith("https://")) return null;
  return {
    url: opened.url,
    app: String(opened.app ?? ""),
    table: String(opened.table ?? ""),
    id: String(opened.id ?? ""),
    name: typeof opened.name === "string" && opened.name ? opened.name : "Record",
  };
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/**
 * "2026-03" → "Mar 26" ("Mar" without the year); a week's Monday "2026-03-16" → "16 Mar";
 * anything else unchanged.
 */
export function bucketLabel(label: string, grain: ChartSpec["grain"], year = true): string {
  const month = /^(\d{4})-(\d{2})$/.exec(label);
  if (grain === "month" && month) {
    const name = MONTHS[Number(month[2]) - 1];
    return year ? `${name} ${month[1].slice(2)}` : name;
  }
  const day = /^(\d{4})-(\d{2})-(\d{2})$/.exec(label);
  if (grain === "week" && day) return `${Number(day[3])} ${MONTHS[Number(day[2]) - 1]}`;
  return label;
}

/** Short numbers for chart labels: 4 190 000 → "4.19M", 12 500 → "12.5k", 0.5 → "0.5". */
export function compactNumber(value: number): string {
  const magnitude = Math.abs(value);
  const trim = (n: number) => String(Number(n.toFixed(n >= 100 ? 0 : n >= 10 ? 1 : 2)));
  if (magnitude >= 1e6) return `${trim(value / 1e6)}M`;
  if (magnitude >= 1e4) return `${trim(value / 1e3)}k`;
  return Number.isInteger(value) ? value.toLocaleString("sv-SE") : trim(value);
}

export type DonutSlice = ChartPoint & { start: number; length: number };

/** Slices as fractions of the circle, each starting where the previous ended. */
export function donutSlices(points: ChartPoint[]): DonutSlice[] {
  const total = points.reduce((sum, point) => sum + Math.max(0, point.value), 0);
  let start = 0;
  return points.map((point) => {
    const length = total > 0 ? Math.max(0, point.value) / total : 0;
    const slice = { ...point, start, length };
    start += length;
    return slice;
  });
}

/** Line points in a 0–100 box, x evenly spaced, y from zero (bottom) to the maximum (top). */
export function linePath(points: ChartPoint[]): [number, number][] {
  const max = Math.max(0, ...points.map((point) => point.value));
  const step = points.length > 1 ? 100 / (points.length - 1) : 0;
  return points.map((point, index) => [
    points.length > 1 ? index * step : 50,
    max > 0 ? 100 - (Math.max(0, point.value) / max) * 100 : 100,
  ]);
}
