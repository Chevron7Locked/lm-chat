/* SPDX-License-Identifier: Apache-2.0 */
import "@/styles/chat.css";
import "@/styles/chat-message.css";
/**
 * ChatMessage — renders a single chat message.
 *
 * Supports roles: user / assistant / system / tool.
 * Markdown rendered via react-markdown 10 + rehype-highlight 7 + rehype-sanitize 6.
 * Code fences use CodeBlock (copy button + language label).
 * Reasoning content + tool calls + the pre-token "thinking" state are rendered
 * by ONE calm ProcessStream surface (replaces the former ThinkingIndicator +
 * ThinkingBlock + ToolCallCard chain): reasoning streams live while thinking,
 * auto-collapses to a single "Reasoning" line when the answer starts, and tool
 * calls render as quiet inline lines instead of layout-shifting cards.
 *
 * Code-fence regression: react-markdown 10.1.0 + rehype-highlight 7.0.2
 * renders fenced code blocks correctly without the v0.5.x extract-then-restore
 * workaround. The regression test confirms this.
 */
import type { ReactElement, ReactNode } from "react";
import {
  Children,
  Fragment,
  isValidElement,
  memo,
  useState,
  useRef,
  useEffect,
} from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import rehypeHighlight from "rehype-highlight";
import { common } from "lowlight";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import type { ExtraProps } from "react-markdown";
import { CodeBlock } from "@/components/CodeBlock";
import { ProcessStream } from "@/components/ProcessStream";
import { CitationBadge, tokenizeCitations } from "@/components/CitationBadge";
import type { ToolCall, StreamStats, LoadPhase } from "@/hooks/useSSE";
import { Copy, Check, Trash2, GitFork } from "lucide-react";
import { useToastStore } from "@/stores/toastStore";
import { detectLaunchableModes } from "@/lib/launchableModes";
import { PRESETS } from "@/lib/presets";

// ─── Types ──────────────────────────────────────────────────────────────────

export type MessageRole = "user" | "assistant" | "system" | "tool";

export interface ChatMessageData {
  id: number | string;
  role: MessageRole;
  content: string;
  reasoning_content?: string | null | undefined;
  toolCalls?: ToolCall[] | undefined;
  /** True while this message is the in-flight streaming assistant turn. */
  streaming?: boolean;
  /**
   * True when the user hit Stop mid-reply.
   * Renders a "Stopped" chip beside the content.
   */
  stopped?: boolean;
  /**
   * Unified Continue affordance. True when the assistant's reply hit
   * max_output_tokens (stop_reason === "length" from chat.end) OR the
   * SSE reader exhausted before any terminal frame arrived
   * (truncated_without_terminal). One chip, two sources. Hidden while
   * streaming; a non-interactive marker that tells the user the answer is
   * unfinished.
   */
  showContinue?: boolean;
  /**
   * Per-message stats chip. Populated from SSE StreamStats for
   * the streaming/just-completed assistant turn. Omitted for persisted messages
   * (backend quota endpoint has no per-message split).
   */
  stats?: StreamStats | undefined;
  /**
   * Persona / sub-agent label for this assistant turn. Derived from the
   * chat's active_preset → preset.label ("General", "Coder", "Research"…)
   * or the sub-session's presetLabel for sub-agent turns. Omitted for user
   * and system messages. Renders as a subtle chip above the bubble so the
   * user can see at a glance which persona produced the turn.
   */
  personaLabel?: string | undefined;
  /**
   * FK to compactions.id when the row has been archived by a /compact call.
   * NULL for rows written before migration 0037 (no backfill).
   */
  compaction_id?: number | null;
  /**
   * Model-load / prompt-processing phase for the in-flight streaming turn.
   * Surfaced as a quiet single line inside the ProcessStream during the
   * pre-token window. Omitted for persisted / non-streaming messages.
   */
  loadPhase?: LoadPhase | null | undefined;
}

// ─── Rehype sanitize schema ─────────────────────────────────────────────────

