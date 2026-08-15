/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useStoppedStreamReconciliation — backoff-poll unit tests
 * (FE-STOPPED fix, 2026-07-17).
 *
 * The stop-button reconciliation effect used to wait a fixed 600ms then
 * refetch the persisted row exactly once. There is no open SSE channel after
 * stop (useSSE's stop() aborts the connection), so if the BE's flush lags
 * past 600ms the patch would be compared against a not-yet-durable row.
 *
 * The fix replaces the single 600ms setTimeout with a backoff poll
 * (POLL_DELAYS = [200, 350, 600, 1000, 1500], ~3.65s total across 5
 * attempts) that keeps refetching until resolveStoppedPartial reports the
 * row as `durable` (caught up to the in-memory partial) or the attempts are
 * exhausted, then resolves exactly once (patch the cache if a patch is
 * available, always call resetStream()).
 *
 * RED-ON-REVERT: reverting to the fixed single-shot 600ms setTimeout fails
 * "keeps polling while the row is short..." (the fixed version never issues
 * a 2nd/3rd refetch — it patches/resets after exactly one refetch at
 * 600ms, before the row above ever catches up) and "gives up after the
 * capped attempts..." (the fixed version calls refetchMessages exactly
 * once, not five times).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { QueryClient } from "@tanstack/react-query";
import {
  useStoppedStreamReconciliation,
  type UseStoppedStreamReconciliationArgs,
} from "@/hooks/useStoppedStreamReconciliation";
import type { MessageListResponse, MessageRecord } from "@/hooks/useChats";
import type { StreamState } from "@/hooks/useSSE";

// The hook imports `chatKeys` from "@/hooks/useChats" directly (not
// injected) — mock the module so the query-cache key used by the hook is
// predictable in assertions, without pulling in the real (heavier) module.
vi.mock("@/hooks/useChats", () => ({
  chatKeys: { messages: (id: number) => ["messages", id] as const },
}));

