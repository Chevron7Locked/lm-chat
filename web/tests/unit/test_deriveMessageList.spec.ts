/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Direct unit tests for `deriveMessageList` (web/src/lib/deriveMessageList.ts)
 * — decomposition cut #8's pure extraction out of Chat.tsx's message-merge
 * triangle. Unlike test_Chat_message_merge_characterization.spec.tsx (which
 * renders <Chat> and asserts on the DOM), these call the pure function
 * directly with no React involved — the payoff of the extraction.
 *
 * Covers the same behaviors the characterization suite pins, at the
 * function-signature level: history mapping + showContinue, the
 * compaction_id filter, the optimistic-user bubble, the streamActive
 * matrix, the streamingKey/persistedHasSameKey guard, the displayStreamContent
 * followups-marker strip (both an unclosed and a closed marker), and the
 * allMessages merge order.
 */
import { describe, it, expect } from "vitest";
import { deriveMessageList } from "@/lib/deriveMessageList";
import type { MessageRecord } from "@/hooks/useChats";
import type { StreamState, StreamStats } from "@/hooks/useSSE";

// ─── Fixtures ────────────────────────────────────────────────────────────────

function mkMessage(
  overrides: Partial<MessageRecord> & { id: number; role: MessageRecord["role"] },
): MessageRecord {
  return {
    chat_id: 1,
    content: "",
    reasoning_content: null,
    stop_reason: null,
    tool_calls: null,
    compaction_id: null,
    created_at: "2026-07-01T00:00:00Z",
    ...overrides,
  };
}

function mkStats(overrides: Partial<StreamStats> = {}): StreamStats {
  return { tokensPerSecond: null, ttftSeconds: null, outputTokens: 0, ...overrides };
}

function mkSSEState(overrides: Partial<StreamState> = {}): StreamState {
  return {
    status: "idle",
    // Matches baseArgs()'s chatId by default so every pre-existing
    // override below (which never touches chatId) keeps describing the
    // ordinary same-chat case. Cross-chat behavior is exercised by tests
    // that explicitly pass a mismatched chatId to deriveMessageList.
    chatId: 1,
    messageId: null,
    responseId: null,
    contentDeltas: [],
    reasoningDeltas: [],
    toolCalls: [],
    error: null,
    stats: mkStats(),
    loadPhase: null,
    truncated_without_terminal: false,
    stop_reason: null,
    showContinue: false,
    warnings: [],
    followups: [],
    memorySaved: undefined,
    modeAdopt: undefined,
    ...overrides,
  };
}

