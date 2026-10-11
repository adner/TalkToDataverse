// Live projection of one L1 hold from the native `l1` and `l0` events (decision mode).
//
// L1 decides on every word prefix of the segment it is listening to. Each `decision` carries the
// span of words it saw, the top archetype probabilities, the tables it involves and the call's
// latency. A pause cuts a segment (`segment_cut`) or drops it as not a command
// (`segment_dropped`). Word indices are L1's own, so the transcript is rebuilt from decision text.

import type { NativeEnvelope } from "./native-state";

export const ARCHETYPE_LABELS: Record<string, string> = {
  "query.list": "List",
  "query.count": "Count",
  "query.get": "Get",
  "data.create": "Create",
  "data.update": "Update",
  "data.delete": "Delete",
  "data.export": "Export",
  "record.open": "Open",
  followup: "Follow-up",
  not_a_command: "Not a command",
  cannot_tell: "Cannot tell",
};

export const TABLE_LABELS: Record<string, string> = {
  account: "Accounts",
  cb_bikemodel: "Bike models",
  cb_salesagreement: "Agreements",
  cb_salesorder: "Sales orders",
  cb_salesorderline: "Order lines",
  cb_registeredbike: "Registered bikes",
  cb_warrantycase: "Warranty cases",
  cb_returnauthorization: "Returns",
  cb_supplier: "Suppliers",
  cb_part: "Parts",
  cb_purchaseorder: "Purchase orders",
  cb_qualityincident: "Quality incidents",
  cb_productionline: "Production lines",
  cb_productionorder: "Production orders",
  cb_bomline: "BOM lines",
};

export type Decision = {
  kind: string;
  start: number;
  end: number;
  archetype: string;
  probabilities: [string, number][];
  table: string | null;
  involved: Record<string, number>;
  lit: string[];
  mutation: boolean;
  chart: boolean;
  complete: number | null;
  ref: number | null;
  answers: number | null;
  latencyMs: number | null;
  source: string;
};

export type Flip = { word: number; archetype: string; latencyMs: number | null };

export type HeldSegment = {
  start: number;
  end: number;
  thread: number | null;
  archetype: string | null;
  table: string | null;
  answers: number | null;
  dropped: boolean;
};

export type Revision = { op: string; words: string[] };

export type HoldProjection = {
  utteranceId: string | null;
  words: string[];
  cut: number;
  latest: Decision | null;
  flips: Flip[];
  skipped: number[];
  segments: HeldSegment[];
  calls: number;
  revisions: Revision[];
};

export const emptyHold: HoldProjection = {
  utteranceId: null,
  words: [],
  cut: 0,
  latest: null,
  flips: [],
  skipped: [],
  segments: [],
  calls: 0,
  revisions: [],
};

