/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Cluster 3b FE tests — Continue chip + persisted tool_calls rendering.
 *
 * Locked behaviours:
 *   - Continue chip renders when showContinue === true and streaming !== true.
 *   - Continue chip is hidden when streaming === true (live bubble).
 *   - Continue chip is hidden when showContinue !== true.
 *   - ToolCallCards render from persisted toolCalls on reload (no live stream).
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ChatMessage } from "@/components/ChatMessage";

// ─── Continue chip (Cluster 3b Task 2) ────────────────────────────────────────

describe("ChatMessage — Continue chip (Cluster 3b Task 2)", () => {
  it("renders continue-chip when stop_reason=length (showContinue=true, not streaming)", () => {
    render(
      <ChatMessage
        message={{
          id: 10,
          role: "assistant",
          content: "Some partial response that was cut off.",
          showContinue: true,
          streaming: false,
        }}
      />,
    );
    const chip = screen.getByTestId("continue-chip");
    expect(chip).toBeTruthy();
    // ARIA label must be present for accessibility.
    expect(chip.getAttribute("aria-label")).toBeTruthy();
  });

  it("renders continue-chip when truncated_without_terminal (showContinue=true, not streaming)", () => {
    // This is the Cluster 1 Task 4 source — same chip, different trigger.
    render(
      <ChatMessage
        message={{
          id: 11,
          role: "assistant",
          content: "Truncated mid-stream content.",
          showContinue: true,
          streaming: false,
        }}
      />,
    );
    expect(screen.getByTestId("continue-chip")).toBeTruthy();
  });

  it("hides continue-chip when streaming is true (live bubble)", () => {
    render(
      <ChatMessage
        message={{
          id: 12,
          role: "assistant",
          content: "Still streaming...",
          showContinue: true,
          streaming: true,
        }}
      />,
    );
    // Must not appear during the live stream.
    expect(screen.queryByTestId("continue-chip")).toBeNull();
  });

  it("hides continue-chip when showContinue is false (normal completion)", () => {
    render(
      <ChatMessage
        message={{
          id: 13,
          role: "assistant",
          content: "Completed normally.",
          showContinue: false,
          streaming: false,
        }}
      />,
    );
    expect(screen.queryByTestId("continue-chip")).toBeNull();
  });

  it("renders ONLY the Stopped chip when stopped=true AND showContinue=true (Optimize R1 F8 mutual exclusion)", () => {
    // The state machine upstream prevents this combination, but the render
    // layer must enforce the invariant independently: Stopped wins, the
    // Continue chip never co-renders.
    render(
      <ChatMessage
        message={{
          id: 15,
          role: "assistant",
          content: "Cut off AND stopped — contradictory upstream state.",
          stopped: true,
          showContinue: true,
          streaming: false,
        }}
      />,
    );
    expect(screen.getByTestId("chat-message-stopped-chip")).toBeTruthy();
    expect(screen.queryByTestId("continue-chip")).toBeNull();
  });

  it("hides continue-chip for user-role messages even with showContinue=true", () => {
    render(
      <ChatMessage
        message={{
          id: 14,
          role: "user",
          content: "User message.",
          showContinue: true,
          streaming: false,
        }}
      />,
    );
    expect(screen.queryByTestId("continue-chip")).toBeNull();
  });
});

// ─── Persisted tool_calls rendering (Cluster 3b Task 4) ──────────────────────

describe("ChatMessage — persisted tool_calls on reload (Cluster 3b Task 4)", () => {
  it("renders ToolCallCards from persisted toolCalls on reload", () => {
    render(
      <ChatMessage
        message={{
          id: 20,
          role: "assistant",
          content: "I searched for you.",
          toolCalls: [
            {
              id: "tc_abc123",
              name: "search_web",
              arguments: JSON.stringify({ query: "lm studio" }),
              status: "success",
              result: "LM Studio is a desktop application.",
            },
          ],
          streaming: false,
        }}
      />,
    );
    // formatToolName converts "search_web" → "Search Web".
    expect(screen.getByText(/Search Web/i)).toBeTruthy();
  });

  it("renders multiple ToolCallCards when persisted toolCalls has several items", () => {
    render(
      <ChatMessage
        message={{
          id: 21,
          role: "assistant",
          content: "Done.",
          toolCalls: [
            {
              id: "tc_1",
              name: "tool_alpha",
              arguments: "{}",
              status: "success",
              result: "result a",
            },
            {
              id: "tc_2",
              name: "tool_beta",
              arguments: "{}",
              status: "failure",
              result: "error msg",
            },
          ],
          streaming: false,
        }}
      />,
    );
    // formatToolName converts snake_case → "Tool Alpha", "Tool Beta".
    expect(screen.getByText(/Tool Alpha/i)).toBeTruthy();
    expect(screen.getByText(/Tool Beta/i)).toBeTruthy();
  });

  it("renders no ToolCallCards when toolCalls is null (plain message reload)", () => {
    const { container } = render(
      <ChatMessage
        message={{
          id: 22,
          role: "assistant",
          content: "Plain response.",
          toolCalls: undefined,
          streaming: false,
        }}
      />,
    );
    // No tool-card details elements expected.
    const toolCardList = container.querySelectorAll(".lmchat-tool-card");
    expect(toolCardList.length).toBe(0);
  });
});
