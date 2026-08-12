/* SPDX-License-Identifier: Apache-2.0 */
/**
 * FocusModeExit — the slim, always-reachable exit affordance.
 *
 * Coverage:
 *  - renders nothing when inactive (no residual chrome off focus mode).
 *  - renders a labelled button when active; clicking it calls onExit.
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { FocusModeExit } from "@/components/chat/FocusModeExit";

afterEach(() => {
  cleanup();
});

describe("FocusModeExit", () => {
  it("renders nothing when inactive", () => {
    const { container } = render(
      <FocusModeExit active={false} onExit={() => undefined} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders a labelled exit button when active and calls onExit on click", () => {
    const onExit = vi.fn();
    render(<FocusModeExit active onExit={onExit} />);
    const btn = screen.getByTestId("focus-mode-exit");
    expect(btn.getAttribute("aria-label")).toBe("Exit focus mode");
    fireEvent.click(btn);
    expect(onExit).toHaveBeenCalledOnce();
  });
});
