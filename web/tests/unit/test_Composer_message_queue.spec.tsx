/**
 * Message queue (streaming submit) — the composer's single submit choke
 * point (handleSubmit, hooked from both the Send/Queue button and Enter/
 * Cmd+Enter via handleKeyDown) now enqueues instead of dropping a message
 * submitted while `streaming` is true.
 *
 * Covers:
 *  1. The textarea stays typable while streaming (not `disabled`).
 *  2. Submitting while streaming enqueues — onSubmit is NOT called yet, and
 *     a visible queued-message indicator appears; the draft is cleared.
 *  3. When the CURRENT stream finishes naturally (streaming: true → false),
 *     the queued message auto-sends exactly once.
 *  4. Abort behavior (documented decision): if the stream ends because the
 *     user clicked Stop, the queued message is NOT auto-fired into the
 *     interrupted conversation — it stays queued, discoverable via the
 *     queue row's "Send now" (manual flush) and remove controls.
 *  5. Cross-chat scoping: this Composer instance is not remounted on chat
 *     navigation, so a queued message must stay scoped to the chat it was
 *     submitted from — invisible and non-draining while a different chat
 *     is on screen, and only auto-sent (to the RIGHT chat) once its own
 *     chat is idle and back on screen.
 */
import type { ComponentProps } from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Composer } from "@/components/Composer";

// Real onSubmit signature (not hand-duplicated) — a bare vi.fn() types
// .mock.calls[0] ambiguously, which fails destructuring it under
// noUncheckedIndexedAccess.
type OnSubmitFn = ComponentProps<typeof Composer>["onSubmit"];

// ─── Mocks (same set as test_Composer_integrations_picker.spec.tsx) ─────────

vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: vi.fn() }),
}));

vi.mock("@/hooks/useSTT", () => ({
  useSTT: () => ({
    capability: { available: false, engine: null },
    state: { listening: false, error: null },
    start: vi.fn(),
    stop: vi.fn(),
  }),
  detectSTT: () => ({ available: false, engine: null }),
}));

vi.mock("@/components/MicButton", () => ({
  MicButton: () => null,
}));

vi.mock("@/components/InProjectChip", () => ({
  InProjectChip: () => null,
}));

vi.mock("@/components/RagModeBadge", () => ({
  RagModeBadge: () => null,
}));

vi.mock("@/components/SlashMenu", () => ({
  SlashMenu: () => null,
  parseSlashCommand: () => null,
  BUILTIN_COMMANDS: [],
}));

vi.mock("@/hooks/usePrompts", () => ({
  usePrompts: () => ({ data: [], isLoading: false, isError: false }),
}));

vi.mock("@/hooks/useIntegrationsList", () => ({
  useIntegrationsList: () => ({ data: [], isLoading: false, isError: false }),
  useUpdateIntegrationsList: () => ({ mutate: vi.fn(), isPending: false }),
}));

vi.mock("@/hooks/useChatPreset", () => ({
  useChatPreset: () => ({
    activePreset: "",
    preset: null,
    setPreset: vi.fn(),
    clearPreset: vi.fn(),
  }),
  useHydrateChatPresets: () => undefined,
  useChatPresetStore: vi.fn(),
}));

vi.mock("@/hooks/useLmStudioConfig", () => ({
  useLmStudioConfig: () => ({
    data: undefined,
    isLoading: false,
    isError: false,
  }),
  lmStudioConfigKeys: { resolved: () => ["lmstudio-config", "resolved"] },
}));

const mockModelsData = {
  models: [
    {
      id: "test-model",
      name: "Test Model",
      loaded: true,
      loaded_instance_ids: ["test-model"],
      capabilities: {
        vision: false,
        trained_for_tool_use: false,
        reasoning: null,
        embedding: false,
      },
      max_context_length: 8192,
      size_bytes: 0,
      params_string: "",
    },
  ],
};

vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({ data: mockModelsData, isLoading: false, isError: false, refetch: vi.fn() }),
}));

// ─── Tests ────────────────────────────────────────────────────────────────

