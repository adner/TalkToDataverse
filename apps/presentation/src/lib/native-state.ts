// The native presentation channel: L0 transcript events, L1 decisions (see hold-state.ts) and
// L2 question events, as published by the backend on /api/presentation/events.

export type NativeEnvelope = {
  source: "l0" | "l1" | "l2";
  event: Record<string, unknown>;
};

export type L0Projection = {
  availability: string;
  pressState: string;
  utteranceId: string | null;
  transcript: string;
  latestSeq: number | null;
};

export type NativeProjection = {
  l0: L0Projection;
};

export const emptyNativeProjection: NativeProjection = {
  l0: {
    availability: "connecting",
    pressState: "idle",
    utteranceId: null,
    transcript: "",
    latestSeq: null,
  },
};

export function beginNativeUtterance(
  current: NativeProjection,
  utteranceId: string | null = null,
): NativeProjection {
  return {
    l0: {
      ...current.l0,
      pressState: "pressed",
      utteranceId,
      transcript: "",
      latestSeq: null,
    },
  };
}

function numberOrNull(value: unknown): number | null {
  return typeof value === "number" ? value : null;
}

function stringOr(value: unknown, fallback: string): string {
  return typeof value === "string" ? value : fallback;
}

function transcriptText(stable: unknown, tail: unknown): string {
  return [stringOr(stable, ""), stringOr(tail, "")].filter(Boolean).join(" ");
}

export function reduceNativeEvent(
  current: NativeProjection,
  envelope: NativeEnvelope,
): NativeProjection {
  if (envelope.source !== "l0") return current;
  const event = envelope.event;
  const type = stringOr(event.type, "unknown");
  const utteranceId = stringOr(event.utterance_id, "");
  const seq = numberOrNull(event.seq);

  if (
    utteranceId === current.l0.utteranceId &&
    seq !== null &&
    current.l0.latestSeq !== null &&
    seq <= current.l0.latestSeq
  ) {
    return current;
  }
  if (type === "utterance_started") {
    const started = beginNativeUtterance(current, utteranceId || null);
    return { l0: { ...started.l0, availability: "ready", latestSeq: seq } };
  }
  const transcript =
    type === "text_appended" || type === "utterance_finalized"
      ? stringOr(event.text, current.l0.transcript)
      : type === "utterance_ended"
        ? stringOr(event.streamed_text, current.l0.transcript)
        : current.l0.transcript;
  const pressState =
    type === "utterance_ended"
      ? "finalizing"
      : type === "utterance_finalized" || type === "utterance_cancelled"
        ? "idle"
        : current.l0.pressState;
  return {
    l0: {
      availability: "ready",
      pressState,
      utteranceId: utteranceId || current.l0.utteranceId,
      transcript,
      latestSeq: seq ?? current.l0.latestSeq,
    },
  };
}

export function applyNativeSnapshot(
  current: NativeProjection,
  snapshot: Record<string, unknown>,
): NativeProjection {
  const l0 = (snapshot.l0 ?? {}) as Record<string, unknown>;
  return {
    l0: {
      ...current.l0,
      availability: stringOr(l0.availability, "unavailable"),
      pressState: stringOr(l0.press_state, "idle"),
      utteranceId: typeof l0.utterance_id === "string" ? l0.utterance_id : null,
      transcript: transcriptText(l0.stable_text, l0.tail),
      latestSeq: numberOrNull(l0.latest_seq),
    },
  };
}
