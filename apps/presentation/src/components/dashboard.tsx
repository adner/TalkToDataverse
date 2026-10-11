"use client";

import { randomUUID } from "@ag-ui/client";
import type { BaseEvent, Interrupt } from "@ag-ui/core";
import { useAgent, useInterrupt } from "@copilotkit/react-core/v2";
import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";

import {
  archetypeLabel,
  currentReading,
  type Decision,
  emptyHold,
  type HoldProjection,
  reduceHold,
  TABLE_LABELS,
  tableLabel,
} from "@/lib/hold-state";
import {
  applyNativeSnapshot,
  beginNativeUtterance,
  emptyNativeProjection,
  type NativeEnvelope,
  reduceNativeEvent,
} from "@/lib/native-state";
import {
  callDurationMs,
  callOutcome,
  callTarget,
  exportPath,
  isWrite,
  TOOL_VERBS,
} from "@/lib/record-tools";
import { FlightChart } from "@/components/flight-chart";
import { RecordModal } from "@/components/record-modal";
import { latestChart, parseOpened, type OpenedRecord } from "@/lib/chart";
import { onTypedInputChange, readTypedInput } from "@/lib/stage-settings";
import { reduceToolActivity, type ToolActivity } from "@/lib/tool-activity";
import { createWorkspaceFileLinksPlugin, fileHref } from "@/lib/workspace-file-links";

type SotExecution = {
  status?: string;
  phase?: string;
  requiresInput?: boolean;
  question?: string | null;
  model?: string;
  toolCalls?: number;
};

type SotState = {
  operationId?: string;
  execution?: SotExecution;
};

type AudioSettings = {
  status: string;
  initialized: boolean;
  armed: boolean;
  captureState: string;
  settings: {
    deviceId: number | null;
    prerollMs: number;
    tailFlushMs: number;
    minimumClipMs: number;
    blockMs: number;
    targetSampleRate: number;
  };
  device: { id: number | null; name: string | null; sampleRate: number | null };
  bufferedPrerollMs: number;
};

type Health = {
  environment: { name: string; url: string };
  layers: Record<string, { mode: string; model?: string; framework?: string }>;
};

type InputResult = {
  utteranceId: string;
  text: string;
};

type DiscardedInput = {
  utteranceId: string;
  discarded: true;
  reason: "too_short";
  durationMs: number;
  minimumMs: number;
};

/** The L1 cut a flight executes, as published in `segment_cut` (decision mode). */
type Segment = {
  chart?: boolean;
  utterance_id?: string;
  index?: number;
  thread?: number;
  text?: string;
  archetype?: string;
  archetype_p?: Record<string, number>;
  table?: string | null;
  mutation?: boolean;
  involved?: Record<string, number>;
  ref?: number | null;
  answers?: number | null;
  reason?: string;
  [key: string]: unknown;
};

type Flight = {
  utteranceId: string;
  text: string;
  segment: Segment;
  threadId: string;
  threadNumber: number;
  presentationSessionId: string;
  launchedAt: number;
};

type Microphone = "cold" | "loading" | "ready" | "recording" | "finalizing";

type ResolveInterrupt = (payload: unknown) => unknown;

const BACKEND_URL = (process.env.NEXT_PUBLIC_SOT_BACKEND_URL ?? "http://127.0.0.1:8000").replace(
  /\/$/,
  "",
);
const RACE_SIZE = 4;
// One colour per thread links a cut in the transcript to its flight card.
const THREAD_COLORS = ["#22d3ee", "#f472b6", "#facc15", "#4ade80", "#a78bfa", "#fb923c", "#60a5fa", "#2dd4bf"];

function humanize(value?: string | null) {
  if (!value || value === "-") return "Pending";
  return value
    .replaceAll("_", " ")
    .toLocaleLowerCase()
    .replace(/(^|\s)([a-z])/g, (_, prefix: string, letter: string) => `${prefix}${letter.toLocaleUpperCase()}`);
}

function percent(value: number | null | undefined) {
  return typeof value === "number" ? `${Math.round(value * 100)}%` : "—";
}

function threadTag(thread: number | null | undefined) {
  return typeof thread === "number" ? String(thread).padStart(2, "0") : "··";
}

function threadStyle(thread: number | null | undefined) {
  if (typeof thread !== "number" || thread < 1) return undefined;
  return { "--thread": THREAD_COLORS[(thread - 1) % THREAD_COLORS.length] } as React.CSSProperties;
}

