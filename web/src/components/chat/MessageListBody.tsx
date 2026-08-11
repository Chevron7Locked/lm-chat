/* SPDX-License-Identifier: Apache-2.0 */
import type { RefObject } from "react";
import type { NavigateFunction } from "react-router-dom";
import { resolveChatIntegrationsField } from "@/components/Composer";
import { ChatMessage } from "@/components/ChatMessage";
import type { ChatMessageData } from "@/components/ChatMessage";
import type { ChatStreamPayload, StreamState } from "@/hooks/useSSE";
import type { CompactionSpan } from "@/hooks/useCompactions";
import type { ChatSummary } from "@/hooks/useChats";
import type { PushOptions } from "@/stores/toastStore";
import { CompactionTab } from "@/components/CompactionTab";
import { FirstMessageHint } from "@/components/chat/FirstMessageHint";
import { SubchatDivider } from "@/components/chat/SubchatDivider";
import { DetachFromProjectMarker } from "@/components/chat/DetachFromProjectMarker";
import { FollowupChips } from "@/components/chat/FollowupChips";
import { MemorySavedIndicator } from "@/components/chat/MemorySavedIndicator";
import { StreamErrorBanner } from "@/components/chat/StreamErrorBanner";

// ─── MessageListBody — the "normal" message-list render ─────────────────────
// Extracts the final else-branch of the messages-area's 4-way conditional
// (EmptyState | ABComparePane | SubSessionPanel | this) out of Chat.tsx's
// render. Pure move — no logic/behavior change; every identifier the JSX
// reads from Chat's closure is threaded through as a prop with the same
// value. The three sibling branches (EmptyState, ABComparePane,
// SubSessionPanel) stay in Chat.tsx.

interface MessageListBodyProps {
  allMessages: ChatMessageData[];
  sseState: StreamState;
  currentChat: ChatSummary | undefined;
  activeServerMessages: ChatMessageData[];
  compactions: CompactionSpan[];
  chatId: number;
  handleEditUserMessage: (
    messageId: number,
    newContent: string,
  ) => Promise<void>;
  handleRegenerateClick: (messageId: number) => void;
  handleResendClick: (messageId: number) => void;
  handleDeleteMessage: (messageId: number) => void;
  onLaunchMode: (presetId: string) => void;
  currentPersonaLabel: string | undefined;
  recentlyStreamedIdRef: RefObject<number | null>;
  activePresetLabel: string | null;
  pendingUser: { text: string; baseline: number } | null;
  optimisticUserMessages: ChatMessageData[];
  streamingMessages: ChatMessageData[];
  followupSuggestions: string[];
  resolveTurnModel: () => string;
  push: (opts: PushOptions) => string;
  handleSubmit: (
    cid: number,
    payload: ChatStreamPayload,
    userText: string,
    presetLabel?: string,
  ) => void;
  mtpSuspectedShownRef: RefObject<Set<number>>;
  lastSubmitRef: RefObject<{
    chatId: number;
    payload: ChatStreamPayload;
  } | null>;
  handleStreamRetry: () => void;
  stopStream: () => void;
  navigate: NavigateFunction;
  messagesEndRef: RefObject<HTMLDivElement | null>;
}