function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function numberOr<T>(value: unknown, fallback: T): number | T {
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

function stringOr<T>(value: unknown, fallback: T): string | T {
  return typeof value === "string" ? value : fallback;
}

function span(value: unknown): [number, number] | null {
  return Array.isArray(value) && typeof value[0] === "number" && typeof value[1] === "number"
    ? [value[0], value[1]]
    : null;
}

function words(text: unknown): string[] {
  return typeof text === "string" ? text.split(/\s+/).filter(Boolean) : [];
}

/** Place a span's words at its indices, keeping words L1 has already seen elsewhere. */
function placeWords(current: string[], start: number, text: unknown): string[] {
  const placed = words(text);
  if (!placed.length) return current;
  const next = current.slice();
  placed.forEach((word, offset) => {
    next[start + offset] = word;
  });
  return next;
}

export function parseDecision(event: Record<string, unknown>): Decision | null {
  const range = span(event.span);
  if (!range) return null;
  const probabilities = Object.entries(record(event.archetype_p))
    .filter((entry): entry is [string, number] => typeof entry[1] === "number")
    .sort((a, b) => b[1] - a[1]);
  const involved = Object.fromEntries(
    Object.entries(record(event.involved)).filter(([, p]) => typeof p === "number"),
  ) as Record<string, number>;
  return {
    kind: stringOr(event.kind, "word"),
    start: range[0],
    end: range[1],
    archetype: stringOr(event.archetype, "cannot_tell"),
    probabilities,
    table: stringOr(event.table, null),
    involved,
    lit: Array.isArray(event.lit) ? event.lit.filter((t): t is string => typeof t === "string") : [],
    mutation: event.mutation === true,
    chart: event.chart === true,
    complete: numberOr(event.complete, null),
    ref: numberOr(event.ref, null),
    answers: numberOr(event.answers, null),
    latencyMs: numberOr(event.latency_ms, null),
    source: stringOr(event.source, ""),
  };
}

function startHold(utteranceId: string | null): HoldProjection {
  return { ...emptyHold, utteranceId };
}

export function reduceHold(current: HoldProjection, envelope: NativeEnvelope): HoldProjection {
  const event = envelope.event;
  const type = stringOr(event.type, "");
  const utteranceId = stringOr(event.utterance_id, null);

  if (envelope.source === "l0") {
    if (type === "utterance_started") return startHold(utteranceId);
    if (type === "utterance_finalized" && utteranceId === current.utteranceId) {
      const revisions = (Array.isArray(event.revisions) ? event.revisions : []).map((item) => {
        const revision = record(item);
        return {
          op: stringOr(revision.op, "replace"),
          words: (Array.isArray(revision.words) ? revision.words : []).map((word) =>
            typeof word === "string" ? word : stringOr(record(word).text, ""),
          ),
        };
      });
      return { ...current, revisions };
    }
    return current;
  }
  if (envelope.source !== "l1") return current;

  // Typed input has no L0 events: the first L1 event of a new utterance starts its hold.
  const base =
    utteranceId && utteranceId !== current.utteranceId ? startHold(utteranceId) : current;

  if (type === "decision") {
    const decision = parseDecision(event);
    if (!decision) return base;
    const placed = placeWords(base.words, decision.start, event.text);
    if (decision.start !== base.cut) {
      // A late decision on an earlier span: keep its words but not its reading.
      return { ...base, words: placed, calls: base.calls + 1 };
    }
    const previous = base.latest?.start === base.cut ? base.latest.archetype : null;
    const flips =
      decision.archetype !== previous
        ? [
            ...base.flips,
            { word: Math.max(decision.end - 1, 0), archetype: decision.archetype, latencyMs: decision.latencyMs },
          ]
        : base.flips;
    return { ...base, words: placed, latest: decision, flips, calls: base.calls + 1 };
  }
  if (type === "decision_skipped") {
    const range = span(event.span);
    return range ? { ...base, skipped: [...base.skipped, Math.max(range[1] - 1, 0)] } : base;
  }
  if (type === "segment_cut") {
    const segment = record(event.segment);
    const start = numberOr(segment.start, base.cut);
    const end = numberOr(segment.end, start);
    return {
      ...base,
      words: placeWords(base.words, start, segment.text),
      cut: end,
      segments: [
        ...base.segments,
        {
          start,
          end,
          thread: numberOr(segment.thread, null),
          archetype: stringOr(segment.archetype, null),
          table: stringOr(segment.table, null),
          answers: numberOr(segment.answers, null),
          dropped: false,
        },
      ],
    };
  }
  if (type === "segment_dropped") {
    const range = span(event.span);
    if (!range) return base;
    return {
      ...base,
      words: placeWords(base.words, range[0], event.text),
      cut: range[1],
      segments: [
        ...base.segments,
        { start: range[0], end: range[1], thread: null, archetype: "not_a_command", table: null, answers: null, dropped: true },
      ],
    };
  }
  return base;
}

/** The reading for the segment L1 is listening to now, or null between segments. */
export function currentReading(hold: HoldProjection): Decision | null {
  return hold.latest && hold.latest.start === hold.cut ? hold.latest : null;
}

export function archetypeLabel(archetype: string | null | undefined): string {
  return archetype ? (ARCHETYPE_LABELS[archetype] ?? archetype) : "Pending";
}

export function tableLabel(table: string | null | undefined): string {
  return table ? (TABLE_LABELS[table] ?? table) : "No table";
}
