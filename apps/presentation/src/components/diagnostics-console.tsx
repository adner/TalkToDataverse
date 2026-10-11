"use client";

import Link from "next/link";
import { FormEvent, useCallback, useEffect, useMemo, useState } from "react";

type DiagnosticSettings = {
  enabled: boolean;
  level: "DEBUG" | "INFO" | "WARNING" | "ERROR";
  console: boolean;
  file: boolean;
  filePath: string;
  maxFileBytes: number;
  backupCount: number;
  memoryEvents: number;
  includeTranscripts: boolean;
  includeIntentPayloads: boolean;
  includeToolPayloads: boolean;
  includeResponses: boolean;
};

type DiagnosticEvent = {
  id: number;
  timestamp: string;
  sinceStartMs: number;
  level: string;
  layer: string;
  event: string;
  message: string;
  utteranceId?: string;
  operationId?: string;
  runId?: string;
  toolCallId?: string;
  durationMs?: number;
  data?: unknown;
};

type DiagnosticResponse = {
  events: DiagnosticEvent[];
  latestId: number;
  matched: number;
  retained: number;
  capacity: number;
};

const layers = ["ALL", "L0", "L1", "L2", "SYSTEM"];
const levels = ["DEBUG", "INFO", "WARNING", "ERROR"] as const;

function shortId(value?: string) {
  if (!value) return "—";
  return value.length > 16 ? `${value.slice(0, 7)}…${value.slice(-6)}` : value;
}

function formatDuration(value?: number) {
  if (value === undefined) return null;
  return value >= 1_000 ? `${(value / 1_000).toFixed(2)} s` : `${Math.round(value)} ms`;
}

function formatClock(value: string) {
  return new Intl.DateTimeFormat(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    fractionalSecondDigits: 3,
  }).format(new Date(value));
}

function eventTrace(event: DiagnosticEvent) {
  return event.utteranceId ?? event.operationId ?? event.runId ?? "system";
}