// Allow highlight.js class names on code/span elements (prefixed with "hljs-").
const sanitizeSchema = {
  ...defaultSchema,
  attributes: {
    ...defaultSchema.attributes,
    code: [
      ...(defaultSchema.attributes?.code ?? []),
      ["className", /^language-/, /^hljs/],
    ],
    span: [...(defaultSchema.attributes?.span ?? []), ["className", /^hljs/]],
    pre: [...(defaultSchema.attributes?.pre ?? [])],
  },
};

// ─── Component ──────────────────────────────────────────────────────────────

interface ChatMessageProps {
  message: ChatMessageData;
  /**
   * When true, the streaming indicator is active globally and
   * Edit / Regenerate triggers are hidden.
   */
  streamingActive?: boolean;
  /**
   * Called when the user submits an inline edit on a user-role
   * message.  The id is the persisted message id (number); the second
   * arg is the new content.  Falsy when editing isn't supported (e.g.
   * the optimistic streaming row uses id "streaming").
   */
  onEditUserMessage?: (messageId: number, newContent: string) => Promise<void>;
  /**
   * Called when the user clicks Regenerate on an assistant
   * message.  Receives the assistant message id.
   */
  onRegenerate?: (messageId: number) => void;
  /**
   * Called when the user clicks Resend on a user message.  Rewinds the
   * conversation to this user turn (discards the following assistant
   * response and any later messages) and re-streams a fresh reply.
   * Receives the user message id.  When absent, the Resend button is not
   * rendered.
   */
  onResend?: (messageId: number) => void;
  /**
   * Called when the user clicks "Fork from here" on an assistant message.
   * Branches the main thread at this point into a new chat (backend copies
   * every message up to and including this one). Receives the assistant
   * message id. When absent, the Fork button is not rendered.
   */
  onForkFromHere?: (messageId: number) => void;
  /**
   * Called when the user clicks Delete on a message (any role). Receives
   * the persisted message id. Optional —
   * when absent the Delete button is not rendered.
   */
  onDeleteMessage?: (messageId: number) => void;
  /**
   * Persona / sub-agent label to render as a subtle chip on this assistant
   * turn. Derived upstream from the chat's active preset or sub-session label.
   * When absent, no per-turn label chip is rendered.
   */
  personaLabel?: string | undefined;
  /**
   * When true, suppresses the entrance animation on this message row.
   * Used to prevent the just-completed streamed message from re-animating
   * when the persisted copy replaces the streaming bubble.
   */
  skipEntranceAnimation?: boolean | undefined;
  /**
   * Called when the user clicks a "Launch /research"-style chip below an
   * assistant turn. Receives the preset id (e.g. ``"research"``) to launch
   * as a clean-context sub-session — the same path the composer's slash
   * command takes. When absent, no chip row is rendered even if the
   * message text references a launchable mode.
   */
  onLaunchMode?: (presetId: string) => void;
}

