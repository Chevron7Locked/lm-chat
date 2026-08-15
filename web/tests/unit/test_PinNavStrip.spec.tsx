/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for PinNavStrip and PinnedMessagesPanel — the P13e (O-07/O-08)
 * chrome-polish pinning surface.
 *
 * Locked behaviours:
 *   - Returns null when no pins exist for the chat (component absent from DOM).
 *   - Renders one pin item per pinned message ID with a truncated preview.
 *   - Click on a pin invokes scrollIntoView on the matching data-message-id node.
 *   - Unpin (✕) removes the entry from the store and the rendered list.
 *   - PinnedMessagesPanel opens / closes via the `open` prop and unpin works
 *     identically from the panel.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, act } from "@testing-library/react";
import { PinNavStrip } from "@/components/PinNavStrip";
import { PinnedMessagesPanel } from "@/components/PinnedMessagesPanel";
import { usePinnedMessagesStore } from "@/stores/pinnedMessagesStore";

// Reset the persisted store + DOM between tests.
beforeEach(() => {
  localStorage.clear();
  // Force-reset the in-memory Zustand state (the store seeds from localStorage
  // at module-import time, so we re-seed here to keep tests independent).
  usePinnedMessagesStore.setState({ byChat: {} });
});

describe("PinNavStrip", () => {
  const messages = [
    { id: 1, role: "user", content: "What is the meaning of life?" },
    { id: 2, role: "assistant", content: "42 is the canonical answer — Adams." },
    { id: 3, role: "user", content: "Lorem ipsum dolor sit amet consectetur." },
  ];

  it("renders null when the chat has no pinned messages", () => {
    const { container } = render(<PinNavStrip chatId={1} messages={messages} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders one pin item per pinned message and shows truncated preview", () => {
    usePinnedMessagesStore.getState().pin(1, 2);
    usePinnedMessagesStore.getState().pin(1, 3);

    render(<PinNavStrip chatId={1} messages={messages} previewLen={20} />);

    expect(screen.getByTestId("pin-nav-strip")).not.toBeNull();
    expect(screen.getByTestId("pin-nav-item-2")).not.toBeNull();
    expect(screen.getByTestId("pin-nav-item-3")).not.toBeNull();
    // 20-char cap means the long message gets a trailing ellipsis.
    const item3 = screen.getByTestId("pin-nav-item-3");
    expect(item3.textContent).toContain("…");
  });

  it("invokes scrollIntoView on the targeted data-message-id node when a pin is clicked", () => {
    usePinnedMessagesStore.getState().pin(7, 2);

    // Mount a stand-in message element so the click handler finds a target.
    const target = document.createElement("div");
    target.setAttribute("data-message-id", "2");
    const scrollIntoViewSpy = vi.fn();
    target.scrollIntoView = scrollIntoViewSpy;
    document.body.appendChild(target);

    render(<PinNavStrip chatId={7} messages={messages} />);
    fireEvent.click(screen.getByTestId("pin-nav-item-2"));
    expect(scrollIntoViewSpy).toHaveBeenCalledTimes(1);

    document.body.removeChild(target);
  });

  it("unpins via ✕ button and removes the entry from the strip", () => {
    usePinnedMessagesStore.getState().pin(1, 2);
    const { rerender } = render(<PinNavStrip chatId={1} messages={messages} />);

    expect(screen.getByTestId("pin-nav-item-2")).not.toBeNull();
    fireEvent.click(screen.getByLabelText("Unpin message 2"));

    // Re-render to flush the new store state into the tree.
    rerender(<PinNavStrip chatId={1} messages={messages} />);
    expect(screen.queryByTestId("pin-nav-item-2")).toBeNull();
  });

  it("ignores pins whose backing message is missing (defensive)", () => {
    usePinnedMessagesStore.getState().pin(1, 99); // no message id 99
    const { container } = render(<PinNavStrip chatId={1} messages={messages} />);
    // No pins resolve → component renders nothing.
    expect(container.firstChild).toBeNull();
  });
});

describe("PinnedMessagesPanel", () => {
  const messages = [
    { id: 10, role: "user", content: "Pinned A" },
    { id: 11, role: "assistant", content: "Pinned B" },
  ];

  it("renders nothing when closed", () => {
    usePinnedMessagesStore.getState().pin(2, 10);
    const { container } = render(
      <PinnedMessagesPanel chatId={2} open={false} onClose={() => undefined} messages={messages} />
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders an entry per pinned message when open", () => {
    usePinnedMessagesStore.getState().pin(2, 10);
    usePinnedMessagesStore.getState().pin(2, 11);
    render(
      <PinnedMessagesPanel chatId={2} open onClose={() => undefined} messages={messages} />
    );
    expect(screen.getByTestId("pinned-panel-item-10")).not.toBeNull();
    expect(screen.getByTestId("pinned-panel-item-11")).not.toBeNull();
  });

  it("shows the empty-state copy when no pins exist", () => {
    render(
      <PinnedMessagesPanel chatId={42} open onClose={() => undefined} messages={messages} />
    );
    expect(screen.getByText(/No pinned messages yet/i)).not.toBeNull();
  });

  it("calls onClose when the Escape key is pressed", () => {
    usePinnedMessagesStore.getState().pin(2, 10);
    const onClose = vi.fn();
    render(
      <PinnedMessagesPanel chatId={2} open onClose={onClose} messages={messages} />
    );
    act(() => {
      window.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    });
    expect(onClose).toHaveBeenCalled();
  });
});

describe("pinnedMessagesStore", () => {
  it("toggle flips pinned state and persists to localStorage", () => {
    usePinnedMessagesStore.getState().toggle(5, 99);
    expect(usePinnedMessagesStore.getState().isPinned(5, 99)).toBe(true);

    // localStorage round-trip — JSON encoded.
    const raw = localStorage.getItem("lmchat:pinned-messages");
    expect(raw).not.toBeNull();
    const parsed = JSON.parse(raw ?? "null") as Record<string, number[]>;
    expect(parsed["5"]).toEqual([99]);

    usePinnedMessagesStore.getState().toggle(5, 99);
    expect(usePinnedMessagesStore.getState().isPinned(5, 99)).toBe(false);
    // Empty array → key removed from storage.
    const raw2 = localStorage.getItem("lmchat:pinned-messages");
    const parsed2 = JSON.parse(raw2 ?? "{}") as Record<string, number[]>;
    expect(parsed2["5"]).toBeUndefined();
  });

  it("pinsFor returns [] for an unknown chat", () => {
    expect(usePinnedMessagesStore.getState().pinsFor(999)).toEqual([]);
  });
});
