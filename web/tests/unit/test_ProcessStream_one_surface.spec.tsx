/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests — ProcessStream: ONE calm process surface per assistant turn.
 *
 * Proves the unification target (replacing the former ThinkingIndicator +
 * ThinkingBlock + ToolCallCard chain):
 *
 *   - The thinking surface and the answer are ONE surface. During the
 *     pre-token window there is EXACTLY ONE thinking affordance (one
 *     `thinking-indicator`) and EXACTLY ONE cursor — never a doubled
 *     indicator (the old bug: standalone ThinkingIndicator + ThinkingBlock's
 *     pulsing dot showing at once).
 *   - When the answer starts, the reasoning auto-collapses to a single
 *     "Reasoning" line (click to re-expand, persisted per message) and the
 *     ProcessStream stops rendering its own cursor (no double caret — the
 *     answer bubble owns the end-of-answer caret).
 *   - Tool calls render as quiet inline lines, not layout-shifting cards.
 *   - State machine: pre-token / reasoning-streaming / answer-streaming /
 *     tool-call / done.
 *
 * These render the REAL ChatMessage (which delegates to ProcessStream) so the
 * test exercises the wired path, not a component in isolation.
 */
import { describe, it, expect, beforeEach } from "vitest";
import { render, screen, fireEvent, within } from "@testing-library/react";
import { ChatMessage } from "@/components/ChatMessage";
import { ProcessStream } from "@/components/ProcessStream";
import type { ToolCall } from "@/hooks/useSSE";

beforeEach(() => {
  // Reasoning expand-state is persisted in sessionStorage; reset between tests.
  try {
    sessionStorage.clear();
  } catch {
    /* ignore */
  }
});

describe("ProcessStream — one surface, no double indicator", () => {
  it("(a) pre-token thinking: ONE thinking-indicator + ONE cursor, no answer bubble", () => {
    const { container } = render(
      <ChatMessage
        message={{
          id: "streaming",
          role: "assistant",
          content: "",
          reasoning_content: null,
          streaming: true,
        }}
        streamingActive={true}
      />,
    );

    // Exactly one thinking affordance — never two (the old indicator+block bug).
    expect(screen.getAllByTestId("thinking-indicator")).toHaveLength(1);
    // Exactly one cursor across the whole turn.
    expect(container.querySelectorAll(".lmchat-stream-caret")).toHaveLength(1);
    // The ProcessStream owns it during the pre-token window.
    expect(screen.getAllByTestId("process-stream-caret")).toHaveLength(1);
    // No answer bubble yet.
    expect(container.querySelector(".lmchat-bubble-assistant")).toBeNull();
    // The surface marks itself live.
    expect(
      screen.getByTestId("process-stream").getAttribute("data-live"),
    ).toBe("true");
  });

  it("(b) reasoning streaming: reasoning shown live + ONE cursor beneath, still no answer", () => {
    const { container } = render(
      <ChatMessage
        message={{
          id: "streaming",
          role: "assistant",
          content: "",
          reasoning_content: "Let me work through this step by step.",
          streaming: true,
        }}
        streamingActive={true}
      />,
    );

    // Live reasoning text is visible (uncollapsed) while thinking.
    expect(
      screen.getByText(/Let me work through this step by step\./),
    ).toBeTruthy();
    // Still exactly one cursor — beneath the reasoning.
    expect(container.querySelectorAll(".lmchat-stream-caret")).toHaveLength(1);
    expect(screen.getAllByTestId("process-stream-caret")).toHaveLength(1);
    // No collapsible "Reasoning" toggle yet — it's live, not collapsed.
    expect(
      screen.queryByRole("button", { name: /Reasoning/i }),
    ).toBeNull();
    // Once reasoning is arriving, the standalone phase line is gone (no dot row).
    expect(screen.queryByTestId("thinking-indicator")).toBeNull();
  });

  it("(c) answer streaming: reasoning collapses to a 'Reasoning' line; NO double cursor", () => {
    const { container } = render(
      <ChatMessage
        message={{
          id: "streaming",
          role: "assistant",
          content: "The answer is 42.",
          reasoning_content: "Thinking about the meaning of life.",
          streaming: true,
        }}
        streamingActive={true}
      />,
    );

    // Reasoning is now a collapsed toggle line, not live text.
    const toggle = screen.getByRole("button", { name: /Reasoning/i });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");

    // Answer bubble is present with the streamed content.
    expect(screen.getByText(/The answer is 42\./)).toBeTruthy();

    // ONE cursor only — the answer bubble's end-of-answer caret. ProcessStream
    // must NOT render its own caret once the answer has started (no double).
    expect(container.querySelectorAll(".lmchat-stream-caret")).toHaveLength(1);
    expect(screen.queryByTestId("process-stream-caret")).toBeNull();
    expect(screen.getByTestId("chat-message-stream-caret")).toBeTruthy();

    // The surface is no longer "live".
    expect(
      screen.getByTestId("process-stream").getAttribute("data-live"),
    ).toBe("false");
  });

  it("(c2) the collapsed reasoning line re-expands on click and persists per message", () => {
    render(
      <ChatMessage
        message={{
          id: 77,
          role: "assistant",
          content: "Final answer.",
          reasoning_content: "Hidden reasoning detail.",
          streaming: false,
        }}
      />,
    );

    const toggle = screen.getByRole("button", { name: /Reasoning/i });
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    // Persisted to sessionStorage under the per-message key.
    expect(sessionStorage.getItem("lmchat:thinking:77")).toBe("true");
  });

  it("(d) tool calls render as quiet inline lines (not cards) with name + status + result", () => {
    const toolCalls: ToolCall[] = [
      {
        id: "tc1",
        name: "firecrawl_search",
        arguments: JSON.stringify({ q: "lm studio" }),
        status: "success",
        result: "Found 3 results.",
      },
    ];
    const { container } = render(
      <ChatMessage
        message={{
          id: "streaming",
          role: "assistant",
          content: "",
          reasoning_content: null,
          toolCalls,
          streaming: true,
        }}
        streamingActive={true}
      />,
    );

    // Inline line, NOT the old card class.
    expect(container.querySelectorAll(".lmchat-process-tool")).toHaveLength(1);
    expect(container.querySelector(".lmchat-tool-card")).toBeNull();

    const line = container.querySelector(".lmchat-process-tool");
    expect(line).not.toBeNull();
    // Friendly name (formatToolName humanizes the raw MCP name).
    expect(within(line as HTMLElement).getByText(/Firecrawl Search/i)).toBeTruthy();
    // Status preserved.
    expect(within(line as HTMLElement).getByText("success")).toBeTruthy();
    // Result is reachable (inside the expandable line).
    expect(within(line as HTMLElement).getByText("Found 3 results.")).toBeTruthy();
  });

  it("(e) done: reasoning collapsed, no cursor anywhere", () => {
    const { container } = render(
      <ChatMessage
        message={{
          id: 5,
          role: "assistant",
          content: "Completed answer.",
          reasoning_content: "Some reasoning.",
          streaming: false,
        }}
      />,
    );

    // Collapsed reasoning line present.
    expect(screen.getByRole("button", { name: /Reasoning/i })).toBeTruthy();
    // No cursor on a settled turn.
    expect(container.querySelectorAll(".lmchat-stream-caret")).toHaveLength(0);
    // Answer content present.
    expect(screen.getByText(/Completed answer\./)).toBeTruthy();
  });

  it("renders nothing extra for a plain assistant turn with no reasoning/tools (no empty surface)", () => {
    render(
      <ProcessStream
        messageId={1}
        reasoning={null}
        toolCalls={undefined}
        streaming={false}
        hasAnswer={true}
      />,
    );
    // No reasoning, no tools, not live → the surface renders nothing.
    expect(screen.queryByTestId("process-stream")).toBeNull();
  });
});