function idleStreamState(overrides: Partial<StreamState>): StreamState {
  return {
    status: "idle",
    chatId: null,
    messageId: null,
    responseId: null,
    contentDeltas: [],
    reasoningDeltas: [],
    toolCalls: [],
    error: null,
    stats: { tokensPerSecond: null, ttftSeconds: null, outputTokens: 0 },
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

function messagesResponse(
  partials: { id: number; content: string }[],
): MessageListResponse {
  // Only id/content vary across this file's cases — pad the rest with
  // sensible assistant-row defaults rather than narrowing MessageRecord.
  const messages: MessageRecord[] = partials.map((p) => ({
    ...p,
    chat_id: 1,
    role: "assistant",
    reasoning_content: null,
    created_at: "2026-07-01T00:00:00Z",
  }));
  return { messages, total: messages.length, has_more: false, oldest_id: null };
}

const MSG_KEY = ["messages", 1];

describe("useStoppedStreamReconciliation — backoff poll", () => {
  let qc: QueryClient;
  // Typed generic (not bare `ReturnType<typeof vi.fn>`) so the mock's own
  // callable signature actually matches UseStoppedStreamReconciliationArgs
  // — an untyped vi.fn() is both callable and constructable, which doesn't
  // satisfy the plain `() => void` the hook's args expect.
  let resetStream: ReturnType<typeof vi.fn<() => void>>;
  let refetchMessages: ReturnType<typeof vi.fn>;

  const stoppedState = idleStreamState({
    status: "stopped",
    messageId: 42,
    contentDeltas: ["Hello ", "world (full partial)"],
  });

  beforeEach(() => {
    vi.useFakeTimers();
    qc = new QueryClient();
    resetStream = vi.fn<() => void>();
    refetchMessages = vi.fn();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  // Mount-time artifact (shared with the existing chat-switch
  // characterization test, test_Chat_chatswitch_ephemeral_wipe.spec.tsx): the
  // SECOND effect's `prevChatIdForStopRef` starts at `null`, so when
  // sseState.status is already "stopped" at mount, that effect's
  // `null !== chatId` check also fires and calls resetStream() once,
  // independent of the backoff-poll effect under test here. Clear that call
  // immediately after render so assertions below isolate the poll's own
  // resetStream() call.
  function renderReconciliation(chatId: number | null, sseState: StreamState) {
    const args: UseStoppedStreamReconciliationArgs = {
      chatId,
      sseState,
      refetchMessages: refetchMessages as unknown as UseStoppedStreamReconciliationArgs["refetchMessages"],
      resetStream,
      qc,
    };
    const rendered = renderHook((props: UseStoppedStreamReconciliationArgs) =>
      useStoppedStreamReconciliation(props), { initialProps: args });
    resetStream.mockClear();
    return rendered;
  }

  it("keeps polling while the row is short, then patches+resets once caught up (durable)", async () => {
    refetchMessages
      .mockResolvedValueOnce({ data: messagesResponse([{ id: 42, content: "Hello" }]) }) // attempt 0 @200ms — short
      .mockResolvedValueOnce({ data: messagesResponse([{ id: 42, content: "Hello wor" }]) }) // attempt 1 @+350ms — still short
      .mockResolvedValueOnce({
        data: messagesResponse([{ id: 42, content: "Hello world (full partial)" }]),
      }); // attempt 2 @+600ms — caught up (durable)

    renderReconciliation(1, stoppedState);

    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    expect(refetchMessages).toHaveBeenCalledTimes(1);
    expect(resetStream).not.toHaveBeenCalled();

    await act(async () => { await vi.advanceTimersByTimeAsync(350); });
    expect(refetchMessages).toHaveBeenCalledTimes(2);
    expect(resetStream).not.toHaveBeenCalled();

    await act(async () => { await vi.advanceTimersByTimeAsync(600); });
    expect(refetchMessages).toHaveBeenCalledTimes(3);
    expect(resetStream).toHaveBeenCalledTimes(1);
    // Durable → no patch needed.
    expect(qc.getQueryData(MSG_KEY)).toBeUndefined();

    // No further polls scheduled after the row is durable.
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(refetchMessages).toHaveBeenCalledTimes(3);
  });

  it("gives up after the capped attempts and still patches with the in-memory partial", async () => {
    qc.setQueryData<MessageListResponse>(MSG_KEY, messagesResponse([{ id: 42, content: "" }]));
    // Every refetch returns a row that never catches up.
    refetchMessages.mockResolvedValue({ data: messagesResponse([{ id: 42, content: "Hello" }]) });

    renderReconciliation(1, stoppedState);

    // 5 attempts span 200+350+600+1000+1500 = 3650ms.
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });

    expect(refetchMessages).toHaveBeenCalledTimes(5);
    expect(resetStream).toHaveBeenCalledTimes(1);

    const patched = qc.getQueryData<MessageListResponse>(MSG_KEY);
    expect(patched?.messages.find((m) => m.id === 42)?.content).toBe(
      "Hello world (full partial)",
    );
  });

  it("resetStream is called exactly once across the whole capped poll", async () => {
    refetchMessages.mockResolvedValue({ data: messagesResponse([{ id: 42, content: "Hello" }]) });
    renderReconciliation(1, stoppedState);
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    expect(resetStream).toHaveBeenCalledTimes(1);
  });

  it("cleanup cancels pending polls — no action after unmount", async () => {
    refetchMessages.mockResolvedValue({ data: messagesResponse([{ id: 42, content: "Hello" }]) });
    const { unmount } = renderReconciliation(1, stoppedState);

    // Let the first attempt fire and resolve, then unmount before the next
    // scheduled poll would fire.
    await act(async () => { await vi.advanceTimersByTimeAsync(200); });
    expect(refetchMessages).toHaveBeenCalledTimes(1);

    unmount();

    // Advance well past all remaining attempts — nothing further happens.
    await act(async () => { await vi.advanceTimersByTimeAsync(5000); });
    expect(refetchMessages).toHaveBeenCalledTimes(1);
    expect(resetStream).not.toHaveBeenCalled();
    expect(qc.getQueryData(MSG_KEY)).toBeUndefined();
  });

  it("happy path: already durable on first poll → single refetch + resetStream, no patch", async () => {
    refetchMessages.mockResolvedValueOnce({
      data: messagesResponse([{ id: 42, content: "Hello world (full partial)" }]),
    });
    renderReconciliation(1, stoppedState);

    await act(async () => { await vi.advanceTimersByTimeAsync(200); });

    expect(refetchMessages).toHaveBeenCalledTimes(1);
    expect(resetStream).toHaveBeenCalledTimes(1);
    expect(qc.getQueryData(MSG_KEY)).toBeUndefined();
  });
});
