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

describe("SubSessionPanel — state-machine transitions", () => {
  it("idle: shows the welcome copy and the Summarize CTA", () => {
    render(
      createElement(SubSessionPanel, {
        subSession: makeSession(),
        sseState: makeState(),
        onFinalize: () => undefined,
        onInject: () => undefined,
        onCancel: () => undefined,
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
      }),
    );

    fireEvent.click(screen.getByRole("button", { name: /Cancel sub-session/i }));
    expect(onCancel).toHaveBeenCalledTimes(1);
  });
});