// Local-only — the Copy affordance lives next to Edit / Regenerate in the
// action row. Owns its own copied-icon + reset-timer state so neither the
// parent nor a global store has to track per-message UI flicker.
interface CopyMessageButtonProps {
  content: string;
  messageId: string;
}
function CopyMessageButton({ content, messageId }: CopyMessageButtonProps) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const push = useToastStore((s) => s.push);
  useEffect(
    () => () => {
      if (timerRef.current !== null) clearTimeout(timerRef.current);
    },
    [],
  );
  function onSuccess(): void {
    setCopied(true);
    if (timerRef.current !== null) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => {
      setCopied(false);
    }, 2_500);
    push({ variant: "success", message: "Message copied." });
  }
  function fallbackCopy(): boolean {
    // execCommand path — works in non-secure contexts (HTTP IPs) and in
    // browsers that block navigator.clipboard.writeText after an async
    // boundary. Stages a hidden textarea, selects it, runs execCommand.
    const ta = document.createElement("textarea");
    ta.value = content;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.left = "-9999px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    try {
      ta.select();
      ta.setSelectionRange(0, content.length);
      // eslint-disable-next-line @typescript-eslint/no-deprecated
      const ok = document.execCommand("copy");
      return ok;
    } catch {
      return false;
    } finally {
      document.body.removeChild(ta);
    }
  }
  function handleCopy(): void {
    // Called DURING the click handler (no async boundary before the
    // clipboard call) so Safari + Firefox keep the user-gesture
    // context. navigator.clipboard.writeText is preferred (modern,
    // supports rich payloads); if it's unavailable OR throws, fall
    // back to the deprecated execCommand path which still works on
    // insecure / older contexts. Failure paths log to console so the
    // so the user can diagnose a real environment issue rather than
    // staring at a generic toast.
    const clip = navigator.clipboard as unknown as Clipboard | undefined;
    if (clip !== undefined) {
      // Fire and forget — DO NOT await here: that would create an
      // async boundary that breaks Safari's gesture-context check.
      clip.writeText(content).then(
        () => {
          onSuccess();
        },
        (err: unknown) => {
          console.warn("ChatMessage.copy.clipboard_api_failed", err);
          if (fallbackCopy()) {
            onSuccess();
          } else {
            push({
              variant: "error",
              message: "Couldn't copy that — try again.",
            });
          }
        },
      );
      return;
    }
    if (fallbackCopy()) {
      onSuccess();
    } else {
      console.warn(
        "ChatMessage.copy.no_clipboard_and_execcommand_failed",
        { isSecureContext: window.isSecureContext },
      );
      push({ variant: "error", message: "Couldn't copy that — try again." });
    }
  }
  return (
    <button
      type="button"
      onClick={handleCopy}
      aria-label={copied ? "Copied!" : "Copy message"}
      title={copied ? "Copied!" : "Copy message"}
      className="lmchat-message-action-btn"
      data-testid={`chat-message-copy-btn-${messageId}`}
    >
      {copied ? (
        <Check size={14} aria-hidden />
      ) : (
        <Copy size={14} aria-hidden />
      )}
    </button>
  );
}

// Local-only — Delete button in the message action row. A single click
// pushes a toast-styled "Delete this message?" prompt with a Delete
// action button (the X on the toast cancels). In-button two-step
// confirms are easy to misclick and
// don't read as confirmation prompts; a toast does.
interface DeleteMessageButtonProps {
  messageId: number;
  messageRole: "user" | "assistant" | "system" | "tool";
  onDelete: (messageId: number) => void;
}
function DeleteMessageButton({
  messageId,
  messageRole,
  onDelete,
}: DeleteMessageButtonProps) {
  const push = useToastStore((s) => s.push);
  function handleClick(): void {
    const noun = messageRole === "user" ? "your message" : "this response";
    push({
      variant: "warning",
      title: "Delete message?",
      message: `Remove ${noun} from this chat. This can't be undone.`,
      duration: 8_000,
      action: {
        label: "Delete",
        onClick: () => {
          onDelete(messageId);
        },
      },
    });
  }
  return (
    <button
      type="button"
      onClick={handleClick}
      aria-label="Delete message"
      title="Delete message"
      className="lmchat-message-action-btn lmchat-message-action-btn--danger"
      data-testid={`chat-message-delete-btn-${String(messageId)}`}
    >
      <Trash2 size={14} aria-hidden />
    </button>
  );
}