/** Baseline args — idle stream, no persisted rows, no pending optimistic bubble. */
function baseArgs(): Parameters<typeof deriveMessageList>[0] {
  return {
    serverMessagesRaw: [],
    finalStats: null,
    pendingUser: null,
    sseState: mkSSEState(),
    chatId: 1,
  };
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("deriveMessageList", () => {
  // 1. History mapping + showContinue(stop_reason === "length").
  it("maps server rows to ChatMessageData, surfacing showContinue from stop_reason", () => {
    const raw: MessageRecord[] = [
      mkMessage({ id: 1, role: "user", content: "Hi there" }),
      mkMessage({ id: 2, role: "assistant", content: "Truncated answer", stop_reason: "length" }),
      mkMessage({ id: 3, role: "assistant", content: "Clean answer", stop_reason: "stop" }),
    ];

    const result = deriveMessageList({ ...baseArgs(), serverMessagesRaw: raw });

    expect(result.serverMessages).toEqual([
      { id: 1, role: "user", content: "Hi there", reasoning_content: null, showContinue: false, compaction_id: null },
      { id: 2, role: "assistant", content: "Truncated answer", reasoning_content: null, showContinue: true, compaction_id: null },
      { id: 3, role: "assistant", content: "Clean answer", reasoning_content: null, showContinue: false, compaction_id: null },
    ]);
  });

  it("omits the toolCalls key entirely when a row has no persisted tool calls", () => {
    const raw: MessageRecord[] = [mkMessage({ id: 1, role: "assistant", content: "no tools", tool_calls: null })];
    const result = deriveMessageList({ ...baseArgs(), serverMessagesRaw: raw });
    expect(Object.prototype.hasOwnProperty.call(result.serverMessages[0], "toolCalls")).toBe(false);
  });

  it("attaches persisted tool_calls as toolCalls when present and non-empty", () => {
    const toolCalls = [{ id: "t1", name: "search", arguments: "{}", status: "success" as const }];
    const raw: MessageRecord[] = [mkMessage({ id: 1, role: "assistant", content: "used a tool", tool_calls: toolCalls })];
    const result = deriveMessageList({ ...baseArgs(), serverMessagesRaw: raw });
    expect(result.serverMessages[0]?.toolCalls).toEqual(toolCalls);
  });

  // 2. activeServerMessages filters out compaction_id != null.
  it("activeServerMessages excludes rows whose compaction_id is non-null", () => {
    const raw: MessageRecord[] = [
      mkMessage({ id: 1, role: "user", content: "kept" }),
      mkMessage({ id: 2, role: "assistant", content: "archived", compaction_id: 5 }),
      mkMessage({ id: 3, role: "assistant", content: "also kept" }),
    ];

    const result = deriveMessageList({ ...baseArgs(), serverMessagesRaw: raw });

    expect(result.serverMessages).toHaveLength(3);
    expect(result.activeServerMessages.map((m) => m.id)).toEqual([1, 3]);
  });

  // 3. optimisticUserMessages present iff pendingUser.
  it("includes an optimistic user bubble only when pendingUser is set", () => {
    const withPending = deriveMessageList({
      ...baseArgs(),
      pendingUser: { text: "hello from the composer", baseline: 0 },
    });
    expect(withPending.optimisticUserMessages).toEqual([
      { id: "pending-user", role: "user", content: "hello from the composer", reasoning_content: null },
    ]);

    const withoutPending = deriveMessageList({ ...baseArgs(), pendingUser: null });
    expect(withoutPending.optimisticUserMessages).toEqual([]);
  });

  // 4. streamActive matrix.
  it("streamActive is true while streaming", () => {
    const result = deriveMessageList({ ...baseArgs(), sseState: mkSSEState({ status: "streaming" }) });
    expect(result.streamActive).toBe(true);
  });

  it("streamActive is true while stopped", () => {
    const result = deriveMessageList({ ...baseArgs(), sseState: mkSSEState({ status: "stopped" }) });
    expect(result.streamActive).toBe(true);
  });

  it("streamActive is false while streaming when the stream belongs to a DIFFERENT chat", () => {
    // sseState is a single instance shared across chat navigation — a
    // status of "streaming" tagged for chat 2 must not make chat 1's own
    // derivation (chatId: 1, from baseArgs()) treat itself as live.
    const result = deriveMessageList({
      ...baseArgs(),
      sseState: mkSSEState({ status: "streaming", chatId: 2, contentDeltas: ["hello from chat 2"] }),
    });
    expect(result.streamActive).toBe(false);
    expect(result.streamingMessages).toEqual([]);
  });

  it("does NOT render a different chat's live content into this chat's message list", () => {
    const raw: MessageRecord[] = [mkMessage({ id: 1, role: "user", content: "hi" })];
    const result = deriveMessageList({
      ...baseArgs(),
      serverMessagesRaw: raw,
      sseState: mkSSEState({
        status: "streaming",
        chatId: 99,
        messageId: 50,
        contentDeltas: ["someone else's answer"],
      }),
    });
    // Only this chat's own persisted message — no phantom streaming
    // bubble borrowed from chat 99.
    expect(result.allMessages).toHaveLength(1);
    expect(result.allMessages.some((m) => m.content === "someone else's answer")).toBe(false);
  });

  it("streamActive is true when complete AND a pendingUser bubble is still outstanding", () => {
    const result = deriveMessageList({
      ...baseArgs(),
      sseState: mkSSEState({ status: "complete" }),
      pendingUser: { text: "hi", baseline: 0 },
    });
    expect(result.streamActive).toBe(true);
  });

  it("streamActive is false when complete AND no pendingUser bubble is outstanding", () => {
    const result = deriveMessageList({
      ...baseArgs(),
      sseState: mkSSEState({ status: "complete" }),
      pendingUser: null,
    });
    expect(result.streamActive).toBe(false);
  });

  // 5. streamingKey = messageId | "streaming".
  it("streamingKey is the numeric messageId when set", () => {
    const result = deriveMessageList({ ...baseArgs(), sseState: mkSSEState({ status: "streaming", messageId: 42 }) });
    expect(result.streamingKey).toBe(42);
  });

  it('streamingKey falls back to the literal "streaming" when messageId is null', () => {
    const result = deriveMessageList({ ...baseArgs(), sseState: mkSSEState({ status: "streaming", messageId: null }) });
    expect(result.streamingKey).toBe("streaming");
  });

  // 6. persistedHasSameKey → streamingMessages empty.
  it("persistedHasSameKey is true when a raw server row shares the streamingKey, suppressing the duplicate streaming row", () => {
    const raw: MessageRecord[] = [mkMessage({ id: 50, role: "assistant", content: "already persisted" })];
    const result = deriveMessageList({
      ...baseArgs(),
      serverMessagesRaw: raw,
      sseState: mkSSEState({ status: "streaming", messageId: 50, contentDeltas: ["Hello"] }),
    });

    expect(result.streamingKey).toBe(50);
    expect(result.persistedHasSameKey).toBe(true);
    expect(result.streamingMessages).toEqual([]);
  });

  it("persistedHasSameKey is false (and streamingMessages non-empty) when no raw row shares the streamingKey", () => {
    const raw: MessageRecord[] = [mkMessage({ id: 1, role: "user", content: "hi" })];
    const result = deriveMessageList({
      ...baseArgs(),
      serverMessagesRaw: raw,
      sseState: mkSSEState({ status: "streaming", messageId: 50, contentDeltas: ["Hello"] }),
    });

    expect(result.persistedHasSameKey).toBe(false);
    expect(result.streamingMessages).toHaveLength(1);
    expect(result.streamingMessages[0]?.id).toBe(50);
  });

  // 7. displayStreamContent strips at the FIRST "<!--followups" occurrence,
  //    regardless of whether the closing "-->" has arrived yet.
  it("strips the display content at an UNCLOSED followups marker", () => {
    const result = deriveMessageList({
      ...baseArgs(),
      sseState: mkSSEState({ contentDeltas: ["Hello world ", '<!--followups:["q1"'] }),
    });
    expect(result.displayStreamContent).toBe("Hello world");
  });

  it("strips the display content at a CLOSED followups marker (same truncation point)", () => {
    const result = deriveMessageList({
      ...baseArgs(),
      sseState: mkSSEState({ contentDeltas: ["Hello world ", '<!--followups:["q1","q2"]-->'] }),
    });
    expect(result.displayStreamContent).toBe("Hello world");
  });

  it("leaves the display content untouched when no followups marker is present", () => {
    const result = deriveMessageList({
      ...baseArgs(),
      sseState: mkSSEState({ contentDeltas: ["Hello ", "world"] }),
    });
    expect(result.displayStreamContent).toBe("Hello world");
  });

  // 8. allMessages = server + optimistic + streaming, in that order.
  it("allMessages concatenates server, optimistic, and streaming messages in order", () => {
    const raw: MessageRecord[] = [mkMessage({ id: 1, role: "user", content: "persisted" })];
    const result = deriveMessageList({
      serverMessagesRaw: raw,
      finalStats: null,
      pendingUser: { text: "pending bubble", baseline: 1 },
      sseState: mkSSEState({ status: "streaming", messageId: 99, contentDeltas: ["live"] }),
      chatId: 1,
    });

    expect(result.allMessages.map((m) => m.id)).toEqual([1, "pending-user", 99]);
  });

  // Bonus: confirms the two `finalStatsRef.current` reads collapsed onto the
  // single `finalStats` param read the SAME value within one derivation.
  it("uses the single finalStats param for both the last-server-row stats AND the post-stream streamingMessages fallback", () => {
    const finalStats = mkStats({ outputTokens: 123, tokensPerSecond: 45 });
    const liveStats = mkStats({ outputTokens: 1 }); // distinct from finalStats — proves which one wins

    const raw: MessageRecord[] = [mkMessage({ id: 1, role: "assistant", content: "done streaming" })];
    const result = deriveMessageList({
      serverMessagesRaw: raw,
      finalStats,
      pendingUser: { text: "still pending", baseline: 1 }, // keeps streamActive true post-"complete"
      sseState: mkSSEState({ status: "complete", messageId: 999, contentDeltas: ["done"], stats: liveStats }),
      chatId: 1,
    });

    // Last assistant server row picks up finalStats (idx === lastIdx && role === "assistant").
    expect(result.serverMessages[0]?.stats).toEqual(finalStats);
    // Non-streaming streamingMessages row falls back to finalStats, not the live sseState.stats.
    expect(result.streamingMessages[0]?.stats).toEqual(finalStats);
  });
});
