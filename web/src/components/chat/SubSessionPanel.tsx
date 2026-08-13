/* SPDX-License-Identifier: Apache-2.0 */
import type { CSSProperties } from "react";
import { X } from "lucide-react";
import { ChatMessage } from "@/components/ChatMessage";
import type { SubSessionSSEState } from "@/hooks/useSubSessionSSE";

// ─── Sub-session panel ───────────────────────────────────────────────────────

interface SubSessionPanelProps {
  subSession: {
    presetLabel: string;
    /** `id` is set only on messages hydrated from a restored transcript
     *  (P3 restore-on-load) — used as a stable render key in place of
     *  array index; a live session's locally-typed turns render exactly
     *  as before. */
    messages: { role: "user" | "assistant"; content: string; id?: number }[];
    finalizing: boolean;
    finalContent: string | null;
  };
  sseState: SubSessionSSEState;
  onFinalize: () => void;
  onInject: () => void;
  onCancel: () => void;
}

// Mirrors ChatMessage.tsx's toolPreStyle (kept module-private there); the
// sub-session tool-call cards reuse the same .lmchat-tool-* classes for
// visual consistency with main-chat tool cards.
const toolCardPreStyle: CSSProperties = {
  margin: 0,
  fontFamily: "var(--font-mono)",
  fontSize: "var(--font-size-sm)",
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
};

const subSessionErrorStyle: CSSProperties = {
  margin: "12px 0",
  padding: "12px 14px",
  background: "color-mix(in oklch, var(--color-danger) 8%, transparent)",
  border: "1px solid color-mix(in oklch, var(--color-danger) 30%, transparent)",
  borderRadius: 8,
  color: "var(--color-text)",
  fontSize: "var(--font-size-sm)",
  lineHeight: 1.45,
};