function ChatMessageInner({
  message,
  streamingActive = false,
  onEditUserMessage,
  onRegenerate,
  onResend,
  onForkFromHere,
  onDeleteMessage,
  personaLabel,
  skipEntranceAnimation = false,
  onLaunchMode,
}: ChatMessageProps) {
  const isUser = message.role === "user";
  const isAssistant = message.role === "assistant";
  const isSystem = message.role === "system";
  const isTool = message.role === "tool";

  // A failed assistant turn (stream died after chat.start with no content)
  // was rendering as a phantom row
  // whose only content was the hover action bar. That created an
  // orphan "second action bar" floating below the real message. Hide
  // these empty assistant turns entirely — the user message above
  // keeps its own Regenerate action so the user can retry. We still
  // render assistant rows mid-stream (message.streaming === true) and
  // assistant rows that have any content (even just whitespace).
  if (
    isAssistant &&
    message.content === "" &&
    (message.reasoning_content === null ||
      message.reasoning_content === undefined ||
      message.reasoning_content === "") &&
    message.streaming !== true
  ) {
    return null;
  }

  // Inline edit state.  Only enabled for persisted user messages
  // (numeric id) and only when the parent supplies onEditUserMessage.
  const persistedNumericId =
    typeof message.id === "number" ? message.id : Number(message.id);
  const canEdit =
    isUser &&
    Number.isFinite(persistedNumericId) &&
    onEditUserMessage !== undefined &&
    !streamingActive;
  const canRegenerate =
    isAssistant &&
    Number.isFinite(persistedNumericId) &&
    onRegenerate !== undefined &&
    !streamingActive &&
    message.streaming !== true;
  const canResend =
    isUser &&
    Number.isFinite(persistedNumericId) &&
    onResend !== undefined &&
    !streamingActive;
  // Fork from here: branches the main thread at this assistant turn into a
  // new chat. Same gating as Regenerate — persisted assistant turns only,
  // never while a stream is in flight (the message boundary isn't stable
  // to fork from until the turn is actually done).
  const canFork =
    isAssistant &&
    Number.isFinite(persistedNumericId) &&
    onForkFromHere !== undefined &&
    !streamingActive &&
    message.streaming !== true;
  // Copy is available on any user / assistant turn that has content and
  // isn't actively streaming. Unlike Edit / Regenerate, it does not
  // require a persisted numeric id — copying from an optimistic
  // streaming-complete row is supported; the streaming row itself is
  // gated by ``message.streaming !== true``.
  const canCopy =
    (isUser || isAssistant) &&
    message.content !== "" &&
    message.streaming !== true;
  // Delete: available on persisted messages of any role except system/tool
  // (those are infrastructural). Without per-message delete a failed
  // assistant turn (empty body, only a
  // Regenerate button) looks isolated and can't be dismissed without
  // taking the whole chat down.
  const canDelete =
    (isUser || isAssistant) &&
    Number.isFinite(persistedNumericId) &&
    onDeleteMessage !== undefined &&
    !streamingActive &&
    message.streaming !== true;
  // Launch-mode chips: assistant turns only, and suppressed while this
  // turn is still streaming — the text (and thus the detected commands)
  // is still changing token-by-token, so a chip row would flicker in and
  // out as the model finishes typing a command name.
  const launchableModes =
    isAssistant && onLaunchMode !== undefined && message.streaming !== true
      ? detectLaunchableModes(message.content)
      : [];

  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [saveError, setSaveError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  function beginEdit(): void {
    setDraft(message.content);
    setEditing(true);
    setSaveError(null);
  }

  function cancelEdit(): void {
    setEditing(false);
    setDraft("");
    setSaveError(null);
  }

  async function saveEdit(): Promise<void> {
    const trimmed = draft.trim();
    if (trimmed === "") {
      setSaveError("Message can't be empty.");
      return;
    }
    if (onEditUserMessage === undefined) return;
    setSaving(true);
    setSaveError(null);
    try {
      await onEditUserMessage(persistedNumericId, trimmed);
      setEditing(false);
    } catch (err) {
      const e = err as { detail?: string; message?: string };
      setSaveError(e.detail ?? e.message ?? "Couldn't save — try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div
      data-message-id={String(message.id)}
      data-message-role={message.role}
      className="lmchat-message-row"
      {...(skipEntranceAnimation ? { "data-skip-animate": "" } : {})}
    >
      {/* Persona / sub-agent label — assistant turns only. Shows the persona
          name ("General", "Research", "Coder"…) or the active sub-agent so
          the user can tell at a glance which context produced the turn.
          Suppressed while streaming (label would be premature for the live caret
          row) and when no label is supplied by the parent. */}
      {isAssistant &&
        personaLabel !== undefined &&
        personaLabel !== "" &&
        message.streaming !== true && (
          <div
            className="lmchat-persona-label"
            data-testid="chat-message-persona-label"
            aria-label={`Response from ${personaLabel} persona`}
          >
            {personaLabel}
          </div>
        )}

      {/* ONE calm process surface — reasoning (live while thinking, collapsed
          but expandable after), inline tool lines, and the pre-token thinking
          state + single cursor. Replaces the former ThinkingIndicator +
          ThinkingBlock + ToolCallCard chain. Assistant turns only. */}
      {isAssistant && (
        <ProcessStream
          messageId={message.id}
          reasoning={message.reasoning_content}
          toolCalls={message.toolCalls}
          streaming={message.streaming === true}
          hasAnswer={message.content !== ""}
          loadPhase={message.loadPhase}
        />
      )}

      {/* Message bubble */}
      {message.content !== "" && !editing && (
        <div
          className={[
            isUser ? "lmchat-bubble-user" : "lmchat-bubble-assistant",
            isAssistant && message.streaming === true
              ? "lmchat-streaming-bubble"
              : "",
          ]
            .filter(Boolean)
            .join(" ")}
        >
          {isUser || isSystem || isTool ? (
            <PlainText text={message.content} muted={isSystem || isTool} />
          ) : (
            <MarkdownContent content={message.content} />
          )}
          {/* Pulsing caret at the stream tip.  Only rendered
              for the in-flight streaming assistant turn so completed
              messages never carry a phantom cursor. */}
          {isAssistant && message.streaming === true && (
            <span
              aria-hidden="true"
              className="lmchat-stream-caret"
              data-testid="chat-message-stream-caret"
            />
          )}
          {/* "Stopped" (user hit Stop) and "Cut off" (reply unfinished:
              stop_reason=length OR truncated_without_terminal; hidden while
              streaming so it doesn't flash mid-reply) are MUTUALLY EXCLUSIVE.
              The state machine upstream never sets both, but the ternary
              enforces the invariant at the render layer regardless — Stopped
              wins if both flags ever arrive together. */}
          {/* NOTE: Stopped is DELIBERATELY live-state-only — no persistence.
              "I stopped this turn just now" is a moment-in-time affordance,
              not a durable property of the message. After reload, the user's
              stop intent is no longer current — they're seeing a partial
              message but the stop act itself is in the past. The cut-off chip
              (next block) IS persisted via messages.stop_reason (migration
              0026) because truncation is a server-side property of the turn.
              The asymmetry is intentional; don't "fix" it. */}
          {isAssistant && message.stopped === true ? (
            <span
              role="status"
              className="lmchat-stopped-chip"
              data-testid="chat-message-stopped-chip"
            >
              Stopped
            </span>
          ) : isAssistant &&
            message.showContinue === true &&
            message.streaming !== true ? (
            // TODO: consider making this an interactive "Continue" button
            // that resubmits the turn with a "please continue" instruction.
            // A truncated reply is the natural one-click affordance moment —
            // the user already knows they want more and the label is right there.
            // Until that lands, keep this passive ("Cut off") so the visible
            // label doesn't read as a button.
            <span
              role="status"
              className="lmchat-continue-chip"
              data-testid="continue-chip"
              aria-label="Reply was cut off — send another message to continue."
              title="Reply was cut off — send another message to continue."
            >
              Cut off
            </span>
          ) : null}
        </div>
      )}

      {/* Launch-mode chip row — one "Launch /research"-style chip per
          sub-session preset command referenced in this assistant turn's
          text (capped at the 5 preset modes; see detectLaunchableModes).
          Sits below the bubble content, never inline-linkified into the
          markdown itself. */}
      {isAssistant && onLaunchMode !== undefined && launchableModes.length > 0 && (
        <div
          className="lmchat-launch-chips"
          role="group"
          aria-label="Suggested sub-agent modes"
          data-testid="launch-chips-row"
        >
          {launchableModes.map((presetId) => {
            const slashCmd = PRESETS[presetId]?.slashCmd;
            if (slashCmd === undefined || slashCmd === null) return null;
            return (
              <button
                key={presetId}
                type="button"
                onClick={() => {
                  onLaunchMode(presetId);
                }}
                className="lmchat-launch-chip"
                data-testid={`launch-chip-${presetId}`}
              >
                {`Launch /${slashCmd}`}
              </button>
            );
          })}
        </div>
      )}

      {/* Inline edit form for user messages. */}
      {editing && isUser && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            void saveEdit();
          }}
          className="lmchat-edit-form"
          data-testid={`chat-message-edit-form-${String(message.id)}`}
        >
          <textarea
            autoFocus
            value={draft}
            onChange={(e) => {
              setDraft(e.target.value);
            }}
            onKeyDown={(e) => {
              if (e.key === "Escape") {
                e.preventDefault();
                cancelEdit();
              }
              // Cmd/Ctrl+Enter submits.
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                void saveEdit();
              }
            }}
            rows={3}
            aria-label="Edit message"
            className="lmchat-edit-textarea"
            data-testid={`chat-message-edit-textarea-${String(message.id)}`}
          />
          {saveError !== null && (
            <p className="lmchat-edit-error" role="alert">
              {saveError}
            </p>
          )}
          <div className="lmchat-edit-actions">
            <button
              type="submit"
              disabled={saving}
              className="lmchat-edit-save-btn"
              data-testid={`chat-message-edit-save-${String(message.id)}`}
            >
              {saving ? "Saving…" : "Save"}
            </button>
            <button
              type="button"
              onClick={cancelEdit}
              className="lmchat-edit-cancel-btn"
              data-testid={`chat-message-edit-cancel-${String(message.id)}`}
            >
              Cancel
            </button>
          </div>
        </form>
      )}

      {/* Hover action row (Edit / Regenerate / Fork / Copy / Delete).
          Order: Edit → Regenerate → Fork → Copy → Delete. Delete is
          rightmost per the standard destructive-action-isolated pattern;
          Copy stays adjacent to Edit/Regenerate; Fork sits next to
          Regenerate since both are assistant-turn actions. */}
      {!editing &&
        (canCopy || canEdit || canResend || canRegenerate || canFork || canDelete) && (
        <div className="lmchat-message-actions">
          {canEdit && (
            <button
              type="button"
              onClick={beginEdit}
              aria-label="Edit message"
              title="Edit"
              className="lmchat-message-action-btn"
              data-testid={`chat-message-edit-btn-${String(message.id)}`}
            >
              <svg
                viewBox="0 0 24 24"
                width="14"
                height="14"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <path d="M12 20h9" />
                <path d="M16.5 3.5a2.121 2.121 0 0 1 3 3L7 19l-4 1 1-4Z" />
              </svg>
            </button>
          )}
          {canResend && (
            <button
              type="button"
              onClick={() => {
                onResend(persistedNumericId);
              }}
              aria-label="Resend message"
              title="Resend"
              className="lmchat-message-action-btn"
              data-testid={`chat-message-resend-btn-${String(message.id)}`}
            >
              {/* RotateCcw (lucide) — looping-arrow icon */}
              <svg
                viewBox="0 0 24 24"
                width="14"
                height="14"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" />
                <path d="M3 3v5h5" />
              </svg>
            </button>
          )}
          {canRegenerate && (
            <button
              type="button"
              onClick={() => {
                onRegenerate(persistedNumericId);
              }}
              aria-label="Regenerate response"
              title="Regenerate"
              className="lmchat-message-action-btn"
              data-testid={`chat-message-regenerate-btn-${String(message.id)}`}
            >
              <svg
                viewBox="0 0 24 24"
                width="14"
                height="14"
                fill="none"
                stroke="currentColor"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
                aria-hidden="true"
              >
                <polyline points="23 4 23 10 17 10" />
                <polyline points="1 20 1 14 7 14" />
                <path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10" />
                <path d="M20.49 15a9 9 0 0 1-14.85 3.36L1 14" />
              </svg>
            </button>
          )}
          {canFork && (
            <button
              type="button"
              onClick={() => {
                onForkFromHere(persistedNumericId);
              }}
              aria-label="Fork from here"
              title="Fork from here"
              className="lmchat-message-action-btn"
              data-testid={`chat-message-fork-btn-${String(message.id)}`}
            >
              <GitFork size={14} aria-hidden />
            </button>
          )}
          {canCopy && (
            <CopyMessageButton
              content={message.content}
              messageId={String(message.id)}
            />
          )}
          {canDelete && (
            <DeleteMessageButton
              messageId={persistedNumericId}
              messageRole={message.role}
              onDelete={onDeleteMessage}
            />
          )}
        </div>
      )}

      {/* Per-message stats chip (assistant only, quiet mono). */}
      {isAssistant &&
        message.stats !== undefined &&
        message.streaming !== true && (
          <MessageStatsChip stats={message.stats} />
        )}
      {/* NOTE: the pre-token / reasoning-phase cursor is owned by ProcessStream
          above (one calm cursor beneath the reasoning); the end-of-answer
          cursor is rendered inside the message bubble. There is no separate
          trailing cursor here — that would double the caret. */}
    </div>
  );
}

