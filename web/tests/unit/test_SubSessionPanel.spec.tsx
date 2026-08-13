/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for the SubSessionPanel component (Chat.tsx).
 *
 * Covers the panel's state-machine transitions:
 *   idle → streaming → finalized → inject-pending → closed
 *
 * The panel is a pure render of its props; the surrounding state is driven
 * by `useSubSessionSSE` in production. These tests pass synthetic prop
 * combinations so each branch of the panel renders predictably.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { createElement } from "react";

// ChatMessage pulls in markdown/code-fence rendering — heavy and irrelevant
// here. Stub it down to a leaf that surfaces role + content as text.
vi.mock("@/components/ChatMessage", () => ({
  ChatMessage: ({ message }: { message: { id: string; role: string; content: string } }) =>
    createElement(
      "div",
      { "data-testid": `mock-chatmessage-${message.id}`, "data-role": message.role },
      message.content,
    ),
}));

import { SubSessionPanel } from "@/components/chat/SubSessionPanel";
import type { SubSessionSSEState } from "@/hooks/useSubSessionSSE";
import type { SubSessionSummaryDto } from "@/lib/subSession";

type SubSession = {
  presetLabel: string;
  messages: Array<{ role: "user" | "assistant"; content: string }>;
  finalizing: boolean;
  finalContent: string | null;
};

function makeSession(over: Partial<SubSession> = {}): SubSession {
  return {
    presetLabel: "Coder",
    messages: [],
    finalizing: false,
    finalContent: null,
    ...over,
  };
}

function makeState(over: Partial<SubSessionSSEState> = {}): SubSessionSSEState {
  return { status: "idle", content: "", error: null, toolCalls: [], ...over };
}

// P4 — history + reopen props every render now needs; defaults matching
// "nothing fetched yet, browse view closed" so the existing state-machine
// tests above (written pre-P4) keep exercising the live/reopened branch
// unchanged.
const historyProps = {
  history: null as SubSessionSummaryDto[] | null,
  historyLoading: false,
  isHistoryOpen: false,
  onOpenHistory: () => undefined,
  onCloseHistory: () => undefined,
  onReopen: () => undefined,
};

function makeHistoryEntry(
  over: Partial<SubSessionSummaryDto> = {},
): SubSessionSummaryDto {
  return {
    id: 9,
    chat_id: 42,
    preset_id: "research",
    title: "What changed in the API?",
    status: "final",
    model_id: "qwen3.6",
    created_at: "2026-08-10T10:00:00Z",
    updated_at: "2026-08-10T10:05:00Z",
    ...over,
  };
}