function isDiscardedInput(value: InputResult | DiscardedInput): value is DiscardedInput {
  return "discarded" in value && value.discarded === true;
}

function SignalGlyph({ active = false }: { active?: boolean }) {
  return (
    <span className={active ? "signal-glyph is-active" : "signal-glyph"} aria-hidden="true">
      <i />
      <i />
      <i />
      <i />
      <i />
    </span>
  );
}

function MarkdownResponse({ content, streaming }: { content: string; streaming: boolean }) {
  const remarkPlugins = useMemo(() => [remarkGfm, createWorkspaceFileLinksPlugin(BACKEND_URL)], []);

  return (
    <div className="markdown-response">
      <ReactMarkdown
        remarkPlugins={remarkPlugins}
        components={{
          a: ({ href, ...props }) => {
            const opensWorkspaceFile = href?.startsWith(`${BACKEND_URL}/api/files/open?`);
            return (
              <a
                {...props}
                href={href}
                target={opensWorkspaceFile ? "_blank" : undefined}
                rel={opensWorkspaceFile ? "noreferrer" : undefined}
              />
            );
          },
        }}
      >
        {content}
      </ReactMarkdown>
      {streaming ? <span className="stream-caret" aria-hidden="true" /> : null}
    </div>
  );
}

// --- L1: the hold being listened to -----------------------------------------------------------

function ProbabilityRace({ reading, stale }: { reading: Decision | null; stale: boolean }) {
  const rows = (reading?.probabilities ?? []).slice(0, RACE_SIZE);

  return (
    <div className="probability-race" data-stale={stale ? "true" : "false"} aria-label="Archetype probabilities">
      {rows.length ? (
        rows.map(([archetype, probability], rank) => (
          <div
            className="race-row"
            data-leader={rank === 0 ? "true" : "false"}
            key={archetype}
            style={{ "--rank": rank, "--p": probability } as React.CSSProperties}
          >
            <span>{archetypeLabel(archetype)}</span>
            <i aria-hidden="true"><b /></i>
            <strong>{percent(probability)}</strong>
          </div>
        ))
      ) : (
        <div className="race-empty">Waiting for the first words</div>
      )}
    </div>
  );
}

function AnchoredTranscript({ hold, liveTranscript }: { hold: HoldProjection; liveTranscript: string }) {
  const live = liveTranscript.trim() ? liveTranscript.trim().split(/\s+/) : [];
  // Words L0 has heard but L1 has not decided on yet trail the decided ones.
  const words = live.length > hold.words.length ? [...hold.words, ...live.slice(hold.words.length)] : hold.words;
  const flips = new Map(hold.flips.map((flip) => [flip.word, flip]));
  const skipped = new Set(hold.skipped);
  const cuts = new Map(hold.segments.map((segment) => [segment.end - 1, segment]));
  const segmentOf = (index: number) =>
    hold.segments.find((segment) => index >= segment.start && index < segment.end);
  const scroller = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const element = scroller.current;
    if (element) element.scrollTop = element.scrollHeight;
  }, [words.length, hold.segments.length]);

  if (!words.length) {
    return <p className="anchored-transcript is-empty" aria-hidden="true" />;
  }

  return (
    <div className="anchored-transcript" aria-live="polite" ref={scroller}>
      {words.map((word, index) => {
        const flip = flips.get(index);
        const cut = cuts.get(index);
        const segment = segmentOf(index);
        const state = index >= hold.words.length
          ? "heard"
          : segment?.dropped
            ? "dropped"
            : segment
              ? "cut"
              : "current";
        return (
          <span className="word-group" key={`${index}-${word}`} style={threadStyle(segment?.answers ?? segment?.thread)}>
            <span className="word-slot" data-state={state}>
              <span className="word">
                {word}
                {skipped.has(index) ? <i className="word-skipped" title="Decision skipped" /> : null}
              </span>
              {flip ? (
                <small className="word-flip">
                  {archetypeLabel(flip.archetype)}
                  {flip.latencyMs !== null ? ` · ${Math.round(flip.latencyMs)} ms` : ""}
                </small>
              ) : null}
            </span>
            {cut ? (
              <b className="word-cut" data-dropped={cut.dropped ? "true" : "false"}>
                {cut.dropped ? "dropped" : cut.answers ? `answers T${threadTag(cut.answers)}` : `T${threadTag(cut.thread)}`}
              </b>
            ) : null}
          </span>
        );
      })}
      {hold.revisions.length ? (
        <span className="revision-note">
          Second pass · {hold.revisions.map((revision) => `${revision.op} “${revision.words.join(" ")}”`).join(" · ")}
        </span>
      ) : null}
    </div>
  );
}