/**
 * Equality function for React.memo. Returns true (skip render) when every
 * field that affects ChatMessage output is unchanged between renders.
 *
 * Fields that MUST trigger a re-render:
 *   - message.id            — different message object entirely
 *   - message.content       — streaming appends here on every SSE delta
 *   - message.reasoning_content — thinking block grows during reasoning phase
 *   - message.streaming     — transitions from true → false when stream ends
 *   - message.toolCalls     — tool-call cards appear/update mid-stream
 *   - streamingActive       — controls visibility of Edit/Regenerate buttons
 *   - onEditUserMessage     — presence gates the edit button (but stable via
 *                             useCallback in Chat.tsx; included for correctness)
 *   - onRegenerate          — same as above
 *   - onResend              — same as above
 *   - onForkFromHere        — same as above (gates the Fork button)
 *   - onLaunchMode          — same as above (gates the launch-chip row)
 *
 * Fields intentionally EXCLUDED (do not need to re-render this message):
 *   - changes to other messages in the list (sibling content, count, order)
 *   - unrelated Chat.tsx state (panelView, confirmClear, slashPaletteOpen, …)
 */
function areChatMessagePropsEqual(
  prev: Readonly<ChatMessageProps>,
  next: Readonly<ChatMessageProps>,
): boolean {
  if (prev.message.id !== next.message.id) return false;
  if (prev.message.content !== next.message.content) return false;
  if (prev.message.reasoning_content !== next.message.reasoning_content)
    return false;
  if (prev.message.streaming !== next.message.streaming) return false;
  if (prev.message.stopped !== next.message.stopped) return false;
  if (prev.message.showContinue !== next.message.showContinue) return false;
  // loadPhase drives the quiet phase line in the pre-token ProcessStream window.
  if (prev.message.loadPhase !== next.message.loadPhase) return false;
  if (prev.streamingActive !== next.streamingActive) return false;
  if (prev.onEditUserMessage !== next.onEditUserMessage) return false;
  if (prev.onRegenerate !== next.onRegenerate) return false;
  if (prev.onResend !== next.onResend) return false;
  if (prev.onForkFromHere !== next.onForkFromHere) return false;
  if (prev.onLaunchMode !== next.onLaunchMode) return false;
  // stats: only the streaming message carries live stats; identity is enough.
  if (prev.message.stats !== next.message.stats) return false;
  if (prev.personaLabel !== next.personaLabel) return false;
  if (prev.skipEntranceAnimation !== next.skipEntranceAnimation) return false;

  // toolCalls: compare by length + last-item identity (fast path for the
  // append-only growth pattern during streaming; full structural equality
  // would require deep comparison of every argument/result string).
  const ptc = prev.message.toolCalls;
  const ntc = next.message.toolCalls;
  if (ptc !== ntc) {
    if (ptc === undefined || ntc === undefined) return false;
    if (ptc.length !== ntc.length) return false;
    // Check the last tool call's status/result in case it mutated in place
    // (e.g. pending → success) without changing the array length.
    const pi = ptc[ptc.length - 1];
    const ni = ntc[ntc.length - 1];
    if (pi !== ni) return false;
  }

  return true;
}

