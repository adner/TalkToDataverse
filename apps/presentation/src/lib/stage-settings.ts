// Per-browser stage options set on /admin. The stage is voice-only by default; the typed-input
// box is for testing without a microphone. Stored in localStorage, so it never reaches the backend.

const TYPED_INPUT_KEY = "sot.stage.typedInput";

export function readTypedInput(): boolean {
  try {
    return window.localStorage.getItem(TYPED_INPUT_KEY) === "on";
  } catch {
    return false;
  }
}

export function writeTypedInput(enabled: boolean): void {
  try {
    if (enabled) window.localStorage.setItem(TYPED_INPUT_KEY, "on");
    else window.localStorage.removeItem(TYPED_INPUT_KEY);
  } catch {
    // Storage blocked: the option simply stays off.
  }
}

/** Follow changes made on /admin in another tab. */
export function onTypedInputChange(listener: (enabled: boolean) => void): () => void {
  const handle = (event: StorageEvent) => {
    if (event.key === null || event.key === TYPED_INPUT_KEY) listener(readTypedInput());
  };
  window.addEventListener("storage", handle);
  return () => window.removeEventListener("storage", handle);
}