function TableChips({ reading, stale }: { reading: Decision | null; stale: boolean }) {
  return (
    <div className="table-chips" data-stale={stale ? "true" : "false"} aria-label="Tables involved">
      {Object.keys(TABLE_LABELS).map((table) => {
        const p = reading?.involved[table];
        const state = !reading
          ? "off"
          : reading.table === table
            ? "primary"
            : reading.lit.includes(table)
              ? "lit"
              : typeof p === "number" && p >= 0.5
                ? "warm"
                : "off";
        return (
          <span className="table-chip" data-state={state} key={table} title={typeof p === "number" ? percent(p) : undefined}>
            {tableLabel(table)}
          </span>
        );
      })}
    </div>
  );
}

function HoldPanel({
  hold,
  liveTranscript,
  model,
  listening,
}: {
  hold: HoldProjection;
  liveTranscript: string;
  model: string;
  listening: boolean;
}) {
  const reading = currentReading(hold) ?? hold.latest;
  const stale = currentReading(hold) === null;

  return (
    <section className="hold-panel" data-listening={listening ? "true" : "false"} aria-label="L1 live decisions">
      <header className="zone-caption">
        <span className="layer-mark">L1</span>
        <strong>Intent</strong>
        <span className="zone-model" title={model}>{model}</span>
        <span className="zone-stat">
          {listening ? <i className="live-dot" aria-hidden="true" /> : null}
          {reading
            ? `word ${reading.end} · ${reading.latencyMs !== null ? `${Math.round(reading.latencyMs)} ms` : "—"} · ${hold.calls} calls`
            : null}
        </span>
        <div className="hold-reading">
          {reading?.mutation ? <span className="write-badge">✎ Write</span> : null}
          {reading?.chart ? <span className="chart-badge">📊 Chart</span> : null}
          {reading?.ref ? <span className="ref-badge" style={threadStyle(reading.ref)}>uses T{threadTag(reading.ref)}</span> : null}
          {reading?.answers ? (
            <span className="ref-badge" style={threadStyle(reading.answers)}>answers T{threadTag(reading.answers)}</span>
          ) : null}
          {reading?.complete !== null && reading?.complete !== undefined ? (
            <span className="complete-badge">complete {percent(reading.complete)}</span>
          ) : null}
        </div>
      </header>
      <div className="hold-body">
        <ProbabilityRace reading={reading} stale={stale} />
        <AnchoredTranscript hold={hold} liveTranscript={liveTranscript} />
      </div>
      <TableChips reading={reading} stale={stale} />
    </section>
  );
}

// --- L2: one flight per cut ---------------------------------------------------------------------

function InterruptCard({ interrupt, resolve }: { interrupt?: Interrupt; resolve: ResolveInterrupt }) {
  const [answer, setAnswer] = useState("");

  return (
    <aside className="flight-interrupt" aria-live="polite">
      <div className="interrupt-flare" aria-hidden="true" />
      <div>
        <span>Operation paused · input needed</span>
        <strong>{interrupt?.message ?? "The agent needs your response."}</strong>
      </div>
      <form
        className="interrupt-answer"
        onSubmit={(event) => {
          event.preventDefault();
          if (answer.trim()) void resolve(answer.trim());
        }}
      >
        <input aria-label="Answer the agent" value={answer} onChange={(event) => setAnswer(event.target.value)} />
        <button type="submit" disabled={!answer.trim()}>Continue</button>
      </form>
    </aside>
  );
}

function ResolvingInterrupt({ interrupt, resolve }: { interrupt?: Interrupt; resolve: ResolveInterrupt }) {
  const resolvedApprovalId = useRef<string | null>(null);
  const isApproval = interrupt?.reason === "tool_call" || interrupt?.reason === "confirmation";

  useEffect(() => {
    if (!isApproval || !interrupt || resolvedApprovalId.current === interrupt.id) return;
    resolvedApprovalId.current = interrupt.id;
    void resolve({ approved: true });
  }, [interrupt, isApproval, resolve]);

  if (isApproval) return null;
  return <InterruptCard interrupt={interrupt} resolve={resolve} />;
}