/**
 * ChatMessage — memoized single-message renderer.
 *
 * Re-renders only when this message's own content, streaming state, or
 * interaction props change. Sibling-message updates and unrelated Chat state
 * (panel open/close, confirm dialogs, etc.) are skipped.
 */
export const ChatMessage = memo(ChatMessageInner, areChatMessagePropsEqual);

// ─── Sub-components ─────────────────────────────────────────────────────────

function PlainText({ text, muted }: { text: string; muted: boolean }) {
  return (
    <p
      className={`lmchat-plain-text${muted ? " lmchat-plain-text--muted" : ""}`}
    >
      {text}
    </p>
  );
}

/**
 * Recursively walk a ReactNode tree, splitting any string nodes that
 * contain `[doc:<id>]` markers into a mix of plain text + {@link CitationBadge}.
 * Non-string nodes are passed through unchanged.  Used by the markdown
 * `p` renderer so citations injected by the RAG pipeline render as chips
 * inside paragraphs without disturbing other inline markup (links, bold,
 * code spans).
 */
function injectCitations(node: ReactNode): ReactNode {
  if (typeof node === "string") {
    if (!node.includes("[doc:")) return node;
    const tokens = tokenizeCitations(node);
    if (tokens.length === 0) return node;
    return (
      <>
        {tokens.map((tok, i) =>
          tok.kind === "text" ? (
            <Fragment key={`t-${String(i)}`}>{tok.value}</Fragment>
          ) : (
            <CitationBadge
              key={`c-${String(i)}-${tok.docId}`}
              docId={tok.docId}
            />
          ),
        )}
      </>
    );
  }
  if (Array.isArray(node)) {
    const arr = node as ReactNode[];
    return arr.map((child, i) => (
      <Fragment key={`arr-${String(i)}`}>{injectCitations(child)}</Fragment>
    ));
  }
  if (isValidElement(node)) {
    const el = node as ReactElement<{ children?: ReactNode }>;
    const children = el.props.children;
    if (children === undefined) return node;
    // Avoid recursing into code blocks — citations inside code fences are
    // literal text, not RAG markers, and CodeBlock owns its own rendering.
    if (
      typeof el.type === "string" &&
      (el.type === "code" || el.type === "pre")
    ) {
      return node;
    }
    const next = Children.map(children, (c) => injectCitations(c));
    return { ...el, props: { ...el.props, children: next } } as ReactElement;
  }
  return node;
}

