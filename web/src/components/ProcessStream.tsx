/* SPDX-License-Identifier: Apache-2.0 */
import "@/styles/process-stream.css";
/**
 * ProcessStream — ONE calm "process" surface per assistant turn.
 *
 * Replaces the former indicator→block→cards chain (ThinkingIndicator +
 * ThinkingBlock + separate ToolCallCard cards), which stacked four
 * independently-animating, mounting/unmounting pieces that flashed and
 * shifted layout. This is a single transparent surface — no card chrome,
 * no vellum band, no breathing animation.
 *
 * State machine (driven entirely by props — no timers, no verb rotation):
 *
 *   (a) pre-token thinking   streaming, no reasoning yet, no answer
 *         → a quiet "Thinking" / loadPhase line + ONE steady cursor beneath.
 *   (b) reasoning streaming  streaming, reasoning arriving, no answer
 *         → reasoning streams LIVE in the transparent surface + cursor beneath.
 *   (c) answer streaming     streaming, answer content present
 *         → reasoning AUTO-COLLAPSES to a single "Reasoning" line (expandable);
 *           inline tool lines stay; the answer renders in the SAME column with
 *           the cursor at the END of the answer (owned by ChatMessage, not here).
 *   (d) tool call            any phase with toolCalls
 *         → quiet inline monospace "› name … status" lines inside this surface,
 *           expandable to args/result. NOT separate cards.
 *   (e) done                 not streaming
 *         → reasoning collapsed to the "Reasoning" line (click to re-expand);
 *           tool lines remain; no cursor.
 *
 * Expand state for the collapsed reasoning line is persisted per message in
 * sessionStorage (same contract as the former ThinkingBlock) so it survives
 * re-renders within a session and resets on reload.
 *
 * Motion budget: one calm cursor (the shared .lmchat-stream-caret, a gentle
 * opacity pulse) + the natural text streaming. No rotating verbs, no bouncing
 * dots, no pulsing dot, no 12s vellum-depth breathing. prefers-reduced-motion
 * is honoured by the shared caret rule and the CSS here.
 *
 * Scroll behaviour (mirrors the main chat window in Chat.tsx):
 *   - While reasoning streams the reasoning text container auto-sticks to the
 *     bottom so new tokens remain in view.
 *   - If the user scrolls UP inside the box, auto-scroll stops and lets them
 *     read freely.  Scrolling back within STICK_THRESHOLD_PX of the bottom
 *     re-arms it.  The same pattern is used for both the live (pre-answer) text
 *     view and the expanded collapsed-reasoning body.
 *   - Scroll uses "instant" behaviour during active streaming and "smooth" at
 *     rest, matching the main window.  prefers-reduced-motion respected by
 *     keeping the instant path (no smooth animation) when the media query fires.
 */
import { useState, useEffect, useRef, useCallback } from "react";
import type { ToolCall, LoadPhase } from "@/hooks/useSSE";
import { formatToolName } from "@/lib/formatToolName";

// ─── Reasoning expand-state persistence (ported from ThinkingBlock) ──────────

const SS_PREFIX = "lmchat:thinking";

function ssKey(msgId: number | string): string {
  return `${SS_PREFIX}:${String(msgId)}`;
}

function readExpanded(msgId: number | string): boolean {
  try {
    return sessionStorage.getItem(ssKey(msgId)) === "true";
  } catch {
    return false;
  }
}

function writeExpanded(msgId: number | string, val: boolean): void {
  try {
    if (val) {
      sessionStorage.setItem(ssKey(msgId), "true");
    } else {
      sessionStorage.removeItem(ssKey(msgId));
    }
  } catch {
    // Ignore (private mode / disabled storage).
  }
}

// ─── loadPhase copy (ported from the former ThinkingIndicator) ───────────────

/** Human-readable label for the model-load / prompt-processing phase. */
function loadPhaseLabel(phase: LoadPhase): string {
  switch (phase.phase) {
    case "model_load": {
      if (phase.progress !== null) {
        const pct = Math.round(phase.progress * 100);
        return `Loading model ${String(pct)}%`;
      }
      return "Loading model…";
    }
    case "prompt_processing": {
      if (phase.progress !== null) {
        const pct = Math.round(phase.progress * 100);
        return `Processing prompt ${String(pct)}%`;
      }
      return "Processing…";
    }
    case "generating":
      return "Generating";
    default:
      return "Thinking";
  }
}

// ─── Props ───────────────────────────────────────────────────────────────────

interface ProcessStreamProps {
  /** Message id — used for the reasoning expand-state sessionStorage key. */
  messageId: number | string;
  /** Combined reasoning text (join of reasoning deltas). May be "" or absent. */
  reasoning?: string | null | undefined;
  /** Tool calls for this turn, rendered as quiet inline lines. */
  toolCalls?: ToolCall[] | undefined;
  /** True while this turn is the in-flight streaming assistant row. */
  streaming?: boolean;
  /** True once answer content has started arriving (collapses reasoning). */
  hasAnswer?: boolean;
  /**
   * Model-load / prompt-processing phase line (streaming only). Shown as a
   * single quiet line in the pre-token window so there is no separate widget.
   */
  loadPhase?: LoadPhase | null | undefined;
}