function OperationInterrupt({ agentId }: { agentId: string }) {
  return useInterrupt({
    agentId,
    renderInChat: false,
    render: ({ interrupt, resolve }) => <ResolvingInterrupt interrupt={interrupt ?? undefined} resolve={resolve} />,
  });
}

function FlightQuestion({ flight, question }: { flight: Flight; question: string }) {
  const [answer, setAnswer] = useState("");
  const [sending, setSending] = useState(false);

  async function send(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = answer.trim();
    if (!text || sending) return;
    setSending(true);
    try {
      await fetch(`${BACKEND_URL}/api/flights/answer`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ session_id: flight.presentationSessionId, thread: flight.threadNumber, text }),
      });
      setAnswer("");
    } finally {
      setSending(false);
    }
  }

  return (
    <aside className="flight-interrupt" aria-live="polite">
      <div className="interrupt-flare" aria-hidden="true" />
      <div>
        <span>Thread {threadTag(flight.threadNumber)} asks · answer by voice or here</span>
        <strong>{question}</strong>
      </div>
      <form className="interrupt-answer" onSubmit={send}>
        <input aria-label="Answer the agent" value={answer} onChange={(event) => setAnswer(event.target.value)} disabled={sending} />
        <button type="submit" disabled={sending || !answer.trim()}>Answer</button>
      </form>
    </aside>
  );
}

function SegmentChips({
  segment,
  onReference,
}: {
  segment: Segment;
  onReference: (thread: number | null) => void;
}) {
  const archetype = segment.archetype ?? null;
  const p = archetype ? segment.archetype_p?.[archetype] : undefined;
  const involved = Object.entries(segment.involved ?? {})
    .filter(([table]) => table !== segment.table)
    .sort((a, b) => b[1] - a[1]);

  return (
    <div className="segment-chips">
      <span className="archetype-chip" data-archetype={archetype ?? "none"}>
        {archetypeLabel(archetype)}
        {typeof p === "number" ? <small>{percent(p)}</small> : null}
      </span>
      {segment.table ? <span className="table-chip" data-state="primary">{tableLabel(segment.table)}</span> : null}
      {involved.map(([table, probability]) => (
        <span className="table-chip" data-state={probability >= 0.7 ? "lit" : "warm"} key={table}>
          {tableLabel(table)}
          <small>{percent(probability)}</small>
        </span>
      ))}
      {segment.mutation ? <span className="write-badge">✎ Write</span> : null}
      {segment.chart ? <span className="chart-badge">📊 Chart</span> : null}
      {segment.ref ? (
        <button
          className="ref-badge"
          style={threadStyle(segment.ref)}
          onBlur={() => onReference(null)}
          onFocus={() => onReference(segment.ref ?? null)}
          onMouseEnter={() => onReference(segment.ref ?? null)}
          onMouseLeave={() => onReference(null)}
          type="button"
        >
          uses T{threadTag(segment.ref)}
        </button>
      ) : null}
    </div>
  );
}

function ToolRow({
  activity,
  nowMs,
  onOpen,
}: {
  activity: ToolActivity;
  nowMs: number;
  onOpen: (record: OpenedRecord) => void;
}) {
  const path = exportPath(activity);
  const outcome = callOutcome(activity);
  const opened = parseOpened(activity);

  return (
    <li className="tool-row" data-status={activity.status} data-write={isWrite(activity) ? "true" : "false"}>
      <span className="tool-verb">{TOOL_VERBS[activity.name] ?? activity.name}</span>
      <span className="tool-target">{callTarget(activity) || activity.name}</span>
      <span className="tool-outcome">
        {path ? (
          <a href={fileHref(path, BACKEND_URL)} rel="noreferrer" target="_blank">
            {outcome} · {path.split("/").at(-1)} ↗
          </a>
        ) : opened ? (
          <button className="tool-open" onClick={() => onOpen(opened)} type="button">
            {outcome} ↗
          </button>
        ) : (
          outcome
        )}
      </span>
      <time>{Math.round(callDurationMs(activity, nowMs))} ms</time>
    </li>
  );
}

