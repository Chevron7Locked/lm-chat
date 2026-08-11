/* SPDX-License-Identifier: Apache-2.0 */
/**
 * deriveMessageList — pure derivation of Chat.tsx's rendered message list.
 *
 * Extracts the message-merge triangle (server rows + optimistic user
 * bubble + in-flight streaming assistant) out of the Chat() component
 * body so it can be unit tested directly, with no React rendering
 * required. Chat.tsx still owns
 * the JSX emission and the non-derivation state (reasoningCount/contentCount/
 * hasReasoningContent/historyTokens feed presence/context-meter, not this
 * list, and stay in Chat.tsx).
 *
 * This function reads ONLY its args — no hooks, no refs, no module state.
 */
import type { ChatMessageData, MessageRole } from "@/components/ChatMessage";
import type { MessageRecord } from "@/hooks/useChats";
import type { StreamState, StreamStats } from "@/hooks/useSSE";

export function deriveMessageList(args: {
  serverMessagesRaw: MessageRecord[];
  finalStats: StreamStats | null;
  pendingUser: { text: string; baseline: number } | null;
  sseState: StreamState;
}): {
  // The caller (Chat.tsx) consumes only activeServerMessages,
  // optimisticUserMessages, streamActive, streamingMessages, allMessages, and
  // allMessagesForPins. serverMessages, displayStreamContent, streamingKey, and
  // persistedHasSameKey are internal intermediates deliberately surfaced on the
  // contract so the race-guard behavior can be pinned by DIRECT unit tests
  // (test_deriveMessageList.spec.ts) rather than only through <Chat> renders.
  serverMessages: ChatMessageData[];
  activeServerMessages: ChatMessageData[];
  optimisticUserMessages: ChatMessageData[];
  displayStreamContent: string;
  streamActive: boolean;
  streamingKey: number | string;
  persistedHasSameKey: boolean;
  streamingMessages: ChatMessageData[];
  allMessages: ChatMessageData[];
  allMessagesForPins: { id: number; content: string; role: MessageRole }[];
} {
  const {
    serverMessagesRaw: _serverMessagesRaw,
    finalStats: _finalStats,
    pendingUser,
    sseState,
  } = args;

  // Build the message list: server messages + in-flight streaming assistant.
  // Carry finalStats onto the LAST assistant server message so the
  // stats chip stays mounted when the streaming bubble swaps for the
  // persisted message. Without this, the chip was on the streaming
  // bubble (which has stats) but absent on the persisted message
  // (DB rows don't carry stats), so the swap shrunk the message by
  // ~20px and read as a layout reload. finalStatsRef is reset on
  // each new submit (~line 574), so prior turns / page revisits
  // correctly render without a phantom chip.
  const _lastIdx = _serverMessagesRaw.length - 1;
  const serverMessages: ChatMessageData[] = _serverMessagesRaw.map(
    (m, idx) => ({
      id: m.id,
      role: m.role,
      content: m.content,
      reasoning_content: m.reasoning_content,
      // Persisted source of the unified Continue affordance. stop_reason
      // survives reload via migration 0026, so the chip no longer
      // vanishes when the streaming bubble swaps for the refetched row.
      showContinue: m.stop_reason === "length",
      // Persisted tool calls (migration 0024) thread onto the message
      // card so ToolCallCards survive reload.
      ...(m.tool_calls != null && m.tool_calls.length > 0
        ? { toolCalls: m.tool_calls }
        : {}),
      ...(idx === _lastIdx && m.role === "assistant" && _finalStats !== null
        ? { stats: _finalStats }
        : {}),
      compaction_id: m.compaction_id ?? null,
    }),
  );

  const activeServerMessages = serverMessages.filter(
    (_m, i) => _serverMessagesRaw[i]?.compaction_id == null,
  );

  // Optimistic user bubble — present from the moment of send until the refetch
  // brings the persisted copy in (see pendingUser effect above).
  const optimisticUserMessages: ChatMessageData[] =
    pendingUser !== null
      ? [
          {
            id: "pending-user",
            role: "user" as const,
            content: pendingUser.text,
            reasoning_content: null,
          },
        ]
      : [];

  // Keep the streamed assistant on screen through the brief complete→refetch
  // gap (status flips to "complete" before the refetch lands); contentDeltas
  // persist after completion, so holding it here prevents a blink-out.
  // Also keep visible while "stopped" so the partial with a "Stopped"
  // chip stays on screen until the 600ms refetch + comparison.
  const streamActive =
    sseState.status === "streaming" ||
    sseState.status === "stopped" ||
    (sseState.status === "complete" && pendingUser !== null);

  // Strip the hidden followups comment from the displayed content.
  //
  // During streaming, the model emits `<!--followups:["q1","q2"]-->`
  // over multiple delta chunks and the closing `-->` arrives
  // ~500-1000ms after the opening `<!--followups:`. `extractFollowups`
  // uses an `\s*$` end-anchored regex, so it only matches once the
  // closer lands — meaning the user sees
  // `<!--followups:["How do octopuses...` token-streaming INTO the
  // bubble until the closer arrives, then it abruptly vanishes.
  // That's the "view reloads a second after the message finishes"
  // symptom, distinct from the React key swap (which is also real
  // and handled separately below).
  //
  // Fix: truncate the display at the FIRST occurrence of
  // `<!--followups` regardless of whether the closer has arrived
  // yet. extractFollowups is still used in the complete-handler to
  // parse the followups payload for the chip row — that path runs
  // AFTER the stream ends, where the closer is guaranteed present.
  const rawStreamContent = sseState.contentDeltas.join("");
  const _markerIdx = rawStreamContent.indexOf("<!--followups");
  const displayStreamContent =
    _markerIdx >= 0
      ? rawStreamContent.slice(0, _markerIdx).trimEnd()
      : rawStreamContent;

  // The streaming bubble must share its React key with the eventual
  // persisted row, or the swap at end-of-stream unmounts the bubble
  // (key="streaming") and mounts the persisted one (key=<msg_id>),
  // re-parsing markdown + re-running syntax highlighter — the visible
  // "view reloads a second after the message" flash. `sseState.messageId`
  // is set on chat.start and equals the eventual persisted id, so using
  // it here means same-key → in-place reconciliation across the swap.
  const streamingKey: number | string =
    typeof sseState.messageId === "number" ? sseState.messageId : "streaming";
  // Race-window guard: between refetch-lands and pendingUser-clears,
  // `serverMessages` can already contain the persisted row with id
  // === sseState.messageId while `streamActive` is still true (because
  // pendingUser hasn't been nulled by its own effect yet). Without
  // this guard both would render with the same key → React duplicate-
  // key warning + a remount of one of them — re-introducing the flash
  // the stable-key change was meant to eliminate.
  const persistedHasSameKey =
    typeof streamingKey === "number" &&
    _serverMessagesRaw.some((m) => m.id === streamingKey);
  const streamingMessages: ChatMessageData[] =
    streamActive && !persistedHasSameKey
      ? [
          {
            id: streamingKey,
            role: "assistant" as const,
            content: displayStreamContent,
            reasoning_content: sseState.reasoningDeltas.join("") || null,
            toolCalls: sseState.toolCalls,
            streaming: sseState.status === "streaming",
            // Surface a "Stopped" chip when the user stopped.
            stopped: sseState.status === "stopped",
            // Live source of the unified Continue affordance
            // (stop_reason === "length" OR truncated_without_terminal —
            // computed in useSSE). ChatMessage suppresses it while
            // streaming === true, so it appears only after the
            // terminal frame.
            showContinue: sseState.showContinue,
            // Live stats during streaming; final stats after complete.
            stats:
              sseState.status === "streaming"
                ? sseState.stats
                : (_finalStats ?? sseState.stats),
            // Model-load / prompt-processing phase — surfaced as a quiet line
            // inside the in-flight turn's ProcessStream (pre-token window). Only
            // meaningful while streaming with no content yet.
            loadPhase:
              sseState.status === "streaming" ? sseState.loadPhase : null,
          },
        ]
      : [];

  const allMessages = [
    ...serverMessages,
    ...optimisticUserMessages,
    ...streamingMessages,
  ];

  // The pre-token "thinking" state is now rendered INSIDE the in-flight
  // assistant turn's ProcessStream (one calm surface), not as a
  // separate indicator below the message list. The hand-off that used to
  // mount/unmount a standalone ThinkingIndicator is gone — the streaming
  // ChatMessage carries loadPhase + reasoning and owns the single cursor.

  // PinNavStrip + PinnedMessagesPanel consume a simplified message list that
  // includes only the persisted (numeric-id) messages — the optimistic
  // streaming row uses "streaming" as id and can't be a pin target.
  const allMessagesForPins = serverMessages
    .map((m) => ({
      id: typeof m.id === "number" ? m.id : Number(m.id),
      content: m.content,
      role: m.role,
    }))
    .filter((m) => Number.isFinite(m.id));

  return {
    serverMessages,
    activeServerMessages,
    optimisticUserMessages,
    displayStreamContent,
    streamActive,
    streamingKey,
    persistedHasSameKey,
    streamingMessages,
    allMessages,
    allMessagesForPins,
  };
}