describe("Composer message queue (streaming submit)", () => {
  const baseProps = {
    chatId: 1,
    onSubmit: vi.fn(),
    onStop: vi.fn(),
    onClear: vi.fn(),
    onFork: vi.fn(),
    onCompact: vi.fn(),
    onMemoryPin: vi.fn(),
    modelId: "test-model",
  };

  it("keeps the textarea typable while streaming", () => {
    render(<Composer {...baseProps} streaming onSubmit={vi.fn()} />);
    const textarea = screen.getByLabelText("Message") as HTMLTextAreaElement;
    expect(textarea.disabled).toBe(false);

    fireEvent.change(textarea, { target: { value: "still typing mid-stream" } });
    expect(textarea.value).toBe("still typing mid-stream");
  });

  it("enqueues on submit while streaming instead of calling onSubmit, and shows a queued indicator", () => {
    const onSubmit = vi.fn<OnSubmitFn>();
    render(<Composer {...baseProps} streaming onSubmit={onSubmit} />);
    const textarea = screen.getByLabelText("Message") as HTMLTextAreaElement;

    fireEvent.change(textarea, { target: { value: "queued message" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    // The send handler must NOT fire yet — the message is only queued.
    expect(onSubmit).not.toHaveBeenCalled();

    // Draft is cleared, same as a normal send.
    expect(textarea.value).toBe("");

    // A visible pending/queued indicator appears with the message text.
    expect(screen.getByTestId("composer-queue")).toBeTruthy();
    const item = screen.getByTestId("composer-queue-item");
    expect(item.textContent).toContain("queued message");
  });

  it("supports at least one queued message via a small FIFO queue (two messages queue in order)", () => {
    const onSubmit = vi.fn<OnSubmitFn>();
    render(<Composer {...baseProps} streaming onSubmit={onSubmit} />);
    const textarea = screen.getByLabelText("Message") as HTMLTextAreaElement;

    fireEvent.change(textarea, { target: { value: "first" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    fireEvent.change(textarea, { target: { value: "second" } });
    fireEvent.keyDown(textarea, { key: "Enter" });

    expect(onSubmit).not.toHaveBeenCalled();
    const items = screen.getAllByTestId("composer-queue-item");
    expect(items).toHaveLength(2);
    expect(items[0]?.textContent).toContain("first");
    expect(items[1]?.textContent).toContain("second");
  });

  it("auto-sends the queued message exactly once when the current stream finishes naturally", () => {
    const onSubmit = vi.fn<OnSubmitFn>();
    const { rerender } = render(
      <Composer {...baseProps} streaming onSubmit={onSubmit} />,
    );
    const textarea = screen.getByLabelText("Message") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "queued message" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();

    // The stream completes naturally: streaming flips true -> false.
    rerender(<Composer {...baseProps} streaming={false} onSubmit={onSubmit} />);

    expect(onSubmit).toHaveBeenCalledTimes(1);
    const onSubmitCall1 = onSubmit.mock.calls[0];
    if (onSubmitCall1 === undefined) throw new Error("expected onSubmit to have been called");
    const [sentChatId, sentPayload, sentUserText] = onSubmitCall1;
    expect(sentChatId).toBe(1);
    expect(sentUserText).toBe("queued message");
    expect(sentPayload.input).toEqual([{ type: "text", content: "queued message" }]);

    // The queue is now empty — re-rendering again must not resend.
    expect(screen.queryByTestId("composer-queue")).toBeNull();
    rerender(<Composer {...baseProps} streaming={false} onSubmit={onSubmit} />);
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("does NOT auto-fire a queued message when the stream ends via Stop (abort) — it stays queued", () => {
    const onSubmit = vi.fn<OnSubmitFn>();
    const onStop = vi.fn();
    const { rerender } = render(
      <Composer {...baseProps} streaming onSubmit={onSubmit} onStop={onStop} />,
    );
    const textarea = screen.getByLabelText("Message") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "queued message" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();

    // User clicks Stop.
    fireEvent.click(screen.getByLabelText("Stop generation"));
    expect(onStop).toHaveBeenCalledTimes(1);

    // The parent tears the stream down: streaming -> false.
    rerender(
      <Composer {...baseProps} streaming={false} onSubmit={onSubmit} onStop={onStop} />,
    );

    // No auto-send into the aborted state.
    expect(onSubmit).not.toHaveBeenCalled();
    // The draft is preserved, not discarded.
    const item = screen.getByTestId("composer-queue-item");
    expect(item.textContent).toContain("queued message");
  });

  it("lets the user manually flush a stranded (post-abort) queued message via Send now", () => {
    const onSubmit = vi.fn<OnSubmitFn>();
    const onStop = vi.fn();
    const { rerender } = render(
      <Composer {...baseProps} streaming onSubmit={onSubmit} onStop={onStop} />,
    );
    const textarea = screen.getByLabelText("Message") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "stranded message" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    fireEvent.click(screen.getByLabelText("Stop generation"));
    rerender(
      <Composer {...baseProps} streaming={false} onSubmit={onSubmit} onStop={onStop} />,
    );
    expect(onSubmit).not.toHaveBeenCalled();

    // "Send now" only appears once idle — flushes the head item manually.
    fireEvent.click(screen.getByTestId("composer-queue-send-now"));

    expect(onSubmit).toHaveBeenCalledTimes(1);
    const onSubmitCall2 = onSubmit.mock.calls[0];
    if (onSubmitCall2 === undefined) throw new Error("expected onSubmit to have been called");
    const [, , sentUserText] = onSubmitCall2;
    expect(sentUserText).toBe("stranded message");
    expect(screen.queryByTestId("composer-queue")).toBeNull();
  });

  it("does NOT drain a queued message into a different chat navigated to before its stream finishes, and drains it once the origin chat is back on screen", () => {
    const onSubmit = vi.fn<OnSubmitFn>();
    const { rerender } = render(
      <Composer {...baseProps} chatId={1} streaming onSubmit={onSubmit} />,
    );
    const textarea = screen.getByLabelText("Message") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "queued in chat 1" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(onSubmit).not.toHaveBeenCalled();

    // The user navigates to a different chat while chat 1's stream is
    // still running in the background — Chat.tsx renders <Composer> with
    // no `key={chatId}`, so this is a prop change on the SAME instance,
    // not a remount. The chat-1 item must not be visible from chat 2.
    rerender(<Composer {...baseProps} chatId={2} streaming onSubmit={onSubmit} />);
    expect(screen.queryByTestId("composer-queue")).toBeNull();

    // Chat 1's stream completes naturally in the background while chat 2
    // is still the one on screen.
    rerender(<Composer {...baseProps} chatId={2} streaming={false} onSubmit={onSubmit} />);

    // Must NOT fire into chat 2 — this is the wrong-chat leak.
    expect(onSubmit).not.toHaveBeenCalled();
    expect(screen.queryByTestId("composer-queue")).toBeNull();

    // The user navigates back to chat 1 (streaming already settled false
    // in the background) — the queued item drains there, correctly, and
    // becomes visible again while it's still pending.
    rerender(<Composer {...baseProps} chatId={1} streaming={false} onSubmit={onSubmit} />);

    expect(onSubmit).toHaveBeenCalledTimes(1);
    const onSubmitCall3 = onSubmit.mock.calls[0];
    if (onSubmitCall3 === undefined) throw new Error("expected onSubmit to have been called");
    const [sentChatId, , sentUserText] = onSubmitCall3;
    expect(sentChatId).toBe(1);
    expect(sentUserText).toBe("queued in chat 1");
  });

  it("removes a queued message without sending it", () => {
    const onSubmit = vi.fn<OnSubmitFn>();
    render(<Composer {...baseProps} streaming onSubmit={onSubmit} />);
    const textarea = screen.getByLabelText("Message") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "will be removed" } });
    fireEvent.keyDown(textarea, { key: "Enter" });
    expect(screen.getByTestId("composer-queue-item")).toBeTruthy();

    fireEvent.click(screen.getByTestId("composer-queue-remove"));

    expect(screen.queryByTestId("composer-queue")).toBeNull();
    // Rerendering to idle must not send a removed item.
    expect(onSubmit).not.toHaveBeenCalled();
  });
});