function FlightCard({
  flight,
  highlighted,
  onReference,
  onOpenRecord,
}: {
  flight: Flight;
  highlighted: boolean;
  onReference: (thread: number | null) => void;
  onOpenRecord: (record: OpenedRecord, thread: number) => void;
}) {
  const agentId = `sot-flight-${flight.utteranceId}`;
  const { agent, isReady } = useAgent({ agentId, runtimeAgentId: "default", threadId: flight.threadId });
  const launched = useRef(false);
  const [toolActivity, setToolActivity] = useState<ToolActivity[]>([]);
  const [runError, setRunError] = useState<string | null>(null);
  const [nowMs, setNowMs] = useState(0);
  const openedCalls = useRef(new Set<string>());

  // A record L2 opens shows up once, as soon as its call returns; the row keeps a reopen button.
  useEffect(() => {
    for (const activity of toolActivity) {
      const opened = parseOpened(activity);
      if (!opened || openedCalls.current.has(activity.id)) continue;
      openedCalls.current.add(activity.id);
      onOpenRecord(opened, flight.threadNumber);
    }
  }, [toolActivity, onOpenRecord, flight.threadNumber]);

  useEffect(() => {
    const subscription = agent.subscribe({
      onEvent: ({ event }: { event: BaseEvent }) => {
        const elapsedMs = Math.max(0, Date.now() - flight.launchedAt);
        setToolActivity((current) => reduceToolActivity(current, event, elapsedMs));
      },
      onRunFailed: ({ error }: { error: Error }) => setRunError(error.message),
    });
    return subscription.unsubscribe;
  }, [agent, flight.launchedAt]);

  useEffect(() => {
    if (!isReady || launched.current) return;
    launched.current = true;
    agent.addMessage({ id: flight.utteranceId, role: "user", content: flight.text });
    void agent
      .runAgent({
        runId: randomUUID(),
        tools: [],
        context: [],
        forwardedProps: {
          sotSegment: flight.segment,
          sotPresentationSessionId: flight.presentationSessionId,
        },
      })
      .catch((caught: unknown) => setRunError(caught instanceof Error ? caught.message : "The operation could not start."));
  }, [agent, flight, isReady]);

  useEffect(() => {
    if (!agent.isRunning) return;
    const tick = () => setNowMs(Math.max(0, Date.now() - flight.launchedAt));
    tick();
    const timer = window.setInterval(tick, 100);
    return () => window.clearInterval(timer);
  }, [agent.isRunning, flight.launchedAt]);

  const sot = ((agent.state as { sot?: SotState } | undefined)?.sot ?? {}) as SotState;
  const response = agent.messages
    .filter((message) => message.role === "assistant" && typeof message.content === "string")
    .map((message) => String(message.content))
    .join("\n\n");
  const status = runError ? "failed" : (sot.execution?.status ?? (agent.isRunning ? "running" : "queued"));
  const phase = sot.execution?.phase ?? (agent.isRunning ? "Planning" : "Queued");
  const isComplete = ["succeeded", "failed", "cancelled"].includes(status);
  const calls = [...toolActivity].reverse();
  const elapsed = isComplete ? (calls.at(-1)?.finishedMs ?? nowMs) : nowMs;
  const chart = latestChart(toolActivity);
  const openRow = (record: OpenedRecord) => onOpenRecord(record, flight.threadNumber);

  return (
    <article
      className="flight-card"
      data-highlight={highlighted ? "true" : "false"}
      data-status={status}
      data-thread={flight.threadNumber}
      style={threadStyle(flight.threadNumber)}
    >
      <header className="flight-header">
        <span
          className="flight-number"
          title={`Segment ${Number(flight.segment.index ?? 0) + 1} · cut on ${flight.segment.reason ?? "pause"}`}
        >
          T{threadTag(flight.threadNumber)}
        </span>
        <h2 className="flight-query">{flight.text}</h2>
        <div className="flight-status" data-status={status}>
          <SignalGlyph active={!isComplete} />
          <strong>{isComplete ? humanize(status) : humanize(phase)}</strong>
          <time>{(elapsed / 1000).toFixed(1)} s</time>
        </div>
      </header>

      <SegmentChips onReference={onReference} segment={flight.segment} />

      <div className="flight-body" data-chart={chart ? "true" : "false"}>
        <div className="flight-main">
          <section className="flight-l2" aria-label="L2 tool calls">
            {calls.length ? (
              <ol className="tool-rows">
                {calls.map((activity) => (
                  <ToolRow activity={activity} key={activity.id} nowMs={nowMs} onOpen={openRow} />
                ))}
              </ol>
            ) : (
              <p className="tool-rows-empty">{runError ? "Failed before a tool call" : isComplete ? "Answered without a tool call" : "Choosing tools…"}</p>
            )}
          </section>

          {isReady ? <OperationInterrupt agentId={agentId} /> : null}
          {sot.execution?.question ? <FlightQuestion flight={flight} question={sot.execution.question} /> : null}
          {runError ? <div className="flight-error">{runError}</div> : null}

          <section className={response ? "flight-answer has-response" : "flight-answer"} aria-label="Answer">
            {response ? (
              <MarkdownResponse content={response} streaming={agent.isRunning} />
            ) : (
              <p className="response-placeholder">The answer streams here.</p>
            )}
          </section>
        </div>
        {chart ? <FlightChart chart={chart} /> : null}
      </div>
    </article>
  );
}

