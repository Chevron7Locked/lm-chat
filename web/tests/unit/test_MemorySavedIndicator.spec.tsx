/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for MemorySavedIndicator — the quiet auto-memory-saved signal
 * rendered from the BE `memory.saved` SSE frame (see useSSE's
 * `StreamState.memorySaved`).
 *
 * Locked behaviours:
 *   - Renders nothing when `memorySaved` is undefined (no frame this turn).
 *   - Renders nothing when count <= 0 (defensive — the BE never sends one,
 *     but the component must not trust the wire blindly).
 *   - Renders a singular "Memory updated" line for count === 1.
 *   - Renders a pluralized count for count > 1.
 */
import { describe, it, expect } from "vitest";
import { render, screen, cleanup } from "@testing-library/react";
import { afterEach } from "vitest";
import { MemorySavedIndicator } from "@/components/chat/MemorySavedIndicator";

afterEach(() => {
  cleanup();
});

describe("MemorySavedIndicator", () => {
  it("renders nothing when memorySaved is undefined", () => {
    const { container } = render(
      <MemorySavedIndicator memorySaved={undefined} />,
    );
    expect(container.firstChild).toBeNull();
    expect(screen.queryByTestId("memory-saved-indicator")).toBeNull();
  });

  it("renders nothing when count is 0", () => {
    const { container } = render(
      <MemorySavedIndicator memorySaved={{ count: 0, msgId: 1 }} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders a singular line when count is 1", () => {
    render(<MemorySavedIndicator memorySaved={{ count: 1, msgId: 5 }} />);
    const el = screen.getByTestId("memory-saved-indicator");
    expect(el.textContent).toBe("Memory updated");
  });

  it("renders a pluralized count when count > 1", () => {
    render(<MemorySavedIndicator memorySaved={{ count: 3, msgId: 5 }} />);
    const el = screen.getByTestId("memory-saved-indicator");
    expect(el.textContent).toBe("Memory updated · 3 things remembered");
  });

  it("uses role=status so it's announced without stealing focus", () => {
    render(<MemorySavedIndicator memorySaved={{ count: 1, msgId: 5 }} />);
    expect(screen.getByRole("status")).not.toBeNull();
  });
});