describe("SubSessionPanel — state-machine transitions", () => {
  it("idle: shows the welcome copy and the Summarize CTA", () => {
    render(
      createElement(SubSessionPanel, {
        subSession: makeSession(),
        sseState: makeState(),
        onFinalize: () => undefined,
        onInject: () => undefined,
        onCancel: () => undefined,
        ...historyProps,
      }),
    );

    // "Coder mode" appears in the control-bar label AND in the empty-state
    // welcome copy — both render at idle, so this assertion must allow >=1.
    expect(screen.getAllByText(/Coder mode/i).length).toBeGreaterThan(0);
    expect(screen.getByText(/clean session/i)).toBeTruthy();
    expect(screen.getByRole("button", { name: /Summarize/ })).toBeTruthy();
  });

  it("streaming: replaces the CTA with the Thinking copy and renders the live delta", () => {
    render(
      createElement(SubSessionPanel, {
        subSession: makeSession({
          messages: [{ role: "user", content: "research X" }],
        }),
        sseState: makeState({ status: "streaming", content: "intermediate..." }),
        onFinalize: () => undefined,
        onInject: () => undefined,
        onCancel: () => undefined,
        ...historyProps,
      }),
    );

    // Thinking copy replaces the CTA while streaming.
    expect(screen.getByText(/Thinking/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Summarize/ })).toBeNull();
    // Streaming bubble surfaces the in-flight content.
    const streamingBubble = screen.getByTestId("mock-chatmessage-sub-streaming");
    expect(streamingBubble.textContent).toContain("intermediate...");
  });

  it("finalizing: shows the Generating-summary copy in place of the CTA", () => {
    render(
      createElement(SubSessionPanel, {
        subSession: makeSession({ finalizing: true }),
        sseState: makeState({ status: "complete", content: "" }),
        onFinalize: () => undefined,
        onInject: () => undefined,
        onCancel: () => undefined,
        ...historyProps,
      }),
    );

    expect(screen.getByText(/Generating summary/)).toBeTruthy();
  });

  it("finalized: surfaces the summary preview + Add-to-main-chat CTA", () => {
    render(
      createElement(SubSessionPanel, {
        subSession: makeSession({
          finalContent: "summary body",
          finalizing: false,
        }),
        sseState: makeState({ status: "complete" }),
        onFinalize: () => undefined,
        onInject: () => undefined,
        onCancel: () => undefined,
        ...historyProps,
      }),
    );

    expect(screen.getByText(/Summary ready/i)).toBeTruthy();
    const finalBubble = screen.getByTestId("mock-chatmessage-sub-final");
    expect(finalBubble.textContent).toContain("summary body");
    expect(screen.getByRole("button", { name: /Add to main chat/ })).toBeTruthy();
  });

  it("inject-pending: clicking Add-to-main-chat fires onInject", () => {
    const onInject = vi.fn();
    render(
      createElement(SubSessionPanel, {
        subSession: makeSession({ finalContent: "ok" }),
        sseState: makeState({ status: "complete" }),
        onFinalize: () => undefined,
        onInject,
        onCancel: () => undefined,
        ...historyProps,
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: /Add to main chat/ }));
    expect(onInject).toHaveBeenCalledTimes(1);
  });

  it("idle: clicking Summarize fires onFinalize", () => {
    const onFinalize = vi.fn();
    render(
      createElement(SubSessionPanel, {
        subSession: makeSession({
          messages: [{ role: "user", content: "ok" }],
        }),
        sseState: makeState(),
        onFinalize,
        onInject: () => undefined,
        onCancel: () => undefined,
        ...historyProps,
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: /Summarize/ }));
    expect(onFinalize).toHaveBeenCalledTimes(1);
  });

  it("cancel button is always present and fires onCancel", () => {
    const onCancel = vi.fn();
    render(
      createElement(SubSessionPanel, {
        subSession: makeSession(),
        sseState: makeState(),
        onFinalize: () => undefined,
        onInject: () => undefined,
        onCancel,
        ...historyProps,
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: /Cancel sub-session/i }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it("live session: the History button fires onOpenHistory", () => {
    const onOpenHistory = vi.fn();
    render(
      createElement(SubSessionPanel, {
        subSession: makeSession(),
        sseState: makeState(),
        onFinalize: () => undefined,
        onInject: () => undefined,
        onCancel: () => undefined,
        ...historyProps,
        onOpenHistory,
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: /Sub-session history/i }));
    expect(onOpenHistory).toHaveBeenCalledTimes(1);
  });
});

describe("SubSessionPanel — P4 history browse view", () => {
  it("isHistoryOpen: lists past sessions and reopens the clicked one", () => {
    const onReopen = vi.fn();
    render(
      createElement(SubSessionPanel, {
        subSession: makeSession(),
        sseState: makeState(),
        onFinalize: () => undefined,
        onInject: () => undefined,
        onCancel: () => undefined,
        ...historyProps,
        isHistoryOpen: true,
        history: [
          makeHistoryEntry({ id: 9, title: "What changed in the API?", status: "final" }),
          makeHistoryEntry({ id: 8, preset_id: "coder", title: null, status: "aborted" }),
        ],
        onReopen,
      }),
    );

    expect(screen.getByText("Sub-session history")).toBeTruthy();
    expect(screen.getByText("What changed in the API?")).toBeTruthy();
    // A null title falls back to the mode label — appears twice (the
    // entry's title AND its mode-label caption both read "Coder").
    expect(screen.getAllByText("Coder").length).toBe(2);
    expect(screen.getByRole("list", { name: /Past sub-sessions/i })).toBeTruthy();

    fireEvent.click(screen.getByText("What changed in the API?"));
    expect(onReopen).toHaveBeenCalledWith(9);
  });

  it("isHistoryOpen with no entries yet: shows an empty-state hint, not a crash", () => {
    render(
      createElement(SubSessionPanel, {
        subSession: null,
        sseState: makeState(),
        onFinalize: () => undefined,
        onInject: () => undefined,
        onCancel: () => undefined,
        ...historyProps,
        isHistoryOpen: true,
        history: [],
      }),
    );

    expect(screen.getByText(/No past sessions in this chat yet/i)).toBeTruthy();
  });

  it("isHistoryOpen while loading (history still null): shows a loading hint", () => {
    render(
      createElement(SubSessionPanel, {
        subSession: null,
        sseState: makeState(),
        onFinalize: () => undefined,
        onInject: () => undefined,
        onCancel: () => undefined,
        ...historyProps,
        isHistoryOpen: true,
        historyLoading: true,
        history: null,
      }),
    );

    expect(screen.getByText(/Loading past sessions/i)).toBeTruthy();
  });

  it("closing history fires onCloseHistory", () => {
    const onCloseHistory = vi.fn();
    render(
      createElement(SubSessionPanel, {
        subSession: null,
        sseState: makeState(),
        onFinalize: () => undefined,
        onInject: () => undefined,
        onCancel: () => undefined,
        ...historyProps,
        isHistoryOpen: true,
        history: [],
        onCloseHistory,
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: /Close sub-session history/i }));
    expect(onCloseHistory).toHaveBeenCalledTimes(1);
  });

  it("no session and history closed: renders nothing (defensive null case)", () => {
    const { container } = render(
      createElement(SubSessionPanel, {
        subSession: null,
        sseState: makeState(),
        onFinalize: () => undefined,
        onInject: () => undefined,
        onCancel: () => undefined,
        ...historyProps,
      }),
    );

    expect(container.textContent).toBe("");
  });
});