describe("ProcessStream — thinkbox container + scroll refs", () => {
  it("live reasoning is wrapped in a .lmchat-process-thinkbox container", () => {
    const { container } = render(
      <ProcessStream
        messageId={42}
        reasoning="Working through this carefully…"
        streaming={true}
        hasAnswer={false}
      />,
    );

    // The thinkbox wrapper must be present in live mode.
    const thinkbox = container.querySelector(".lmchat-process-thinkbox");
    expect(thinkbox).not.toBeNull();

    // The live <pre> must be INSIDE the thinkbox.
    const pre = container.querySelector(
      ".lmchat-process-thinkbox .lmchat-process-reasoning__text",
    );
    expect(pre).not.toBeNull();
    expect(pre?.textContent).toContain("Working through this carefully");
  });

  it("expanded collapsed reasoning body is also wrapped in .lmchat-process-thinkbox", () => {
    const { container } = render(
      <ProcessStream
        messageId={43}
        reasoning="Hidden chain of thought."
        streaming={false}
        hasAnswer={true}
      />,
    );

    // Expand the collapsed panel.
    const toggle = screen.getByRole("button", { name: /Reasoning/i });
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");

    // Thinkbox should now be present inside the expanded body.
    const thinkbox = container.querySelector(
      ".lmchat-process-reasoning__body-inner .lmchat-process-thinkbox",
    );
    expect(thinkbox).not.toBeNull();

    // The <pre> inside also carries the scroll handler (ref attached to pre).
    const pre = thinkbox?.querySelector(".lmchat-process-reasoning__text");
    expect(pre).not.toBeNull();
    expect(pre?.textContent).toContain("Hidden chain of thought");
  });

  it("live reasoning <pre> carries the data-testid for scroll-ref identification", () => {
    const { container } = render(
      <ProcessStream
        messageId={44}
        reasoning="Thinking out loud here."
        streaming={true}
        hasAnswer={false}
      />,
    );

    const pre = container.querySelector("[data-testid='process-reasoning-live']");
    expect(pre).not.toBeNull();
    expect(pre?.tagName.toLowerCase()).toBe("pre");
  });
});
