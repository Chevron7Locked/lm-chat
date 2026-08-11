/* SPDX-License-Identifier: Apache-2.0 */
/**
 * usePresence — manages the <html data-presence="..."> 5-state machine.
 *
 * States:
 *   idle       — no active chat or LM Studio disconnected
 *   listening  — composer focused or recently typed (default active state)
 *   thinking   — stream active, reasoning phase (reasoning_content growing)
 *   speaking   — stream active, content phase (content growing)
 *   settled    — after a message completes (600ms, then reverts to listening)
 *
 * body::before in globals.css reacts to [data-presence] to shift bloom
 * intensity and animation pace — the room breathes with the conversation.
 *
 * Usage: call usePresence({ streaming, hasReasoningContent }) from the
 * Chat page. Composer calls notifyComposerFocused() to signal listening.
 */
import { useEffect, useRef, useCallback, useState } from "react";

type PresenceState =
  | "idle"
  | "listening"
  | "thinking"
  | "speaking"
  | "settled";

interface UsePresenceOptions {
  /** True while an SSE stream is active. */
  streaming: boolean;
  /** True when the active streaming message has growing reasoning_content. */
  hasReasoningContent: boolean;
  /** True when the chat is active (has messages or streaming). */
  chatActive: boolean;
}

function setPresence(state: PresenceState): void {
  document.documentElement.setAttribute("data-presence", state);
}

function getPresence(): PresenceState {
  return (
    (document.documentElement.getAttribute(
      "data-presence",
    ) as PresenceState | null) ?? "idle"
  );
}

export function usePresence({
  streaming,
  hasReasoningContent,
  chatActive,
}: UsePresenceOptions): {
  notifyComposerFocused: () => void;
  notifyComposerBlurred: () => void;
  notifyTyping: () => void;
} {
  // Early bail on reduced-motion — CSS already guards animations
  // via @media (prefers-reduced-motion: reduce). Avoid DOM attribute churn.
  // Subscribe to mid-session changes so toggling OS preference takes effect.
  const [reduceMotion, setReduceMotion] = useState<boolean>(
    () =>
      typeof window !== "undefined" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches,
  );

  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    const onChange = (): void => {
      setReduceMotion(mq.matches);
    };
    mq.addEventListener("change", onChange);
    return () => {
      mq.removeEventListener("change", onChange);
    };
  }, []);

  const settledTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const prevStreaming = useRef(false);

  // Transition the presence state based on streaming/reasoning signals.
  useEffect(() => {
    if (reduceMotion) return;
    const wasStreaming = prevStreaming.current;
    prevStreaming.current = streaming;

    if (settledTimerRef.current !== null) {
      clearTimeout(settledTimerRef.current);
      settledTimerRef.current = null;
    }

    if (streaming) {
      // Determine phase: reasoning or content.
      if (hasReasoningContent) {
        setPresence("thinking");
      } else {
        setPresence("speaking");
      }
    } else if (wasStreaming) {
      // Stream just ended — brief "settled" shimmer, then back to listening.
      setPresence("settled");
      settledTimerRef.current = setTimeout(() => {
        const current = getPresence();
        if (current === "settled") {
          setPresence(chatActive ? "listening" : "idle");
        }
        settledTimerRef.current = null;
      }, 800);
    } else if (!chatActive) {
      setPresence("idle");
    }
    // If chatActive and not streaming and was not streaming: leave current state
    // (could be listening from composer focus — don't clobber it).
  }, [streaming, hasReasoningContent, chatActive]);

  // Cleanup on unmount.
  useEffect(() => {
    return () => {
      if (settledTimerRef.current !== null)
        clearTimeout(settledTimerRef.current);
      setPresence("idle");
    };
  }, []);

  const notifyComposerFocused = useCallback((): void => {
    const current = getPresence();
    if (current === "idle" || current === "listening") {
      setPresence("listening");
    }
  }, []);

  const notifyComposerBlurred = useCallback((): void => {
    const current = getPresence();
    if (current === "listening" && !chatActive) {
      setPresence("idle");
    }
  }, [chatActive]);

  const notifyTyping = useCallback((): void => {
    setPresence("listening");
  }, []);

  return { notifyComposerFocused, notifyComposerBlurred, notifyTyping };
}
