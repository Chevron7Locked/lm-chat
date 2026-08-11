/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests — CompactionTab component ("folded page" reimagining).
 *
 * Tests:
 *   - collapsed seam shows the "N turns folded" pill label; aria-expanded="false"
 *   - archived content is absent from the DOM while collapsed
 *   - clicking the fold tab expands: aria-expanded="true"
 *   - the summary block renders the eyebrow, summary text, and a real "→" token line
 *   - the dense transcript renders role-marker rows (USER/MODEL) with the raw
 *     archived text, and NEVER via <ChatMessage> bubble chrome
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CompactionTab } from "@/components/CompactionTab";

// ─── Mocks ────────────────────────────────────────────────────────────────────

vi.mock("@/hooks/useCompactions", () => ({
  useCompactionMessages: (
    chatId: number | null,
    compactionId: number | null,
    enabled: boolean,
  ) => {
    if (enabled) {
      return {
        data: [
          { id: 100, role: "user", content: "Archived user message 1" },
          { id: 101, role: "assistant", content: "Archived assistant message 1" },
        ],
        isLoading: false,
      };
    }
    return { data: null, isLoading: false };
  },
}));

// ─── Component ────────────────────────────────────────────────────────────────

const mockQueryClient = new QueryClient({
  defaultOptions: { queries: { retry: false } },
});

const mockCompaction = {
  id: 42,
  summary: "This is a running summary of the archived span.",
  anchor_msg_id: 50,
  archived_count: 2,
  original_token_count: 900,
  summary_token_count: 120,
  created_at: "2026-07-01T00:00:00Z",
};

function renderComponent() {
  return render(
    <QueryClientProvider client={mockQueryClient}>
      {createElement(CompactionTab, { compaction: mockCompaction, chatId: 1 })}
    </QueryClientProvider>,
  );
}

describe("CompactionTab", () => {
  it("collapsed: the seam tab shows the folded-turn count and aria-expanded=false", () => {
    renderComponent();
    expect(screen.getByText("2 turns folded")).toBeTruthy();
    const toggle = screen.getByRole("button");
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
  });

  it("does NOT render archived content when collapsed", () => {
    renderComponent();
    expect(screen.queryByText("Archived user message 1")).toBeNull();
    expect(screen.queryByText("Archived assistant message 1")).toBeNull();
  });

  it("expands on toggle: aria-expanded becomes true", () => {
    renderComponent();
    const toggle = screen.getByRole("button");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
  });

  it("renders the summary block: eyebrow, summary text, and a real '→' token line", () => {
    renderComponent();
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText("Summary")).toBeTruthy();
    expect(screen.getByText(mockCompaction.summary)).toBeTruthy();
    expect(screen.getByText("~900 → ~120 tokens")).toBeTruthy();
  });

  it("shows a single group-level 'Archived' eyebrow instead of a per-message label", () => {
    renderComponent();
    fireEvent.click(screen.getByRole("button"));
    expect(screen.getByText("Archived · 2 messages")).toBeTruthy();
  });

  it("renders the dense transcript as plain role-marked rows with the raw archived text", () => {
    const { container } = renderComponent();
    fireEvent.click(screen.getByRole("button"));

    const transcript = container.querySelector(
      ".lmchat-compaction-tab__transcript",
    );
    expect(transcript).toBeTruthy();
    expect(transcript?.textContent).toContain("Archived user message 1");
    expect(transcript?.textContent).toContain("Archived assistant message 1");

    // Role markers are mapped labels, not raw role strings.
    expect(screen.getByText("USER")).toBeTruthy();
    expect(screen.getByText("MODEL")).toBeTruthy();
  });

  it("never renders <ChatMessage> bubble chrome for archived rows", () => {
    const { container } = renderComponent();
    fireEvent.click(screen.getByRole("button"));

    // ChatMessage's row/bubble/persona-label chrome must not appear anywhere
    // in this tree — archived history renders as plain transcript rows only.
    expect(container.querySelector(".lmchat-message-row")).toBeNull();
    expect(container.querySelector(".lmchat-bubble-user")).toBeNull();
    expect(container.querySelector(".lmchat-bubble-assistant")).toBeNull();
    expect(
      container.querySelector('[data-testid="chat-message-persona-label"]'),
    ).toBeNull();
  });

  it("clicking the fold tab toggles expansion (the whole pill is the toggle)", () => {
    renderComponent();
    const toggle = screen.getByRole("button");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("true");
    fireEvent.click(toggle);
    expect(toggle.getAttribute("aria-expanded")).toBe("false");
  });
});