export function DiagnosticsConsole() {
  const backendUrl = process.env.NEXT_PUBLIC_SOT_BACKEND_URL ?? "http://127.0.0.1:8000";
  const [events, setEvents] = useState<DiagnosticEvent[]>([]);
  const [settings, setSettings] = useState<DiagnosticSettings | null>(null);
  const [retained, setRetained] = useState(0);
  const [capacity, setCapacity] = useState(0);
  const [layer, setLayer] = useState("ALL");
  const [minimumLevel, setMinimumLevel] = useState("DEBUG");
  const [trace, setTrace] = useState("");
  const [search, setSearch] = useState("");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [paused, setPaused] = useState(false);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    const response = await fetch(`${backendUrl}/api/diagnostics/events?limit=5000`, {
      cache: "no-store",
    });
    if (!response.ok) throw new Error("The diagnostic event store is unavailable.");
    const value = (await response.json()) as DiagnosticResponse;
    setEvents(value.events);
    setRetained(value.retained);
    setCapacity(value.capacity);
  }, [backendUrl]);

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      void Promise.all([
        refresh(),
        fetch(`${backendUrl}/api/diagnostics/settings`, { cache: "no-store" })
          .then((response) => {
            if (!response.ok) throw new Error("Diagnostic settings are unavailable.");
            return response.json() as Promise<DiagnosticSettings>;
          })
          .then(setSettings),
      ]).catch((caught: unknown) =>
        setError(caught instanceof Error ? caught.message : "The flight recorder could not connect."),
      );
    }, 0);
    return () => window.clearTimeout(timeout);
  }, [backendUrl, refresh]);

  useEffect(() => {
    if (paused) return;
    const interval = window.setInterval(() => void refresh().catch(() => undefined), 750);
    return () => window.clearInterval(interval);
  }, [paused, refresh]);

  const traceOptions = useMemo(() => {
    const options = new Map<string, { id: string; label: string }>();
    for (const event of events) {
      if (event.utteranceId) {
        options.set(event.utteranceId, {
          id: event.utteranceId,
          label: `Utterance ${shortId(event.utteranceId)}`,
        });
      }
    }
    return [...options.values()].reverse();
  }, [events]);

  const visibleEvents = useMemo(() => {
    const minimum = levels.indexOf(minimumLevel as (typeof levels)[number]);
    const needle = search.toLocaleLowerCase();
    return events.filter((event) => {
      if (layer !== "ALL" && event.layer !== layer) return false;
      if (levels.indexOf(event.level as (typeof levels)[number]) < minimum) return false;
      if (
        trace &&
        ![event.utteranceId, event.operationId, event.runId, event.toolCallId].includes(trace)
      ) return false;
      if (needle && !JSON.stringify(event).toLocaleLowerCase().includes(needle)) return false;
      return true;
    });
  }, [events, layer, minimumLevel, search, trace]);

  const selected = events.find((event) => event.id === selectedId) ?? visibleEvents.at(-1) ?? null;
  const errorCount = events.filter((event) => event.level === "ERROR").length;
  const activeTraceCount = new Set(events.map(eventTrace).filter((value) => value !== "system")).size;

  async function saveSettings(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!settings) return;
    setSaving(true);
    setError(null);
    setMessage(null);
    try {
      const response = await fetch(`${backendUrl}/api/diagnostics/settings`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          enabled: settings.enabled,
          level: settings.level,
          console: settings.console,
          file: settings.file,
          file_path: settings.filePath,
          max_file_bytes: settings.maxFileBytes,
          backup_count: settings.backupCount,
          memory_events: settings.memoryEvents,
          include_transcripts: settings.includeTranscripts,
          include_intent_payloads: settings.includeIntentPayloads,
          include_tool_payloads: settings.includeToolPayloads,
          include_responses: settings.includeResponses,
        }),
      });
      const value = (await response.json()) as DiagnosticSettings & { detail?: string };
      if (!response.ok) throw new Error(value.detail ?? "Logging settings were rejected.");
      setSettings(value);
      setMessage("Logging settings applied without restarting the backend.");
      await refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Logging settings could not be saved.");
    } finally {
      setSaving(false);
    }
  }

  async function clearEvents() {
    if (!window.confirm("Clear the in-memory diagnostic timeline? Rotating log files are retained.")) return;
    const response = await fetch(`${backendUrl}/api/diagnostics/clear`, { method: "POST" });
    if (!response.ok) {
      setError("The in-memory trace could not be cleared.");
      return;
    }
    setEvents([]);
    setSelectedId(null);
    setRetained(0);
  }

  return (
    <main className="recorder-console">
      <div className="ambient-grid" aria-hidden="true" />
      <header className="admin-header recorder-header">
        <Link href="/" className="back-link"><span aria-hidden="true">←</span> Live cognition field</Link>
        <div className="brand-lockup">
          <div className="brand-sigil" aria-hidden="true"><i /><i /><i /></div>
          <div><span>SpeedOfThought</span><strong>Flight recorder</strong></div>
        </div>
        <nav className="recorder-nav" aria-label="Control rooms">
          <Link href="/admin">Audio</Link>
          <span className={settings?.enabled ? "admin-live is-armed" : "admin-live"}>
            <i />{paused ? "View paused" : settings?.enabled ? "Recording traces" : "Logging disabled"}
          </span>
        </nav>
      </header>

      <section className="recorder-hero">
        <div>
          <span>End-to-end causality</span>
          <h1>Find the first<br />wrong turn.</h1>
        </div>
        <div className="recorder-counters" aria-label="Diagnostic summary">
          <div><strong>{String(activeTraceCount).padStart(2, "0")}</strong><span>Correlated traces</span></div>
          <div><strong>{retained}</strong><span>Events retained</span></div>
          <div data-alert={errorCount > 0 ? "true" : "false"}><strong>{errorCount}</strong><span>Errors</span></div>
        </div>
      </section>

      <section className="recorder-toolbar" aria-label="Trace filters">
        <div className="layer-switcher">
          {layers.map((item) => (
            <button className={layer === item ? "is-active" : ""} key={item} onClick={() => setLayer(item)} type="button">{item}</button>
          ))}
        </div>
        <label><span>Trace</span><select value={trace} onChange={(event) => setTrace(event.target.value)}><option value="">All concurrent work</option>{traceOptions.map((option) => <option value={option.id} key={option.id}>{option.label}</option>)}</select></label>
        <label><span>Minimum</span><select value={minimumLevel} onChange={(event) => setMinimumLevel(event.target.value)}>{levels.map((item) => <option key={item}>{item}</option>)}</select></label>
        <label className="recorder-search"><span>Search payload</span><input value={search} onChange={(event) => setSearch(event.target.value)} placeholder="classifier, tool, error…" /></label>
        <button className={paused ? "pause-traces is-paused" : "pause-traces"} type="button" onClick={() => setPaused((value) => !value)}>{paused ? "Resume live" : "Pause view"}</button>
      </section>

      {error ? <div className="recorder-message is-error">{error}<button type="button" onClick={() => setError(null)}>×</button></div> : null}
      {message ? <div className="recorder-message is-success">{message}<button type="button" onClick={() => setMessage(null)}>×</button></div> : null}

      <div className="recorder-grid">
        <aside className="recorder-settings">
          <header><span>Capture policy</span><strong>What gets remembered</strong></header>
          {settings ? (
            <form onSubmit={saveSettings}>
              <label className="setting-toggle"><span><strong>Logging enabled</strong><small>Master switch for new events</small></span><input type="checkbox" checked={settings.enabled} onChange={(event) => setSettings({ ...settings, enabled: event.target.checked })} /></label>
              <label><span>Capture level</span><select value={settings.level} onChange={(event) => setSettings({ ...settings, level: event.target.value as DiagnosticSettings["level"] })}>{levels.map((item) => <option key={item}>{item}</option>)}</select></label>
              <div className="payload-matrix">
                <span>Safe payloads</span>
                {([
                  ["includeTranscripts", "Transcripts"],
                  ["includeIntentPayloads", "Intent + profile"],
                  ["includeToolPayloads", "Tool arguments + results"],
                  ["includeResponses", "Agent responses"],
                ] as const).map(([key, label]) => <label key={key}><input type="checkbox" checked={settings[key]} onChange={(event) => setSettings({ ...settings, [key]: event.target.checked })} /><span>{label}</span></label>)}
              </div>
              <label className="setting-toggle"><span><strong>Console JSON</strong><small>One structured line per event</small></span><input type="checkbox" checked={settings.console} onChange={(event) => setSettings({ ...settings, console: event.target.checked })} /></label>
              <label className="setting-toggle"><span><strong>Rotating file</strong><small>Persists across browser refreshes</small></span><input type="checkbox" checked={settings.file} onChange={(event) => setSettings({ ...settings, file: event.target.checked })} /></label>
              <label><span>JSONL path</span><input value={settings.filePath} onChange={(event) => setSettings({ ...settings, filePath: event.target.value })} disabled={!settings.file} /></label>
              <label><span>Memory capacity</span><input type="number" min={100} max={100000} step={100} value={settings.memoryEvents} onChange={(event) => setSettings({ ...settings, memoryEvents: Number(event.target.value) })} /></label>
              <button className="apply-logging" disabled={saving} type="submit"><span>{saving ? "Applying…" : "Apply logging"}</span><i>↗</i></button>
            </form>
          ) : <div className="recorder-empty">Loading capture policy…</div>}
          <p className="privacy-note">Secrets, tokens, raw records, prompts, and Python source are redacted regardless of these switches. Raw audio is never logged.</p>
        </aside>

        <section className="trace-tape" aria-label="Diagnostic event timeline">
          <header>
            <div><span>Causal tape</span><strong>{visibleEvents.length} visible events</strong></div>
            <small>{capacity ? `${Math.round((retained / capacity) * 100)}% buffer used` : "Connecting"}</small>
          </header>
          <div className="trace-axis" aria-hidden="true"><i className="axis-l0" /><i className="axis-l1" /><i className="axis-l2" /></div>
          <div className="trace-events">
            {visibleEvents.length ? visibleEvents.map((event) => (
              <button
                type="button"
                className={selected?.id === event.id ? "trace-event is-selected" : "trace-event"}
                data-layer={event.layer}
                data-level={event.level}
                key={event.id}
                onClick={() => setSelectedId(event.id)}
              >
                <span className="trace-sequence">{String(event.id).padStart(4, "0")}</span>
                <span className="trace-node"><i /></span>
                <span className="trace-copy"><small>{formatClock(event.timestamp)} · {event.event}</small><strong>{event.message}</strong><b>{shortId(event.operationId ?? event.utteranceId)}</b></span>
                {formatDuration(event.durationMs) ? <span className="trace-duration">{formatDuration(event.durationMs)}</span> : null}
              </button>
            )) : <div className="recorder-empty"><strong>No signal matches this view.</strong><span>Change the layer, severity, trace, or search filter.</span></div>}
          </div>
        </section>

        <aside className="event-inspector">
          <header><span>Selected frame</span><strong>{selected ? selected.event : "No event selected"}</strong></header>
          {selected ? <>
            <dl>
              <div><dt>Layer</dt><dd data-layer={selected.layer}>{selected.layer}</dd></div>
              <div><dt>Severity</dt><dd>{selected.level}</dd></div>
              <div><dt>Utterance</dt><dd title={selected.utteranceId}>{shortId(selected.utteranceId)}</dd></div>
              <div><dt>Operation</dt><dd title={selected.operationId}>{shortId(selected.operationId)}</dd></div>
              <div><dt>Run</dt><dd title={selected.runId}>{shortId(selected.runId)}</dd></div>
              <div><dt>Duration</dt><dd>{formatDuration(selected.durationMs) ?? "—"}</dd></div>
            </dl>
            <div className="payload-header"><span>Sanitized payload</span><button type="button" onClick={() => void navigator.clipboard.writeText(JSON.stringify(selected, null, 2))}>Copy JSON</button></div>
            <pre>{JSON.stringify(selected.data ?? {}, null, 2)}</pre>
          </> : <div className="recorder-empty">Select an event on the causal tape to inspect its correlations and sanitized payload.</div>}
          <div className="inspector-actions">
            <a href={`${backendUrl}/api/diagnostics/export${trace ? `?trace=${encodeURIComponent(trace)}` : ""}`} download>Export {trace ? "this trace" : "all traces"}</a>
            <button type="button" onClick={() => void clearEvents()}>Clear memory</button>
          </div>
        </aside>
      </div>
    </main>
  );
}