export function SubSessionPanel({
  subSession,
  sseState,
  onFinalize,
  onInject,
  onCancel,
}: SubSessionPanelProps) {
  const streaming = sseState.status === "streaming";
  const isReasoning =
    streaming && sseState.reasoning_content && !sseState.content;

  // Anti-jump (2026-06-18): the just-completed assistant turn must render
  // in the SAME slot it streamed in — AFTER the tool cards — not jump up
  // into the settled-messages list above the cards. Split a trailing assistant
  // turn out of the head list and re-render it below the cards. (The streaming
  // in-flight message and this settled one are mutually exclusive.)
  const _msgs = subSession.messages;
  const _last = _msgs.length > 0 ? _msgs[_msgs.length - 1] : undefined;
  const trailingAssistant =
    _last?.role === "assistant" ? _last : null;
  const headMessages =
    trailingAssistant !== null ? _msgs.slice(0, -1) : _msgs;

  return (
    <div className="lmchat-subsession-outer">
      {/* Mode eyebrow — monospace ALL-CAPS preset name above the bar.
          Uses the actual presetLabel so /code shows "CODER", /research
          shows "RESEARCH", etc. aria-hidden: purely decorative. */}
      <div className="lmchat-subsession-mode-eyebrow" aria-hidden="true">
        {subSession.presetLabel.toUpperCase()}
      </div>

      {/* Control bar */}
      <div className="lmchat-subsession-bar">
        <span className="lmchat-subsession-label">
          {subSession.presetLabel} mode
        </span>
        <div className="lmchat-subsession-bar__actions">
          {subSession.finalContent !== null ? (
            <button
              type="button"
              onClick={onInject}
              className="lmchat-subsession-finish-btn atelier-cta"
            >
              Add to main chat →
            </button>
          ) : subSession.finalizing || streaming ? (
            <span
              className="lmchat-subsession-status"
              aria-live="polite"
              aria-atomic="true"
            >
              {subSession.finalizing ? "Generating summary…" : "Thinking…"}
            </span>
          ) : (
            <button
              type="button"
              onClick={onFinalize}
              className="lmchat-subsession-finish-btn atelier-cta"
            >
              Summarize → main chat
            </button>
          )}
          <button
            type="button"
            onClick={onCancel}
            className="lmchat-subsession-cancel-btn"
            aria-label="Cancel sub-session"
          >
            <X size={14} aria-hidden />
          </button>
        </div>
      </div>

      {/* Sub-session messages */}
      <div className="lmchat-subsession-messages">
        {subSession.messages.length === 0 && sseState.status === "idle" && (
          <div className="lmchat-subsession-empty">
            <p className="lmchat-subsession-intro">
              You're now in{" "}
              <strong className="lmchat-subsession-intro__strong">
                {subSession.presetLabel} mode
              </strong>
              . This is a clean session — the model only sees this conversation,
              not your main chat history.
            </p>
            <p className="lmchat-subsession-hint">
              When finished, press "Summarize → main chat" to surface the
              outcome in your main conversation.
            </p>
          </div>
        )}
        {headMessages.map((msg, i) => {
          const key = msg.id !== undefined ? `sub-row-${String(msg.id)}` : `sub-${String(i)}`;
          return (
            <ChatMessage
              key={key}
              message={{
                id: key,
                role: msg.role,
                content: msg.content,
                reasoning_content: null,
              }}
              streamingActive={false}
              personaLabel={msg.role === "assistant" ? subSession.presetLabel : undefined}
            />
          );
        })}
        {/* Live tool-call cards. BE was emitting
            sub.tool_call.{start,name,arguments,success} but the
            panel rendered only "Thinking…" — the toolCalls array was
            tracked in sseState but never displayed. Without this the
            user has no visibility into search_web / fetch_url firing
            mid-/research. */}
        {/* Surface BE-emitted sub.error so the user knows WHY a sub-session
            stopped — pre-fix the panel went silent to "Summarize → main chat"
            with no explanation when tool_format_generation_error or
            no_final_content fired. */}
        {sseState.status === "error" && sseState.error !== null && (
          <div
            className="lmchat-subsession-error"
            role="alert"
            style={subSessionErrorStyle}
          >
            <strong style={{ display: "block", marginBottom: "var(--space-glue)" }}>
              Sub-session stopped
            </strong>
            <span>{sseState.error.message}</span>
            {typeof sseState.error.hint === "string" &&
              sseState.error.hint !== "" && (
                <p style={{ marginTop: "var(--space-glue-relaxed)", marginBottom: 0, opacity: 0.85 }}>
                  {sseState.error.hint}
                </p>
              )}
          </div>
        )}
        {sseState.toolCalls.length > 0 && (
          <ul className="lmchat-subsession-toolcalls" aria-label="Tools used">
            {sseState.toolCalls.map((tc) => {
              const friendlyName = tc.name
                .replace(/^(mcp__[^_]+__|firecrawl_|fetch_|search_)/, "")
                .replace(/[_-]/g, " ")
                .replace(/\b\w/g, (c) => c.toUpperCase())
                .trim() || "Used a tool";
              return (
                <li key={tc.id}>
                  <details className="lmchat-tool-card">
                    <summary className="lmchat-tool-summary">
                      <span
                        className="lmchat-tool-name"
                        title={tc.name !== "" ? tc.name : undefined}
                      >
                        {friendlyName}
                      </span>
                      <span
                        className="lmchat-tool-status"
                        style={{
                          color:
                            tc.status === "success"
                              ? "var(--color-success)"
                              : tc.status === "failure"
                                ? "var(--color-danger)"
                                : "var(--color-text-muted)",
                        }}
                      >
                        {tc.status}
                      </span>
                    </summary>
                    <div className="lmchat-tool-card-body">
                      {tc.arguments !== "" && (
                        <pre style={toolCardPreStyle}>{tc.arguments}</pre>
                      )}
                      {tc.result !== undefined && tc.result !== "" && (
                        <pre
                          className="lmchat-tool-result"
                          style={toolCardPreStyle}
                        >
                          {tc.result}
                        </pre>
                      )}
                    </div>
                  </details>
                </li>
              );
            })}
          </ul>
        )}
        {/* Streaming in-flight content.
            Reasoning phase: display typography italic at reduced contrast.
            Answer phase: full contrast sans. */}
        {streaming && (sseState.content || sseState.reasoning_content) && (
          <div
            className={
              isReasoning
                ? "lmchat-subsession-reasoning-phase"
                : "lmchat-subsession-answer-phase"
            }
          >
            <ChatMessage
              key="sub-streaming"
              message={{
                id: "sub-streaming",
                role: "assistant",
                content: sseState.content,
                reasoning_content: sseState.reasoning_content,
                streaming: true,
              }}
              streamingActive={true}
            />
          </div>
        )}
        {/* Just-completed assistant turn — rendered here (after the cards),
            the same slot it streamed in, so it doesn't jump above the cards on
            completion (2026-06-18 anti-jump fix). */}
        {!streaming && trailingAssistant !== null && (
          <ChatMessage
            key="sub-trailing"
            message={{
              id: "sub-trailing",
              role: "assistant",
              content: trailingAssistant.content,
              reasoning_content: null,
            }}
            streamingActive={false}
            personaLabel={subSession.presetLabel}
          />
        )}
        {/* Finalized summary preview */}
        {subSession.finalContent !== null && (
          <div className="lmchat-subsession-summary">
            <p className="lmchat-subsession-summary__eyebrow">Summary ready</p>
            <ChatMessage
              key="sub-final"
              message={{
                id: "sub-final",
                role: "assistant",
                content: subSession.finalContent,
                reasoning_content: null,
              }}
              streamingActive={false}
              personaLabel={subSession.presetLabel}
            />
          </div>
        )}
      </div>
    </div>
  );
}

// Sub-session styles (outer/bar/label/messages/summary) migrated to
// .lmchat-subsession-* classes in web/src/styles/chat.css.
