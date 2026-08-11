/* SPDX-License-Identifier: Apache-2.0 */
/**
 * ChatMessage — launch-mode chip row.
 *
 * Locked behaviours:
 *   - An assistant message whose content references /research renders a
 *     launch-chip-research button.
 *   - Clicking it calls onLaunchMode with the preset id ("research").
 *   - A message with no referenced commands renders no chip row.
 *   - User-role messages never render chips, even if onLaunchMode is
 *     supplied and the text references a command.
 *   - The chip row is suppressed while the assistant turn is streaming.
 *   - No chip row without an onLaunchMode handler.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ChatMessage } from "@/components/ChatMessage";

describe("ChatMessage — launch-mode chips", () => {
  it("renders a launch-chip-research button when content references /research", () => {
    const onLaunchMode = vi.fn();
    render(
      <ChatMessage
        message={{
          id: 1,
          role: "assistant",
          content: "For this you should try /research on the topic.",
        }}
        onLaunchMode={onLaunchMode}
      />,
    );
    expect(screen.getByTestId("launch-chip-research")).toBeTruthy();
  });

  it("calls onLaunchMode('research') when the chip is clicked", () => {
    const onLaunchMode = vi.fn();
    render(
      <ChatMessage
        message={{
          id: 2,
          role: "assistant",
          content: "Try /research on the topic.",
        }}
        onLaunchMode={onLaunchMode}
      />,
    );
    fireEvent.click(screen.getByTestId("launch-chip-research"));
    expect(onLaunchMode).toHaveBeenCalledWith("research");
  });

  it("renders one chip per referenced mode, in first-appearance order", () => {
    const onLaunchMode = vi.fn();
    render(
      <ChatMessage
        message={{
          id: 3,
          role: "assistant",
          content: "You could /architect this, then /code it up.",
        }}
        onLaunchMode={onLaunchMode}
      />,
    );
    const row = screen.getByTestId("launch-chips-row");
    const buttons = row.querySelectorAll("button");
    expect(buttons).toHaveLength(2);
    expect(buttons[0]?.getAttribute("data-testid")).toBe(
      "launch-chip-architect",
    );
    expect(buttons[1]?.getAttribute("data-testid")).toBe("launch-chip-coder");
  });

  it("renders no chip row when content has no referenced commands", () => {
    const onLaunchMode = vi.fn();
    render(
      <ChatMessage
        message={{
          id: 4,
          role: "assistant",
          content: "A plain reply with nothing special in it.",
        }}
        onLaunchMode={onLaunchMode}
      />,
    );
    expect(screen.queryByTestId("launch-chips-row")).toBeNull();
  });

  it("renders no chips on a user-role message even if the text references /research", () => {
    const onLaunchMode = vi.fn();
    render(
      <ChatMessage
        message={{
          id: 5,
          role: "user",
          content: "Can you /research this for me?",
        }}
        onLaunchMode={onLaunchMode}
      />,
    );
    expect(screen.queryByTestId("launch-chips-row")).toBeNull();
    expect(screen.queryByTestId("launch-chip-research")).toBeNull();
  });

  it("suppresses the chip row while the assistant turn is streaming", () => {
    const onLaunchMode = vi.fn();
    render(
      <ChatMessage
        message={{
          id: 6,
          role: "assistant",
          content: "Try /research on this",
          streaming: true,
        }}
        onLaunchMode={onLaunchMode}
      />,
    );
    expect(screen.queryByTestId("launch-chips-row")).toBeNull();
  });

  it("renders no chip row without an onLaunchMode handler", () => {
    render(
      <ChatMessage
        message={{
          id: 7,
          role: "assistant",
          content: "Try /research on this",
        }}
      />,
    );
    expect(screen.queryByTestId("launch-chips-row")).toBeNull();
  });
});
