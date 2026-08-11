/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests — persona/sub-agent label on assistant turns.
 *
 * Locked behaviours (2026-06-20):
 *   (a) An assistant turn with personaLabel renders the label chip
 *       with the preset name ("Research", "Coder", etc.).
 *   (b) A turn with active_preset=research shows "Research", NOT the model name.
 *   (c) An assistant turn with NO personaLabel does NOT render the chip.
 *   (d) A user turn with personaLabel does NOT render the chip
 *       (label is assistant-only).
 *   (e) A streaming assistant turn does NOT show the chip mid-stream
 *       (the label only appears on completed turns).
 *
 * The top-bar + settings model selectors are NOT tested here — they live in
 * test_Chat.spec.tsx and test_Settings.spec.tsx and must show real model names.
 * This file only covers the in-chat assistant-turn persona label.
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ChatMessage } from "@/components/ChatMessage";

describe("ChatMessage — persona / sub-agent label", () => {
  it("(a) renders the persona label chip for an assistant turn with personaLabel", () => {
    render(
      <ChatMessage
        message={{
          id: 1,
          role: "assistant",
          content: "Here is my research summary.",
        }}
        personaLabel="Research"
      />,
    );
    const chip = screen.getByTestId("chat-message-persona-label");
    expect(chip).toBeTruthy();
    expect(chip.textContent).toContain("Research");
  });

  it("(b) shows the preset label ('Research'), not a model name, on an assistant turn", () => {
    // Simulate a turn that was produced by the research preset — the parent
    // derives this from active_preset → getPreset('research').label = 'Research'.
    // The ChatMessage component receives the resolved label as personaLabel.
    render(
      <ChatMessage
        message={{
          id: 2,
          role: "assistant",
          content: "Some research output.",
        }}
        personaLabel="Research"
      />,
    );
    const chip = screen.getByTestId("chat-message-persona-label");
    // The label shows the persona, not a raw model id like "qwen3" or "gpt-4o".
    expect(chip.textContent?.toLowerCase()).toContain("research");
    // Ensure no model-id-style string leaks into the chip.
    expect(chip.textContent).not.toMatch(/qwen|gpt|claude|llama/i);
  });

  it("(b2) shows 'Coder' label when active preset is coder", () => {
    render(
      <ChatMessage
        message={{
          id: 3,
          role: "assistant",
          content: "Here is some code.",
        }}
        personaLabel="Coder"
      />,
    );
    const chip = screen.getByTestId("chat-message-persona-label");
    expect(chip.textContent).toContain("Coder");
  });

  it("(c) does NOT render the label chip when personaLabel is absent", () => {
    render(
      <ChatMessage
        message={{
          id: 4,
          role: "assistant",
          content: "A plain reply with no preset active.",
        }}
      />,
    );
    expect(screen.queryByTestId("chat-message-persona-label")).toBeNull();
  });

  it("(c2) does NOT render the label chip when personaLabel is empty string", () => {
    render(
      <ChatMessage
        message={{
          id: 5,
          role: "assistant",
          content: "Reply with empty label.",
        }}
        personaLabel=""
      />,
    );
    expect(screen.queryByTestId("chat-message-persona-label")).toBeNull();
  });

  it("(d) does NOT render the label chip on a user turn, even if personaLabel is set", () => {
    render(
      <ChatMessage
        message={{
          id: 6,
          role: "user",
          content: "My question here.",
        }}
        personaLabel="Research"
      />,
    );
    // Label is assistant-only; user messages never show it.
    expect(screen.queryByTestId("chat-message-persona-label")).toBeNull();
  });

  it("(e) does NOT render the label chip while the turn is actively streaming", () => {
    render(
      <ChatMessage
        message={{
          id: 7,
          role: "assistant",
          content: "Streaming partial...",
          streaming: true,
        }}
        personaLabel="General"
        streamingActive={true}
      />,
    );
    // Label must not appear on the in-flight streaming row.
    expect(screen.queryByTestId("chat-message-persona-label")).toBeNull();
  });

  it("renders 'General' label for the general (default) persona", () => {
    render(
      <ChatMessage
        message={{
          id: 8,
          role: "assistant",
          content: "General response.",
        }}
        personaLabel="General"
      />,
    );
    const chip = screen.getByTestId("chat-message-persona-label");
    expect(chip.textContent).toContain("General");
  });

  it("renders a sub-agent label for a sub-session turn", () => {
    // Sub-session assistant turns receive the sub-session's presetLabel
    // (e.g. "Research") as personaLabel so they're visually distinct
    // from main-chat general turns.
    render(
      <ChatMessage
        message={{
          id: 9,
          role: "assistant",
          content: "Sub-agent research output here.",
        }}
        personaLabel="Research"
      />,
    );
    const chip = screen.getByTestId("chat-message-persona-label");
    expect(chip.textContent).toContain("Research");
  });

  it("(f) does NOT render the label chip when personaLabel is undefined (raw/none mode)", () => {
    // When the user selects "None · raw model" (RAW_PRESET_ID), Chat.tsx
    // derives currentPersonaLabel as undefined. No persona chip should
    // appear on raw-mode turns — the raw model has no persona.
    render(
      <ChatMessage
        message={{
          id: 10,
          role: "assistant",
          content: "Raw model reply — no preset active.",
        }}
        personaLabel={undefined}
      />,
    );
    expect(screen.queryByTestId("chat-message-persona-label")).toBeNull();
  });
});

describe("ChatMessage — skipEntranceAnimation", () => {
  it("sets data-skip-animate on the row when skipEntranceAnimation=true", () => {
    const { container } = render(
      <ChatMessage
        message={{ id: 99, role: "assistant", content: "Done." }}
        skipEntranceAnimation={true}
      />,
    );
    const row = container.querySelector(".lmchat-message-row");
    expect(row).toBeTruthy();
    expect(row!.hasAttribute("data-skip-animate")).toBe(true);
  });

  it("does NOT set data-skip-animate when skipEntranceAnimation is false", () => {
    const { container } = render(
      <ChatMessage
        message={{ id: 100, role: "assistant", content: "Normal new message." }}
        skipEntranceAnimation={false}
      />,
    );
    const row = container.querySelector(".lmchat-message-row");
    expect(row).toBeTruthy();
    expect(row!.hasAttribute("data-skip-animate")).toBe(false);
  });

  it("does NOT set data-skip-animate when skipEntranceAnimation is omitted", () => {
    const { container } = render(
      <ChatMessage
        message={{ id: 101, role: "user", content: "A user message." }}
      />,
    );
    const row = container.querySelector(".lmchat-message-row");
    expect(row).toBeTruthy();
    expect(row!.hasAttribute("data-skip-animate")).toBe(false);
  });
});
