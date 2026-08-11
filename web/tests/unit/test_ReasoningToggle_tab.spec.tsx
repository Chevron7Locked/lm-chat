/**
 * Unit tests for ReasoningToggle "use global default" Tab-reachability
 * (P9a Item B).
 *
 * Closes DEFERRED §13.
 *
 * Covers:
 * - "use global default" button has role="option" so it joins the
 *   useDropdownKeyboard [role="option"] Tab cycle.
 * - Tab cycle includes all options including "use global default"
 *   (5 total when chatId is provided: off, low, medium, high, clear).
 * - Activating "use global default" calls setChatReasoning with "".
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { createElement } from "react";

// ─── Mock chatSettingsStore ───────────────────────────────────────────────────

const mockSetChatReasoning = vi.fn();
const mockCycleGlobalReasoning = vi.fn();

vi.mock("@/stores/chatSettingsStore", () => ({
  useChatSettingsStore: () => ({
    globalReasoning: "off" as const,
    cycleGlobalReasoning: mockCycleGlobalReasoning,
    setChatReasoning: mockSetChatReasoning,
    effectiveReasoning: (_chatId: number) => "medium" as const,
    chatOverrides: { 42: "medium" },
    setGlobalReasoning: vi.fn(),
  }),
}));

// ─── Tests ────────────────────────────────────────────────────────────────────

beforeEach(() => { vi.clearAllMocks(); });

describe("ReasoningToggle — 'use global default' Tab reachability (P9a §13)", () => {
  it("'use global default' button has role='option'", async () => {
    const { ReasoningToggle } = await import("@/components/ReasoningToggle");
    render(createElement(ReasoningToggle, { chatId: 42 }));

    // Open the dropdown via ArrowDown on the main button.
    const btn = screen.getByTestId("reasoning-toggle");
    act(() => { fireEvent.keyDown(btn, { key: "ArrowDown" }); });

    // The clear button must have role="option".
    const clearBtn = screen.getByTestId("reasoning-option-clear");
    expect((clearBtn as HTMLElement).getAttribute("role")).toBe("option");
  });

  it("dropdown has 5 options when chatId is provided (off+low+med+high+clear)", async () => {
    const { ReasoningToggle } = await import("@/components/ReasoningToggle");
    render(createElement(ReasoningToggle, { chatId: 42 }));

    const btn = screen.getByTestId("reasoning-toggle");
    act(() => { fireEvent.keyDown(btn, { key: "ArrowDown" }); });

    // All four level options + the "use global default" option.
    const options = screen.getAllByRole("option");
    expect(options).toHaveLength(5);
  });

  it("'use global default' button is reachable via keyboard Tab cycle", async () => {
    const { ReasoningToggle } = await import("@/components/ReasoningToggle");
    const { getByTestId, getAllByRole } = render(
      createElement(ReasoningToggle, { chatId: 42 })
    );

    const mainBtn = getByTestId("reasoning-toggle");
    act(() => { fireEvent.keyDown(mainBtn, { key: "ArrowDown" }); });

    // Get all options including the clear button.
    const options = getAllByRole("option");
    const clearBtn = getByTestId("reasoning-option-clear");

    // The clear button must be in the set of role="option" elements,
    // confirming it is part of the useDropdownKeyboard itemSelector cycle.
    expect(options).toContain(clearBtn);
  });

  it("clicking 'use global default' calls setChatReasoning with empty string", async () => {
    const { ReasoningToggle } = await import("@/components/ReasoningToggle");
    render(createElement(ReasoningToggle, { chatId: 42 }));

    const btn = screen.getByTestId("reasoning-toggle");
    act(() => { fireEvent.keyDown(btn, { key: "ArrowDown" }); });

    const clearBtn = screen.getByTestId("reasoning-option-clear");
    act(() => { fireEvent.click(clearBtn); });

    expect(mockSetChatReasoning).toHaveBeenCalledWith(42, "");
  });
});