// ─── Component ────────────────────────────────────────────────────────────────

// ─── Stick-to-bottom threshold (mirrors Chat.tsx STICK_THRESHOLD_PX) ─────────

/** px from the bottom within which the scroll position is treated as "at the
 *  bottom" and auto-stick is kept armed. Generous enough to survive a final
 *  delta's height growth without unsticking. */
const STICK_THRESHOLD_PX = 80;

// ─── useStickToBottom — reusable scroll-tracking hook ────────────────────────

/**
 * Returns [scrollRef, autoStickRef, handleScroll] for a scrollable container.
 *
 * - scrollRef: attach to the scrollable element's ref.
 * - autoStickRef: true while the user is near the bottom; flip to false on
 *   scroll-up.  The caller is responsible for scrolling (via a useEffect) and
 *   must check this ref before doing so.
 * - handleScroll: wire to the element's onScroll.
 *
 * This mirrors the pattern used by the main chat window in Chat.tsx.
 */
function useStickToBottom() {
  const scrollRef = useRef<HTMLPreElement | null>(null);
  const autoStickRef = useRef(true);
  const lastRafRef = useRef<number | null>(null);

  const handleScroll = useCallback(() => {
    const el = scrollRef.current;
    if (!el) return;
    const dist = el.scrollHeight - el.scrollTop - el.clientHeight;
    autoStickRef.current = dist <= STICK_THRESHOLD_PX;
  }, []);

  return { scrollRef, autoStickRef, lastRafRef, handleScroll };
}

