/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useSTT — browser Speech-to-Text via Web Speech API.
 *
 * Browser support:
 *   - Chrome / Edge: SpeechRecognition via webkit prefix (webkitSpeechRecognition).
 *   - Safari: webkitSpeechRecognition.
 *   - Firefox: SpeechRecognition is NOT available (returns available: false).
 *
 * State machine:
 *   idle ──[start()]──► listening
 *   listening ──[result]──► idle (transcript injected)
 *   listening ──[silence > 2s (onend)]──► idle
 *   listening ──[stop()]──► idle
 *   listening ──[error]──► idle (error surfaced via onError callback)
 *
 * No backend involvement — STT is entirely browser-side.
 * No audio is persisted or transmitted beyond the browser.
 */
import { useCallback, useRef, useState } from "react";

// ─── Types ──────────────────────────────────────────────────────────────────

type STTEngine = "webkit" | "standard";

export interface STTCapability {
  available: boolean;
  engine: STTEngine | null;
}

export interface STTState {
  listening: boolean;
  error: string | null;
}

export interface UseSTTReturn {
  capability: STTCapability;
  state: STTState;
  start: () => void;
  stop: () => void;
}

// ─── Capability detection ────────────────────────────────────────────────────

/**
 * Detect Web Speech API support.
 *
 * Returns `{ available: true, engine: "webkit" }` for Chrome/Edge/Safari,
 * `{ available: false, engine: null }` for Firefox and environments where
 * the global is absent.
 */
export function detectSTT(): STTCapability {
  if (typeof window === "undefined") {
    return { available: false, engine: null };
  }
  const w = window as unknown as Record<string, unknown>;
  // Chrome/Edge/Safari expose webkitSpeechRecognition.
  if ("webkitSpeechRecognition" in window && w.webkitSpeechRecognition) {
    return { available: true, engine: "webkit" };
  }
  // Spec-standard SpeechRecognition (future browsers / some Chromium builds).
  if ("SpeechRecognition" in window && w.SpeechRecognition) {
    return { available: true, engine: "standard" };
  }
  return { available: false, engine: null };
}

// ─── SpeechRecognition interface ─────────────────────────────────────────────

/** Minimal interface for the Web Speech API recognition object. */
interface SpeechRecognitionInstance {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult: ((ev: SpeechRecognitionEvent) => void) | null;
  onend: (() => void) | null;
  onerror: ((ev: SpeechRecognitionErrorEvent) => void) | null;
  start: () => void;
  stop: () => void;
}

// ─── SpeechRecognition factory ───────────────────────────────────────────────

function createRecognition(): SpeechRecognitionInstance | null {
  const cap = detectSTT();
  if (!cap.available) return null;
  const w = window as unknown as Record<string, unknown>;
  type RecCtor = new () => SpeechRecognitionInstance;
  if (cap.engine === "webkit") {
    const WK = w.webkitSpeechRecognition as RecCtor;
    return new WK();
  }
  const SR = w.SpeechRecognition as RecCtor;
  return new SR();
}

// ─── Hook ────────────────────────────────────────────────────────────────────

/**
 * useSTT — wraps the Web Speech API for push-to-talk recording.
 *
 * @param onTranscript - Called with the final transcript string when
 *   recognition ends. The caller is responsible for injecting it into the
 *   composer (append, don't replace, so the user's existing text is preserved).
 * @param onError - Optional callback for recognition errors.
 */
export function useSTT(
  onTranscript: (text: string) => void,
  onError?: (msg: string) => void,
): UseSTTReturn {
  const capability = detectSTT();
  const [state, setState] = useState<STTState>({
    listening: false,
    error: null,
  });
  const recognitionRef = useRef<SpeechRecognitionInstance | null>(null);

  const start = useCallback((): void => {
    if (!capability.available) {
      onError?.("Speech-to-text not supported in this browser");
      return;
    }
    if (state.listening) return;

    const rec = createRecognition();
    if (rec === null) return;

    rec.lang = navigator.language || "en-US";
    rec.continuous = false;
    rec.interimResults = false;

    rec.onresult = (ev: SpeechRecognitionEvent) => {
      const transcript = Array.from(ev.results)
        .map((r) => r[0]?.transcript ?? "")
        .join(" ")
        .trim();
      if (transcript) {
        onTranscript(transcript);
      }
    };

    rec.onend = () => {
      setState({ listening: false, error: null });
      recognitionRef.current = null;
    };

    rec.onerror = (ev: SpeechRecognitionErrorEvent) => {
      const msg =
        ev.error === "not-allowed"
          ? "Microphone access denied"
          : ev.error === "no-speech"
            ? "No speech detected"
            : `Speech recognition error: ${ev.error}`;
      setState({ listening: false, error: msg });
      onError?.(msg);
      recognitionRef.current = null;
    };

    recognitionRef.current = rec;
    rec.start();
    setState({ listening: true, error: null });
  }, [capability.available, state.listening, onTranscript, onError]);

  const stop = useCallback((): void => {
    recognitionRef.current?.stop();
    recognitionRef.current = null;
    setState({ listening: false, error: null });
  }, []);

  return { capability, state, start, stop };
}
