/* SPDX-License-Identifier: Apache-2.0 */
import type { CSSProperties } from "react";
import { History as HistoryIcon, X } from "lucide-react";
import { ChatMessage } from "@/components/ChatMessage";
import type { SubSessionSSEState } from "@/hooks/useSubSessionSSE";
import type { SubSessionSummaryDto } from "@/lib/subSession";
import { getPreset } from "@/lib/presets";
import { formatRelativeTime } from "@/lib/relativeTime";

// ─── Sub-session panel ───────────────────────────────────────────────────────

interface SubSessionPanelProps {
  /**
   * `null` when no session is live/reopened and only the history browse
   * view (P4) is showing — Chat.tsx mounts this component whenever EITHER
   * a session is open OR `isHistoryOpen` is true.
   */
  subSession: {
    presetLabel: string;
    /** `id` is set only on messages hydrated from a restored transcript
     *  (P3 restore-on-load) — used as a stable render key in place of
     *  array index; a live session's locally-typed turns render exactly
     *  as before. */
    messages: { role: "user" | "assistant"; content: string; id?: number }[];
    finalizing: boolean;
    finalContent: string | null;
  } | null;
  sseState: SubSessionSSEState;
  onFinalize: () => void;
  onInject: () => void;
  onCancel: () => void;
  /**
   * P4 — per-chat sub-session history (list + reopen). `history` is this
   * chat's past sub-sessions (newest first) once fetched, `null` before
   * the first `onOpenHistory` fetch lands. `isHistoryOpen` toggles the
   * browse view, which replaces the live transcript while showing (a
   * `subSession`, if any, keeps streaming server-side underneath —
   * closing history just returns to viewing it, same as
   * `closeSubSessionPanel` never aborting an in-flight stream).
   */
  history: SubSessionSummaryDto[] | null;
  historyLoading: boolean;
  isHistoryOpen: boolean;
  onOpenHistory: () => void;
  onCloseHistory: () => void;
  /** Reopen a past sub-session (any status) by id. */
  onReopen: (subSessionId: number) => void;
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

const historyBarStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  padding: "var(--space-glue) var(--space-sibling-relaxed)",
  borderBottom: "1px solid var(--color-border)",
};

const historyListStyle: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  overflowY: "auto",
};

const historyEntryStyle: CSSProperties = {
  display: "flex",
  flexDirection: "column",
  alignItems: "flex-start",
  gap: 4,
  width: "100%",
  textAlign: "left",
  background: "none",
  border: "none",
  borderBottom: "1px solid var(--color-border)",
  padding: "var(--space-sibling-relaxed)",
  cursor: "pointer",
  color: "var(--color-text)",
};

const historyEntryTopRowStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "var(--space-glue)",
  width: "100%",
};

const historyEntryTitleStyle: CSSProperties = {
  fontSize: "var(--font-size-sm)",
  fontWeight: 500,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
  flex: 1,
};

const historyEntryMetaStyle: CSSProperties = {
  fontSize: "var(--fs-label)",
  color: "var(--color-text-muted)",
  display: "flex",
  gap: "var(--space-glue)",
  alignItems: "center",
};

function statusBadgeColor(status: string): string {
  if (status === "final") return "var(--color-success)";
  if (status === "aborted") return "var(--color-danger)";
  return "var(--color-text-muted)"; // "active" or anything unrecognised
}

/** History browse view — shared by "browsing while a session is open" and
 *  "browsing with nothing open" (Chat.tsx mounts this component either way
 *  once P4's history affordance has been opened). */
function SubSessionHistoryList({
  history,
  historyLoading,
  onReopen,
}: {
  history: SubSessionSummaryDto[] | null;
  historyLoading: boolean;
  onReopen: (subSessionId: number) => void;
}) {
  if (historyLoading && history === null) {
    return (
      <p
        className="lmchat-subsession-hint"
        style={{ padding: "var(--space-sibling-relaxed)" }}
      >
        Loading past sessions…
      </p>
    );
  }
  if (history === null || history.length === 0) {
    return (
      <p
        className="lmchat-subsession-hint"
        style={{ padding: "var(--space-sibling-relaxed)" }}
      >
        No past sessions in this chat yet — start one with a slash command
        like /research or /code.
      </p>
    );
  }
  return (
    <div style={historyListStyle} role="list" aria-label="Past sub-sessions">
      {history.map((entry) => {
        const modeLabel = getPreset(entry.preset_id)?.label ?? entry.preset_id;
        const title = entry.title ?? modeLabel;
        return (
          <button
            key={entry.id}
            type="button"
            role="listitem"
            style={historyEntryStyle}
            onClick={() => {
              onReopen(entry.id);
            }}
          >
            <div style={historyEntryTopRowStyle}>
              <span style={historyEntryTitleStyle} title={title}>
                {title}
              </span>
              <span
                style={{
                  fontSize: "var(--fs-label)",
                  color: statusBadgeColor(entry.status),
                  textTransform: "uppercase",
                  letterSpacing: "0.02em",
                }}
              >
                {entry.status}
              </span>
            </div>
            <div style={historyEntryMetaStyle}>
              <span>{modeLabel}</span>
              <span aria-hidden="true">·</span>
              <span>{formatRelativeTime(entry.created_at)}</span>
            </div>
          </button>
        );
      })}
    </div>
  );
}

export function SubSessionPanel({
  subSession,
  sseState,
  onFinalize,
  onInject,
  onCancel,
  history,
  historyLoading,
  isHistoryOpen,
  onOpenHistory,
  onCloseHistory,
  onReopen,
}: SubSessionPanelProps) {
  const streaming = sseState.status === "streaming";
  const isReasoning =
    streaming && sseState.reasoning_content && !sseState.content;

  // Anti-jump (2026-06-18): the just-completed assistant turn must render
  // in the SAME slot it streamed in — AFTER the tool cards — not jump up
  // into the settled-messages list above the cards. Split a trailing assistant
  // turn out of the head list and re-render it below the cards. (The streaming
  // in-flight message and this settled one are mutually exclusive.)
  const _msgs = subSession?.messages ?? [];
  const _last = _msgs.length > 0 ? _msgs[_msgs.length - 1] : undefined;
  const trailingAssistant =
    _last?.role === "assistant" ? _last : null;
  const headMessages =
    trailingAssistant !== null ? _msgs.slice(0, -1) : _msgs;

  // P4: browsing history takes over the panel body while open — a live
  // session underneath keeps streaming server-side (closing history just
  // returns to viewing it, mirroring closeSubSessionPanel never aborting
  // an in-flight stream).
  if (isHistoryOpen) {
    return (
      <div className="lmchat-subsession-outer">
        <div style={historyBarStyle}>
          <span className="lmchat-subsession-label">Sub-session history</span>
          <button
            type="button"
            onClick={onCloseHistory}
            className="lmchat-subsession-cancel-btn"
            aria-label="Close sub-session history"
          >
            <X size={14} aria-hidden />
          </button>
        </div>
        <SubSessionHistoryList
          history={history}
          historyLoading={historyLoading}
          onReopen={onReopen}
        />
      </div>
    );
  }

  if (subSession === null) return null;

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
            onClick={onOpenHistory}
            className="lmchat-subsession-cancel-btn"
            aria-label="Sub-session history"
            title="Browse past sub-sessions"
          >
            <HistoryIcon size={14} aria-hidden />
          </button>
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