export function ProcessStream({
  messageId,
  reasoning,
  toolCalls,
  streaming = false,
  hasAnswer = false,
  loadPhase,
}: ProcessStreamProps) {
  const reasoningText = reasoning ?? "";
  const hasReasoning = reasoningText !== "";
  const hasTools = toolCalls !== undefined && toolCalls.length > 0;

  // Live while thinking (streaming + no answer yet): reasoning is shown OPEN
  // and uncollapsible. Once the answer starts (hasAnswer) or the turn settles
  // (!streaming), reasoning collapses to one quiet line whose expand state is
  // user-controlled + persisted.
  const live = streaming && !hasAnswer;

  const [expanded, setExpanded] = useState(() => readExpanded(messageId));
  useEffect(() => {
    writeExpanded(messageId, expanded);
  }, [messageId, expanded]);

  // ── Stick-to-bottom for the LIVE reasoning text ──────────────────────────
  const {
    scrollRef: liveScrollRef,
    autoStickRef: liveStickRef,
    lastRafRef: liveRafRef,
    handleScroll: handleLiveScroll,
  } = useStickToBottom();

  useEffect(() => {
    if (!live || !hasReasoning) return;
    if (liveRafRef.current !== null) cancelAnimationFrame(liveRafRef.current);
    liveRafRef.current = requestAnimationFrame(() => {
      liveRafRef.current = null;
      const el = liveScrollRef.current;
      if (!el || !liveStickRef.current) return;
      el.scrollTop = el.scrollHeight;
    });
    return () => {
      if (liveRafRef.current !== null) {
        cancelAnimationFrame(liveRafRef.current);
        liveRafRef.current = null;
      }
    };
  }, [reasoningText, live, hasReasoning, liveScrollRef, liveStickRef, liveRafRef]);

  // Re-arm live stick when the live/pre-answer phase resets (new streaming turn).
  useEffect(() => {
    if (live) liveStickRef.current = true;
  }, [live, liveStickRef]);

  // ── Stick-to-bottom for the EXPANDED collapsed-reasoning body ─────────────
  const {
    scrollRef: bodyScrollRef,
    autoStickRef: bodyStickRef,
    lastRafRef: bodyRafRef,
    handleScroll: handleBodyScroll,
  } = useStickToBottom();

  useEffect(() => {
    if (!expanded || !hasReasoning) return;
    if (bodyRafRef.current !== null) cancelAnimationFrame(bodyRafRef.current);
    bodyRafRef.current = requestAnimationFrame(() => {
      bodyRafRef.current = null;
      const el = bodyScrollRef.current;
      if (!el || !bodyStickRef.current) return;
      el.scrollTop = el.scrollHeight;
    });
    return () => {
      if (bodyRafRef.current !== null) {
        cancelAnimationFrame(bodyRafRef.current);
        bodyRafRef.current = null;
      }
    };
  }, [reasoningText, expanded, hasReasoning, bodyScrollRef, bodyStickRef, bodyRafRef]);

  // Re-arm body stick when the panel expands (user opens it).
  useEffect(() => {
    if (expanded) bodyStickRef.current = true;
  }, [expanded, bodyStickRef]);

  // Nothing to show: no reasoning, no tools, and not in the pre-token window.
  // (The pre-token window still shows the quiet line + cursor so the user knows
  // the turn is alive before any token lands.)
  const showPreToken = live && !hasReasoning && !hasTools;
  if (!hasReasoning && !hasTools && !showPreToken) return null;

  const phaseLine =
    loadPhase !== null && loadPhase !== undefined
      ? loadPhaseLabel(loadPhase)
      : "Thinking";

  return (
    <div
      className="lmchat-process-stream"
      data-testid="process-stream"
      data-live={live ? "true" : "false"}
    >
      {/* Reasoning — LIVE (open, uncollapsible) while thinking; collapsible
          "Reasoning" line once the answer starts or the turn settles. */}
      {hasReasoning &&
        (live ? (
          /* Live (pre-answer) reasoning: open, uncollapsible, in thinkbox. */
          <div className="lmchat-process-reasoning lmchat-process-reasoning--live">
            <div className="lmchat-process-thinkbox">
              <pre
                ref={liveScrollRef}
                className="lmchat-process-reasoning__text"
                onScroll={handleLiveScroll}
                data-testid="process-reasoning-live"
              >
                {reasoningText}
              </pre>
            </div>
          </div>
        ) : (
          /* Collapsed (post-answer / settled) reasoning: toggle + expand body. */
          <div className="lmchat-process-reasoning">
            <button
              type="button"
              aria-expanded={expanded}
              onClick={() => {
                setExpanded((v) => !v);
              }}
              className="lmchat-process-reasoning__toggle"
            >
              <span
                className={`lmchat-process-reasoning__chevron${expanded ? " lmchat-process-reasoning__chevron--expanded" : ""}`}
                aria-hidden="true"
              >
                ›
              </span>
              <span className="lmchat-process-reasoning__label">Reasoning</span>
            </button>
            <div
              className={`lmchat-process-reasoning__body${expanded ? " lmchat-process-reasoning__body--expanded" : ""}`}
            >
              <div className="lmchat-process-reasoning__body-inner">
                <div className="lmchat-process-thinkbox">
                  <pre
                    ref={bodyScrollRef}
                    className="lmchat-process-reasoning__text"
                    onScroll={handleBodyScroll}
                  >
                    {reasoningText}
                  </pre>
                </div>
              </div>
            </div>
          </div>
        ))}

      {/* Tool calls — quiet inline lines (NOT cards). Each is an expandable
          <details> showing args/result, but visually a single monospace line. */}
      {hasTools && (
        <div
          className="lmchat-process-tools"
          role="group"
          aria-label="Tools used"
        >
          {toolCalls.map((tc) => (
            <ToolLine key={tc.id} toolCall={tc} />
          ))}
        </div>
      )}

      {/* Pre-token window — quiet phase line + ONE steady cursor beneath the
          reasoning. The cursor sits here (beneath the process surface) ONLY
          while there is no answer yet; once the answer streams, ChatMessage
          owns the cursor at the end of the answer (no double cursor). */}
      {live && (
        <div className="lmchat-process-waiting">
          {showPreToken && (
            <span
              className="lmchat-process-phase"
              role="status"
              aria-live="polite"
              data-testid="thinking-indicator"
            >
              {phaseLine}
            </span>
          )}
          <span
            aria-hidden="true"
            className="lmchat-stream-caret"
            data-testid="process-stream-caret"
          />
        </div>
      )}
    </div>
  );
}

// ─── Inline tool line ─────────────────────────────────────────────────────────

function ToolLine({ toolCall }: { toolCall: ToolCall }) {
  const friendlyName = formatToolName(toolCall.name);
  const hasBody =
    toolCall.arguments !== "" ||
    (toolCall.result != null && toolCall.result !== "");

  return (
    <details className="lmchat-process-tool" data-status={toolCall.status}>
      <summary className="lmchat-process-tool__summary">
        <span className="lmchat-process-tool__chevron" aria-hidden="true">
          ›
        </span>
        <span
          className="lmchat-process-tool__name"
          title={toolCall.name !== "" ? toolCall.name : undefined}
        >
          {friendlyName}
        </span>
        <span
          className={`lmchat-process-tool__status lmchat-process-tool__status--${toolCall.status}`}
        >
          {toolCall.status}
        </span>
      </summary>
      {hasBody && (
        <div className="lmchat-process-tool__body">
          {toolCall.arguments !== "" && (
            <pre className="lmchat-process-tool__pre">{toolCall.arguments}</pre>
          )}
          {toolCall.result != null && toolCall.result !== "" && (
            <pre className="lmchat-process-tool__pre lmchat-process-tool__pre--result">
              {toolCall.result}
            </pre>
          )}
        </div>
      )}
    </details>
  );
}
