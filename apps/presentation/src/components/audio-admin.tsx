"use client";

import { FormEvent, useCallback, useEffect, useState } from "react";
import Link from "next/link";

import { readTypedInput, writeTypedInput } from "@/lib/stage-settings";

type AudioDevice = {
  id: number;
  name: string;
  sampleRate: number;
  channels: number;
  isDefault: boolean;
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

type ResetPlan = { dryRun: boolean; deleted: number; recreated: number; reverted: number; skipped: number };

function describePlan(plan: ResetPlan) {
  const changes = plan.deleted + plan.recreated + plan.reverted;
  if (!changes) return "Contoso Bikes is at its baseline.";
  const parts = [
    plan.deleted ? `delete ${plan.deleted} new` : "",
    plan.recreated ? `recreate ${plan.recreated} deleted` : "",
    plan.reverted ? `revert ${plan.reverted} changed` : "",
  ].filter(Boolean);
  return `Reset ${plan.dryRun ? "would" : "did"} ${parts.join(", ")} ${changes === 1 ? "record" : "records"}.`;
}

/** Puts Contoso Bikes back to the committed baseline (`sot-reset restore`). */
function DemoReset({ backendUrl }: { backendUrl: string }) {
  const [busy, setBusy] = useState<"check" | "reset" | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(dryRun: boolean) {
    setBusy(dryRun ? "check" : "reset");
    setError(null);
    setMessage(null);
    try {
      const response = await fetch(`${backendUrl}/api/admin/reset?dry_run=${dryRun}`, { method: "POST" });
      const value = (await response.json()) as ResetPlan & { detail?: string };
      if (!response.ok) throw new Error(value.detail ?? "The reset failed.");
      setMessage(describePlan(value));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "The reset failed.");
    } finally {
      setBusy(null);
    }
  }

  return (
    <section className="audio-settings-form demo-reset">
      <header><span>Demo data</span><strong>Contoso Bikes baseline</strong></header>
      <p className="settings-note">Writes in the demo change the story. Reset deletes records created since the baseline, recreates deleted ones and reverts changed columns. Run it before every rehearsal.</p>
      {error ? <div className="admin-message is-error">{error}</div> : null}
      {message ? <div className="admin-message is-success">{message}</div> : null}
      <div className="demo-reset-actions">
        <button className="rearm-button" type="button" onClick={() => void run(true)} disabled={busy !== null}>
          <span>{busy === "check" ? "Checking" : "Check changes"}</span><i aria-hidden="true">?</i>
        </button>
        <button className="rearm-button" type="button" onClick={() => void run(false)} disabled={busy !== null}>
          <span>{busy === "reset" ? "Resetting" : "Reset demo"}</span><i aria-hidden="true">↺</i>
        </button>
      </div>
    </section>
  );
}

/** Stage options kept in this browser: the typed-input box for testing without a microphone. */
function StageOptions() {
  const [typedInput, setTypedInput] = useState(false);

  useEffect(() => {
    const timeout = window.setTimeout(() => setTypedInput(readTypedInput()), 0);
    return () => window.clearTimeout(timeout);
  }, []);

  return (
    <section className="audio-settings-form stage-options">
      <header><span>Stage</span><strong>Input options</strong></header>
      <label className="toggle-row">
        <input
          checked={typedInput}
          onChange={(event) => {
            writeTypedInput(event.target.checked);
            setTypedInput(event.target.checked);
          }}
          type="checkbox"
        />
        <span>Typed input on the stage</span>
      </label>
      <p className="settings-note">Adds a text box next to Hold to talk, for testing without a microphone. Off by default; saved in this browser only, and an open stage picks it up straight away.</p>
    </section>
  );
}

function formatRate(value: number | null | undefined) {
  return value ? `${(value / 1_000).toFixed(value % 1_000 ? 1 : 0)} kHz` : "—";
}

