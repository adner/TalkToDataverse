import { EventType, type BaseEvent } from "@ag-ui/core";

export type ToolStatus = "running" | "waiting" | "succeeded" | "failed";

export type ToolActivity = {
  id: string;
  name: string;
  status: ToolStatus;
  elapsedMs: number;
  // Milliseconds since the flight launched when the call started and when its result came.
  startedMs: number;
  finishedMs: number | null;
  argumentBuffer: string;
  arguments: unknown;
  result: unknown;
};

type ToolEvent = BaseEvent & {
  toolCallId?: string;
  toolCallName?: string;
  delta?: string;
  content?: string;
};

function recordOrNull(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function parseJson(value: string): unknown {
  if (!value.trim()) return null;
  try {
    return JSON.parse(value) as unknown;
  } catch {
    return value;
  }
}

function resultFailed(result: unknown): boolean {
  const record = recordOrNull(result);
  if (!record) return false;
  if (record.isError === true || record.error || record.refused === true) return true;
  return typeof record.status === "string"
    && ["error", "failed", "timeout", "unavailable", "not_found", "invalid_reference"].includes(record.status);
}

function pendingActivity(id: string, elapsedMs: number): ToolActivity {
  return {
    id,
    name: "unknown_tool",
    status: "running",
    elapsedMs,
    startedMs: elapsedMs,
    finishedMs: null,
    argumentBuffer: "",
    arguments: null,
    result: null,
  };
}

function upsert(
  current: ToolActivity[],
  id: string,
  elapsedMs: number,
  update: (activity: ToolActivity) => ToolActivity,
): ToolActivity[] {
  const index = current.findIndex((activity) => activity.id === id);
  if (index < 0) return [update(pendingActivity(id, elapsedMs)), ...current];
  return current.map((activity, activityIndex) =>
    activityIndex === index ? update({ ...activity, elapsedMs }) : activity,
  );
}

/** Fold one AG-UI event into the flight's tool calls, newest first. */
export function reduceToolActivity(
  current: ToolActivity[],
  baseEvent: BaseEvent,
  elapsedMs = 0,
): ToolActivity[] {
  const event = baseEvent as ToolEvent;
  const id = event.toolCallId;
  if (!id) return current;

  switch (event.type) {
    case EventType.TOOL_CALL_START:
      return upsert(current, id, elapsedMs, (activity) => ({
        ...activity,
        name: event.toolCallName ?? activity.name,
        status: "running",
      }));
    case EventType.TOOL_CALL_ARGS:
      return upsert(current, id, elapsedMs, (activity) => {
        const argumentBuffer = activity.argumentBuffer + (event.delta ?? "");
        return { ...activity, argumentBuffer, arguments: parseJson(argumentBuffer) };
      });
    case EventType.TOOL_CALL_END:
      return upsert(current, id, elapsedMs, (activity) => ({ ...activity, status: "waiting" }));
    case EventType.TOOL_CALL_RESULT:
      return upsert(current, id, elapsedMs, (activity) => {
        const result = parseJson(event.content ?? "");
        return {
          ...activity,
          result,
          finishedMs: elapsedMs,
          status: resultFailed(result) ? "failed" : "succeeded",
        };
      });
    default:
      return current;
  }
}