export function MessageListBody({
  allMessages,
  sseState,
  currentChat,
  activeServerMessages,
  compactions,
  chatId,
  handleEditUserMessage,
  handleRegenerateClick,
  handleResendClick,
  handleDeleteMessage,
  onLaunchMode,
  currentPersonaLabel,
  recentlyStreamedIdRef,
  activePresetLabel,
  pendingUser,
  optimisticUserMessages,
  streamingMessages,
  followupSuggestions,
  resolveTurnModel,
  push,
  handleSubmit,
  mtpSuspectedShownRef,
  lastSubmitRef,
  handleStreamRetry,
  stopStream,
  navigate,
  messagesEndRef,
}: MessageListBodyProps) {
  return (
    <>
      {allMessages.length === 0 && sseState.status !== "streaming" && (
        <FirstMessageHint />
      )}
      {/* When the chat carries `detached_from_project_meta`, render
          a separator turn at the top of history showing
          "Detached from {name} on {date}". The project name
          links to /project/:id when the project still exists;
          plain text when it has been deleted. */}
      {currentChat?.detached_from_project_meta != null && (
        <DetachFromProjectMarker
          meta={currentChat.detached_from_project_meta}
        />
      )}
      {(() => {
        const renderItems: ({ kind: "tab"; span: CompactionSpan } | { kind: "msg"; msg: ChatMessageData })[] = [];
        let _ci = 0;
        for (const msg of activeServerMessages) {
          // Capture-and-guard instead of index + `!` (noUncheckedIndexedAccess
          // makes compactions[_ci] `CompactionSpan | undefined`).
          let span = compactions[_ci];
          while (span !== undefined && span.anchor_msg_id <= Number(msg.id)) {
            renderItems.push({ kind: "tab", span });
            _ci++;
            span = compactions[_ci];
          }
          renderItems.push({ kind: "msg", msg });
        }
        let tail = compactions[_ci];
        while (tail !== undefined) {
          renderItems.push({ kind: "tab", span: tail });
          _ci++;
          tail = compactions[_ci];
        }
        return renderItems.map((item) => {
          if (item.kind === "tab") {
            return <CompactionTab key={`c-${String(item.span.id)}`} compaction={item.span} chatId={chatId} />;
          }
          return (
            <ChatMessage
              key={item.msg.id}
              message={item.msg}
              streamingActive={sseState.status === "streaming"}
              onEditUserMessage={handleEditUserMessage}
              onRegenerate={handleRegenerateClick}
              onResend={handleResendClick}
              onDeleteMessage={handleDeleteMessage}
              onLaunchMode={onLaunchMode}
              personaLabel={item.msg.role === "assistant" ? currentPersonaLabel : undefined}
              skipEntranceAnimation={
                typeof item.msg.id === "number" &&
                item.msg.id === recentlyStreamedIdRef.current
              }
            />
          );
        });
      })()}
      {/* Subchat-frame divider: thin labeled rule injected
          before the optimistic user+stream turn when a preset was active.
          Matches the subchat-frame label from the previous JS implementation. */}
      {activePresetLabel !== null && pendingUser !== null && (
        <SubchatDivider label={activePresetLabel} />
      )}
      {[...optimisticUserMessages, ...streamingMessages].map((msg) => (
        <ChatMessage
          key={msg.id}
          message={msg}
          streamingActive={sseState.status === "streaming"}
          onEditUserMessage={handleEditUserMessage}
          onRegenerate={handleRegenerateClick}
          onResend={handleResendClick}
          onDeleteMessage={handleDeleteMessage}
          onLaunchMode={onLaunchMode}
          personaLabel={msg.role === "assistant" ? currentPersonaLabel : undefined}
        />
      ))}
      {/* The pre-token "thinking" state now lives inside
          the in-flight assistant turn's ProcessStream (rendered as part
          of the streamingMessages map above) — no separate indicator. */}
      {/* Follow-up suggestion chips. Shown after the last
          completed model turn when the model emits suggestions.
          Clicking a chip sends it immediately as the next message.
          FollowupChips returns null when suggestions is empty, so
          no space is reserved until the first turn's chips arrive.
          The CSS animation fades-in the row on mount. */}
      <FollowupChips
        suggestions={followupSuggestions}
        streaming={sseState.status === "streaming"}
        onSelect={(q) => {
          if (sseState.status === "streaming") return;
          const model = resolveTurnModel();
          if (model === "") {
            // Silent-failure guard: a followup click with no resolvable
            // model used to no-op — surface the same prompt the other
            // submit paths use.
            push({
              variant: "error",
              message: "Pick a model before sending.",
            });
            return;
          }
          const followupIntegrations = resolveChatIntegrationsField(chatId);
          const payload: ChatStreamPayload = {
            input: [{ type: "text", content: q }],
            model,
            ...(followupIntegrations !== undefined && { integrations: followupIntegrations }),
          };
          handleSubmit(chatId, payload, q);
        }}
      />
      {/* Quiet auto-memory-saved indicator for the just-finished turn.
          Renders nothing when no memory.saved frame arrived this turn (the
          common case: nothing durable to store, or a slow distill still
          running in the background — see MemorySavedIndicator). */}
      <MemorySavedIndicator memorySaved={sseState.memorySaved} />
      {sseState.status === "error" && sseState.error !== null && (
        <StreamErrorBanner
          error={sseState.error}
          chatId={chatId}
          mtpAlreadyShown={mtpSuspectedShownRef.current.has(chatId)}
          canRetry={lastSubmitRef.current !== null}
          onRetry={handleStreamRetry}
          onDismiss={() => {
            stopStream();
          }}
          onSignIn={() => {
            stopStream();
            void navigate("/login");
          }}
          onOpenSettings={() => {
            stopStream();
            void navigate("/settings/lm-studio");
          }}
        />
      )}
      <div ref={messagesEndRef} />
    </>
  );
}
