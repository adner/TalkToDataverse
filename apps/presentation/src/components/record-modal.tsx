"use client";

import { useEffect, useRef } from "react";

import type { OpenedRecord } from "@/lib/chart";
import { tableLabel } from "@/lib/hold-state";

/** Open the record in its own window: the fallback when the app will not load in the frame. */
export function openInWindow(record: OpenedRecord) {
  window.open(record.url, "sot-dataverse-record", "popup,width=1200,height=860");
}

/** The record L2 opened, in the model-driven app inside a dismissable dialog. Flights keep running. */
export function RecordModal({
  record,
  thread,
  onClose,
}: {
  record: OpenedRecord;
  thread: string;
  onClose: () => void;
}) {
  const close = useRef<HTMLButtonElement>(null);
  const pointerInFrame = useRef(false);

  useEffect(() => {
    close.current?.focus();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    // The app's form focuses its first field as it loads, which would swallow Esc. Take focus
    // back unless the presenter is pointing into the form to use it.
    const onBlur = () => {
      if (pointerInFrame.current) return;
      window.setTimeout(() => {
        window.focus();
        close.current?.focus();
      }, 0);
    };
    window.addEventListener("keydown", onKey);
    window.addEventListener("blur", onBlur);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("blur", onBlur);
    };
  }, [onClose]);

  return (
    <div className="record-backdrop" onClick={onClose} role="presentation">
      <section
        aria-label={`${record.name} in ${record.app}`}
        aria-modal="true"
        className="record-modal"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
      >
        <header className="record-header">
          <span className="flight-number">T{thread}</span>
          <div>
            <strong>{record.name}</strong>
            <small>
              {tableLabel(record.table)} · {record.app}
            </small>
          </div>
          <button className="record-window" onClick={() => openInWindow(record)} type="button">
            Open in window ↗
          </button>
          <button aria-label="Close" className="record-close" onClick={onClose} ref={close} type="button">
            ✕
          </button>
        </header>
        <iframe
          className="record-frame"
          onPointerEnter={() => {
            pointerInFrame.current = true;
          }}
          onPointerLeave={() => {
            pointerInFrame.current = false;
          }}
          src={record.url}
          title={`${record.name} in ${record.app}`}
        />
      </section>
    </div>
  );
}