// --- Input ---------------------------------------------------------------------------------------

function TalkButton({
  microphone,
  onPressStart,
  onPressEnd,
  onPressCancel,
}: {
  microphone: Microphone;
  onPressStart: () => void;
  onPressEnd: () => void;
  onPressCancel: () => void;
}) {
  const recording = microphone === "recording";
  return (
    <button
      className="talk-button"
      data-state={microphone}
      type="button"
      onPointerDown={(event) => {
        if (event.button !== 0 || microphone !== "ready") return;
        event.preventDefault();
        event.currentTarget.setPointerCapture(event.pointerId);
        onPressStart();
      }}
      onPointerUp={(event) => {
        event.preventDefault();
        if (event.currentTarget.hasPointerCapture(event.pointerId)) {
          event.currentTarget.releasePointerCapture(event.pointerId);
        }
        onPressEnd();
      }}
      onPointerCancel={onPressCancel}
      onContextMenu={(event) => event.preventDefault()}
      onKeyDown={(event) => {
        if ((event.key === " " || event.key === "Enter") && !event.repeat && microphone === "ready") {
          event.preventDefault();
          onPressStart();
        }
      }}
      onKeyUp={(event) => {
        if (event.key === " " || event.key === "Enter") {
          event.preventDefault();
          onPressEnd();
        }
      }}
      disabled={microphone === "cold" || microphone === "loading" || microphone === "finalizing"}
      aria-label="Push and hold to talk; pause between requests, release when done"
    >
      <SignalGlyph active={recording} />
      <strong>
        {microphone === "cold"
          ? "Voice offline"
          : microphone === "loading"
            ? "Arming microphone"
            : recording
              ? "Listening"
              : microphone === "finalizing"
                ? "Finishing"
                : "Hold to talk"}
      </strong>
    </button>
  );
}