function MarkdownContent({ content }: { content: string }) {
  return (
    <div className="lmchat-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[
          [rehypeHighlight, { detect: true, languages: common }],
          [rehypeSanitize, sanitizeSchema],
        ]}
        components={{
          // Route fenced code blocks through our CodeBlock with copy button.
          code: (
            props: ExtraProps & {
              className?: string | undefined;
              children?: ReactNode;
            },
          ) => <CodeBlock {...props} />,
          // Paragraph renderer — splits text children on `[doc:<id>]` markers
          // and substitutes CitationBadge instances inline.
          p: ({ children, ...rest }: ExtraProps & { children?: ReactNode }) => (
            <p {...rest}>{injectCitations(children)}</p>
          ),
          // Same treatment for list items + emphasis — citations may land
          // inside bullet lists or bold text in assistant turns.
          li: ({
            children,
            ...rest
          }: ExtraProps & { children?: ReactNode }) => (
            <li {...rest}>{injectCitations(children)}</li>
          ),
        }}
      >
        {content}
      </ReactMarkdown>
    </div>
  );
}

// ─── Edit / Regenerate UI styles moved to chat.css ───────────────────────────

// ─── MessageStatsChip ────────────────────────────────────────────────────────

/**
 * Quiet mono-font chip showing per-message stats (tok/s, TTFT, output tokens).
 * Only rendered for completed (non-streaming) assistant messages that have stats.
 */
function MessageStatsChip({ stats }: { stats: StreamStats }) {
  const parts: string[] = [];
  if (stats.tokensPerSecond !== null && stats.tokensPerSecond > 0) {
    parts.push(`${stats.tokensPerSecond.toFixed(1)} tok/s`);
  }
  if (stats.ttftSeconds !== null && stats.ttftSeconds > 0) {
    parts.push(`TTFT ${stats.ttftSeconds.toFixed(2)}s`);
  }
  if (stats.outputTokens > 0) {
    parts.push(`${String(stats.outputTokens)} tokens`);
  }
  if (parts.length === 0) return null;
  return (
    <div className="lmchat-stats-chip" data-testid="message-stats-chip">
      {parts.join(" · ")}
    </div>
  );
}
