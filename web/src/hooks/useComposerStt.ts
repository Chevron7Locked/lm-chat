/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useComposerStt — Composer's Web Speech API integration: transcript
 * injection into the composer text, the ≤60-char aria-live announcement
 * snippet, and the mic start/stop toggle.
 *
 * Extracted from Composer.tsx — behavior-preserving; every callback body is
 * verbatim from Composer.tsx, only wrapped in this hook. `sttAnnouncement`
 * now lives here (its only consumer is the aria-live region JSX in
 * Composer.tsx, which reads the returned value directly — same shape as
 * before).
 */
import { useState } from "react";
import type { Dispatch, RefObject, SetStateAction } from "react";
import { useSTT } from "@/hooks/useSTT";
import type { STTCapability, STTState } from "@/hooks/useSTT";
import type { PushOptions } from "@/stores/toastStore";

export interface UseComposerSttResult {
  sttCapability: STTCapability;
  sttState: STTState;
  sttAnnouncement: string;
  handleSttToggle: () => void;
}

export function useComposerStt(
  setText: Dispatch<SetStateAction<string>>,
  textareaRef: RefObject<HTMLTextAreaElement | null>,
  push: (opts: PushOptions) => string,
): UseComposerSttResult {
  // Last transcript snippet for aria-live announcement (M-006).
  const [sttAnnouncement, setSttAnnouncement] = useState("");

  // STT: transcript is appended to the composer text (not replaced).
  const {
    capability: sttCapability,
    state: sttState,
    start: sttStart,
    stop: sttStop,
  } = useSTT(
    (transcript) => {
      setText((prev) => (prev.trim() ? `${prev} ${transcript}` : transcript));
      // Announce the injected transcript (truncated to 60 chars) to screen readers.
      setSttAnnouncement(
        transcript.length > 60 ? `${transcript.slice(0, 60)}…` : transcript,
      );
      textareaRef.current?.focus();
    },
    (errMsg) => {
      push({ variant: "warning", message: errMsg });
    },
  );

  function handleSttToggle(): void {
    if (sttState.listening) {
      sttStop();
    } else {
      sttStart();
    }
  }

  return { sttCapability, sttState, sttAnnouncement, handleSttToggle };
}