export function Dashboard() {
  const [command, setCommand] = useState("Show me the safety critical warranty cases");
  const [native, setNative] = useState(emptyNativeProjection);
  const [hold, setHold] = useState<HoldProjection>(emptyHold);
  const [flights, setFlights] = useState<Flight[]>([]);
  const [highlightedThread, setHighlightedThread] = useState<number | null>(null);
  const [openRecord, setOpenRecord] = useState<{ record: OpenedRecord; thread: number } | null>(null);
  const showRecord = useCallback((record: OpenedRecord, thread: number) => setOpenRecord({ record, thread }), []);
  const closeRecord = useCallback(() => setOpenRecord(null), []);
  const [drafting, setDrafting] = useState(false);
  // Typed input is off unless enabled on /admin (testing without a microphone).
  const [typedInput, setTypedInput] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [captureNotice, setCaptureNotice] = useState<string | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [microphone, setMicrophone] = useState<Microphone>("cold");
  const [audioSettings, setAudioSettings] = useState<AudioSettings | null>(null);
  const activeUtterance = useRef<string | null>(null);
  const holdActive = useRef(false);
  const releaseRequested = useRef(false);
  const releaseInFlight = useRef(false);
  const presentationSessionId = useRef(randomUUID());

  useEffect(() => {
    const source = new EventSource(`${BACKEND_URL}/api/presentation/events`);
    source.onmessage = (message) => {
      const envelope = JSON.parse(message.data) as NativeEnvelope;
      setNative((current) => reduceNativeEvent(current, envelope));
      setHold((current) => reduceHold(current, envelope));
      const segment = envelope.event.segment as Segment | undefined;
      if (
        envelope.source === "l1" &&
        envelope.event.type === "segment_cut" &&
        segment?.session_id === presentationSessionId.current &&
        segment.answers == null
      ) {
        // One flight per L1 segment, numbered by the backend when it was cut.
        setFlights((current) => [
          ...current,
          {
            utteranceId: `${String(segment.utterance_id)}:${String(segment.index)}`,
            text: String(segment.text ?? ""),
            segment,
            threadId: randomUUID(),
            threadNumber: Number(segment.thread),
            presentationSessionId: presentationSessionId.current,
            launchedAt: Date.now(),
          },
        ]);
      }
    };
    source.onerror = () => setError("The live L0/L1 signal is offline. Check the backend connection.");

    void fetch(`${BACKEND_URL}/api/presentation/snapshot`)
      .then((response) => response.json())
      .then((snapshot: Record<string, unknown>) => setNative((current) => applyNativeSnapshot(current, snapshot)))
      .catch(() => setError("The backend snapshot is unavailable."));

    void fetch(`${BACKEND_URL}/health`)
      .then((response) => response.json())
      .then(async (value: Health) => {
        setHealth(value);
        setMicrophone("loading");
        const response = await fetch(`${BACKEND_URL}/api/audio/initialize`, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({}),
        });
        const audio = (await response.json()) as AudioSettings & { detail?: string };
        if (!response.ok || audio.status !== "ready") {
          throw new Error(audio.detail ?? "The audio ring buffer could not be armed.");
        }
        setAudioSettings(audio);
        setMicrophone("ready");
      })
      .catch((caught: unknown) =>
        setError(caught instanceof Error ? caught.message : "The SpeedOfThought backend is unavailable."),
      );

    void fetch(`${BACKEND_URL}/api/audio/settings`)
      .then((response) => response.json())
      .then((value: AudioSettings) => setAudioSettings(value))
      .catch(() => undefined);

    return () => source.close();
  }, []);

  const cancelPushToTalk = useCallback(() => {
    if (releaseInFlight.current) return;
    if (!holdActive.current && !activeUtterance.current) return;
    holdActive.current = false;
    releaseRequested.current = false;
    activeUtterance.current = null;
    releaseInFlight.current = false;
    void fetch(`${BACKEND_URL}/api/audio/cancel`, { method: "POST" });
    setMicrophone("ready");
  }, []);

  useEffect(() => {
    const cancelOnBlur = () => cancelPushToTalk();
    window.addEventListener("blur", cancelOnBlur);
    return () => window.removeEventListener("blur", cancelOnBlur);
  }, [cancelPushToTalk]);

  async function submitText(text: string) {
    const response = await fetch(`${BACKEND_URL}/api/utterance`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ text, session_id: presentationSessionId.current }),
    });
    if (!response.ok) {
      const problem = (await response.json().catch(() => ({}))) as { detail?: string };
      throw new Error(problem.detail ?? "L0/L1 rejected the utterance.");
    }
    // Flights launch from the segment_cut events this request produces.
    await response.json();
  }

  useEffect(() => {
    const timeout = window.setTimeout(() => setTypedInput(readTypedInput()), 0);
    const stop = onTypedInputChange(setTypedInput);
    return () => {
      window.clearTimeout(timeout);
      stop();
    };
  }, []);

  async function runCommand(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const text = command.trim();
    if (!text || drafting) return;
    setError(null);
    setDrafting(true);
    setCommand("");
    try {
      await submitText(text);
    } catch (caught) {
      setCommand(text);
      setError(caught instanceof Error ? caught.message : "The operation could not start.");
    } finally {
      setDrafting(false);
    }
  }

  async function finishPushToTalk(utteranceId: string) {
    if (releaseInFlight.current) return;
    releaseInFlight.current = true;
    setMicrophone("finalizing");
    setError(null);
    setCaptureNotice(null);
    try {
      const response = await fetch(`${BACKEND_URL}/api/audio/release`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ utterance_id: utteranceId }),
      });
      const value = (await response.json()) as (InputResult | DiscardedInput) & { detail?: string };
      if (!response.ok) throw new Error(value.detail ?? "The recording could not be finalized.");
      if (isDiscardedInput(value)) {
        setCaptureNotice(`Discarded ${value.durationMs} ms tap · minimum clip is ${value.minimumMs} ms`);
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The microphone is unavailable.");
    } finally {
      activeUtterance.current = null;
      releaseRequested.current = false;
      releaseInFlight.current = false;
      setMicrophone("ready");
    }
  }

  async function beginPushToTalk() {
    if (microphone !== "ready" || holdActive.current) return;
    holdActive.current = true;
    releaseRequested.current = false;
    setNative((current) => beginNativeUtterance(current));
    setHold(emptyHold);
    setError(null);
    setCaptureNotice(null);
    setMicrophone("recording");
    try {
      const response = await fetch(`${BACKEND_URL}/api/audio/press`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({ session_id: presentationSessionId.current }),
      });
      const value = (await response.json()) as { utteranceId?: string; detail?: string };
      if (!response.ok || !value.utteranceId) {
        throw new Error(value.detail ?? "Microphone capture could not start.");
      }
      activeUtterance.current = value.utteranceId;
      if (releaseRequested.current || !holdActive.current) {
        await finishPushToTalk(value.utteranceId);
      }
    } catch (caught) {
      holdActive.current = false;
      activeUtterance.current = null;
      releaseRequested.current = false;
      setMicrophone("ready");
      setError(caught instanceof Error ? caught.message : "The microphone is unavailable.");
    }
  }

  function endPushToTalk() {
    if (!holdActive.current) return;
    holdActive.current = false;
    const utteranceId = activeUtterance.current;
    if (utteranceId) {
      void finishPushToTalk(utteranceId);
    } else {
      releaseRequested.current = true;
      setMicrophone("finalizing");
    }
  }

  const listening = microphone === "recording" || microphone === "finalizing";
  const l1Model = health?.layers.l1?.model ?? "Decision-1";
  const l2Model = health?.layers.l2?.model ?? "gpt-6-luna";

  return (
    <main className="stage">
      <header className="stage-header">
        <h1>Dataverse at the <em>Speed of Thought</em> <span aria-hidden="true">💭💭💭</span></h1>
        {health ? <span className="stage-environment">{health.environment.name}</span> : null}
      </header>

      <HoldPanel hold={hold} listening={listening} liveTranscript={listening ? native.l0.transcript : ""} model={l1Model} />

      <section className="airspace" aria-label="Flights">
        <header className="zone-caption">
          <span className="layer-mark">L2</span>
          <strong>Flights</strong>
          <span className="zone-model">{l2Model}</span>
          <span className="zone-stat">
            {flights.length ? `${flights.length} ${flights.length === 1 ? "flight" : "flights"} · newest on top` : null}
          </span>
        </header>
        {flights.length ? (
          <div className="flight-stack">
            {[...flights].reverse().map((flight) => (
              <FlightCard
                flight={flight}
                highlighted={highlightedThread === flight.threadNumber}
                key={flight.utteranceId}
                onOpenRecord={showRecord}
                onReference={setHighlightedThread}
              />
            ))}
          </div>
        ) : (
          <div className="empty-airspace">
            <strong>Hold to talk</strong>
          </div>
        )}
      </section>

      <section className="input-dock" data-recording={listening ? "true" : "false"}>
        {error ? <div className="dock-error"><span>Signal fault</span>{error}<button type="button" onClick={() => setError(null)} aria-label="Dismiss error">×</button></div> : null}
        {captureNotice ? <div className="dock-notice"><span>Capture filtered</span>{captureNotice}<button type="button" onClick={() => setCaptureNotice(null)} aria-label="Dismiss notice">×</button></div> : null}
        <div className="dock-row" data-typed={typedInput ? "true" : "false"}>
          {typedInput ? (
            <form onSubmit={runCommand}>
              <input
                aria-label="Type a request"
                value={command}
                onChange={(event) => setCommand(event.target.value)}
                placeholder="Type a request about Contoso Bikes…"
              />
              <button type="submit" disabled={!command.trim() || drafting}>
                {drafting ? "Deciding" : "Launch"}<span aria-hidden="true">↗</span>
              </button>
            </form>
          ) : null}
          <TalkButton
            microphone={microphone}
            onPressStart={() => void beginPushToTalk()}
            onPressEnd={endPushToTalk}
            onPressCancel={cancelPushToTalk}
          />
          <div className="dock-links">
            <small title={`${audioSettings?.settings.prerollMs ?? 0} ms pre-roll`}>
              {audioSettings?.device.name ?? "Microphone not armed"}
            </small>
            <Link href="/admin">Admin ↗</Link>
            <button
              type="button"
              onClick={() => {
                setFlights([]);
                setHold(emptyHold);
              }}
              disabled={!flights.length && !hold.words.length}
            >
              Clear
            </button>
          </div>
        </div>
      </section>
      {openRecord ? (
        <RecordModal onClose={closeRecord} record={openRecord.record} thread={threadTag(openRecord.thread)} />
      ) : null}
    </main>
  );
}