export function AudioAdmin() {
  const backendUrl = process.env.NEXT_PUBLIC_SOT_BACKEND_URL ?? "http://127.0.0.1:8000";
  const [devices, setDevices] = useState<AudioDevice[]>([]);
  const [settings, setSettings] = useState<AudioSettings | null>(null);
  const [deviceId, setDeviceId] = useState<number | "">("");
  const [prerollMs, setPrerollMs] = useState(600);
  const [tailFlushMs, setTailFlushMs] = useState(150);
  const [minimumClipMs, setMinimumClipMs] = useState(150);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const refresh = useCallback(async (hydrateForm = false) => {
    const [deviceResponse, settingsResponse] = await Promise.all([
      fetch(`${backendUrl}/api/audio/devices`),
      fetch(`${backendUrl}/api/audio/settings`),
    ]);
    if (!deviceResponse.ok || !settingsResponse.ok) throw new Error("Audio control plane is unavailable.");
    const deviceValue = (await deviceResponse.json()) as { devices?: AudioDevice[] };
    const settingsValue = (await settingsResponse.json()) as AudioSettings;
    setDevices(deviceValue.devices ?? []);
    setSettings(settingsValue);
    if (hydrateForm) {
      setDeviceId(settingsValue.settings.deviceId ?? "");
      setPrerollMs(settingsValue.settings.prerollMs);
      setTailFlushMs(settingsValue.settings.tailFlushMs);
      setMinimumClipMs(settingsValue.settings.minimumClipMs);
    }
  }, [backendUrl]);

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      void refresh(true).catch((caught: unknown) =>
        setError(caught instanceof Error ? caught.message : "Audio settings could not be loaded."),
      );
    }, 0);
    return () => window.clearTimeout(timeout);
  }, [refresh]);

  useEffect(() => {
    const interval = window.setInterval(() => {
      if (!saving) void refresh(false).catch(() => undefined);
    }, 750);
    return () => window.clearInterval(interval);
  }, [refresh, saving]);

  async function save(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setSaving(true);
    setError(null);
    setMessage(null);
    try {
      const response = await fetch(`${backendUrl}/api/audio/settings`, {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          device_id: deviceId === "" ? null : deviceId,
          preroll_ms: prerollMs,
          tail_flush_ms: tailFlushMs,
          minimum_clip_ms: minimumClipMs,
        }),
      });
      const value = (await response.json()) as AudioSettings & { detail?: string };
      if (!response.ok) throw new Error(value.detail ?? "The microphone could not be re-armed.");
      setSettings(value);
      setDeviceId(value.settings.deviceId ?? "");
      setMessage(value.status === "fixture" ? "Settings staged. Start the live profile to arm capture." : "Audio ring buffer re-armed.");
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : "Audio settings could not be saved.");
    } finally {
      setSaving(false);
    }
  }

  const fill = settings?.settings.prerollMs
    ? Math.min(100, Math.round((settings.bufferedPrerollMs / settings.settings.prerollMs) * 100))
    : 0;
  const selectedDevice = devices.find((device) => device.id === Number(deviceId));

  return (
    <main className="audio-console">
      <div className="ambient-grid" aria-hidden="true" />
      <header className="admin-header">
        <Link href="/" className="back-link"><span aria-hidden="true">←</span> Live cognition field</Link>
        <div className="brand-lockup">
          <div className="brand-sigil" aria-hidden="true"><i /><i /><i /></div>
          <div><span>SpeedOfThought</span><strong>Audio control room</strong></div>
        </div>
        <div className="recorder-nav"><Link href="/diagnostics">Flight recorder</Link><span className={settings?.armed ? "admin-live is-armed" : "admin-live"}><i />{settings?.armed ? "Ring buffer armed" : settings?.captureState ?? "Connecting"}</span></div>
      </header>

      <section className="admin-hero">
        <div>
          <span>Push-to-talk acquisition</span>
          <h1>Catch the words<br />before the press.</h1>
        </div>
        <p>The microphone stays open locally and retains only a rolling slice of audio. Holding the talk button marks the utterance; releasing it sends the buffered lead-in, held audio, and release tail to L0.</p>
      </section>

      <div className="admin-grid">
        <section className="buffer-instrument">
          <header>
            <div><span>Live ring buffer</span><strong>{settings?.bufferedPrerollMs ?? 0} ms retained</strong></div>
            <small>{settings?.initialized ? "Sampling continuously" : "Not initialized"}</small>
          </header>
          <div className="buffer-dial" style={{ "--buffer-fill": `${fill * 3.6}deg` } as React.CSSProperties}>
            <div>
              <span>{fill}%</span>
              <strong>ready</strong>
            </div>
          </div>
          <div className="buffer-timeline" aria-label={`${prerollMs} milliseconds before press, ${minimumClipMs} milliseconds minimum hold, and ${tailFlushMs} milliseconds after release`}>
            <div className="timeline-preroll" style={{ flexGrow: Math.max(prerollMs, 1) }}><span>−{prerollMs} ms</span><i /></div>
            <b>press</b>
            <div className="timeline-active"><span>hold</span><i /></div>
            <b>release</b>
            <div className="timeline-tail" style={{ flexGrow: Math.max(tailFlushMs, 1) }}><span>+{tailFlushMs} ms</span><i /></div>
          </div>
          <dl className="audio-facts">
            <div><dt>Capture device</dt><dd>{settings?.device.name ?? selectedDevice?.name ?? "System default"}</dd></div>
            <div><dt>Native rate</dt><dd>{formatRate(settings?.device.sampleRate ?? selectedDevice?.sampleRate)}</dd></div>
            <div><dt>ASR rate</dt><dd>{formatRate(settings?.settings.targetSampleRate)}</dd></div>
            <div><dt>Buffer quantum</dt><dd>{settings?.settings.blockMs ?? 10} ms</dd></div>
            <div><dt>Clip floor</dt><dd>{minimumClipMs} ms held</dd></div>
            <div><dt>Release tail</dt><dd>{tailFlushMs} ms</dd></div>
          </dl>
        </section>

        <form className="audio-settings-form" onSubmit={save}>
          <header><span>Session settings</span><strong>Acquisition path</strong></header>

          <label>
            <span>Input device</span>
            <select value={deviceId} onChange={(event) => setDeviceId(event.target.value === "" ? "" : Number(event.target.value))} disabled={saving}>
              <option value="">System default microphone</option>
              {devices.map((device) => <option key={device.id} value={device.id}>{device.name}{device.isDefault ? " · system default" : ""}</option>)}
            </select>
            <small>{selectedDevice ? `${selectedDevice.channels} channel${selectedDevice.channels === 1 ? "" : "s"} · ${formatRate(selectedDevice.sampleRate)}` : "Uses the current Windows WASAPI default."}</small>
          </label>

          <label>
            <span>Pre-roll memory <output>{prerollMs} ms</output></span>
            <input type="range" min="0" max="2000" step="25" value={prerollMs} onChange={(event) => setPrerollMs(Number(event.target.value))} disabled={saving} />
            <small>Audio retained immediately before you press. 500–800 ms is forgiving without pulling in much previous speech.</small>
          </label>

          <label>
            <span>Minimum clip <output>{minimumClipMs} ms</output></span>
            <input type="range" min="0" max="1000" step="25" value={minimumClipMs} onChange={(event) => setMinimumClipMs(Number(event.target.value))} disabled={saving} />
            <small>Captures released sooner are discarded before terminal intent classification or L2 launch. Pre-roll does not count toward this threshold.</small>
          </label>

          <label>
            <span>Release tail <output>{tailFlushMs} ms</output></span>
            <input type="range" min="0" max="750" step="25" value={tailFlushMs} onChange={(event) => setTailFlushMs(Number(event.target.value))} disabled={saving} />
            <small>Continues capture briefly after release so final consonants are not clipped.</small>
          </label>

          {error ? <div className="admin-message is-error">{error}</div> : null}
          {message ? <div className="admin-message is-success">{message}</div> : null}

          <button className="rearm-button" type="submit" disabled={saving || settings?.captureState === "capturing" || settings?.captureState === "finalizing"}>
            <span>{saving ? "Re-arming capture" : "Save and re-arm"}</span><i aria-hidden="true">↗</i>
          </button>
          <p className="settings-note">Settings are process-local for this demo. Changing the device safely closes and reopens the capture stream while no utterance is active.</p>
        </form>

        <DemoReset backendUrl={backendUrl} />
        <StageOptions />
      </div>
    </main>
  );
}
