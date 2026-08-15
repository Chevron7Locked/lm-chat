/**
 * Unit tests for AdminIntegrations page.
 *
 * Tests:
 * - Renders with empty list and no-entries message
 * - Renders entries from useIntegrationsList
 * - R-10 banner is visible
 * - Add form adds a new entry to local state
 * - Remove button removes an entry from local state
 * - Save button calls the mutation with current entries
 * - Reset button discards local changes
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// ─── Mock hooks ──────────────────────────────────────────────────────────────

const mockUseIntegrationsList = vi.fn();
const mockMutate = vi.fn();
const mockMutation = {
  mutate: mockMutate,
  isPending: false,
  isSuccess: false,
  isError: false,
};

vi.mock("@/hooks/useIntegrationsList", () => ({
  useIntegrationsList: () => mockUseIntegrationsList(),
  useUpdateIntegrationsList: () => mockMutation,
}));

// ─── Mock toast ───────────────────────────────────────────────────────────────

const mockPush = vi.fn();
vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: mockPush }),
}));

// ─── Import after mocks ───────────────────────────────────────────────────────

import AdminIntegrations from "@/pages/AdminIntegrations";
import type { IntegrationEntry } from "@/hooks/useIntegrationsList";

// ─── Helpers ─────────────────────────────────────────────────────────────────

beforeEach(() => {
  vi.clearAllMocks();
  mockMutation.mutate = mockMutate;
  mockMutation.isPending = false;
});

function renderPage(entries: IntegrationEntry[] = [], isLoading = false) {
  mockUseIntegrationsList.mockReturnValue({ data: entries, isLoading, error: null });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    createElement(
      QueryClientProvider,
      { client: qc },
      createElement(AdminIntegrations)
    )
  );
}

const STUB_ENTRIES: IntegrationEntry[] = [
  {
    id: 1,
    value: "mcp/searxng",
    sort_order: 0,
    created_at: "2026-05-21T00:00:00Z",
    updated_at: "2026-05-21T00:00:00Z",
  },
  {
    id: 2,
    value: "mcp/filesystem",
    sort_order: 1,
    created_at: "2026-05-21T00:00:00Z",
    updated_at: "2026-05-21T00:00:00Z",
  },
];

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("AdminIntegrations", () => {
  it("renders heading", () => {
    renderPage();
    expect(
      screen.getByRole("heading", { name: /admin.*integrations/i })
    ).toBeTruthy();
  });

  it("renders R-10 banner", () => {
    renderPage();
    const banner = screen.getByTestId("r10-banner");
    expect(banner).toBeTruthy();
    expect(banner.textContent).toMatch(/operator-supplied/i);
    expect(banner.textContent).toMatch(/LM Studio does not expose/i);
  });

  it("shows no-entries message when list is empty", () => {
    renderPage([]);
    expect(screen.getByText(/no integrations configured/i)).toBeTruthy();
  });

  it("renders entries from useIntegrationsList", () => {
    renderPage(STUB_ENTRIES);
    const list = screen.getByTestId("integrations-list");
    expect(list).toBeTruthy();
    expect(screen.getByTestId("remove-btn-mcp/searxng")).toBeTruthy();
    expect(screen.getByTestId("remove-btn-mcp/filesystem")).toBeTruthy();
  });

  it("add form adds a new entry to local state", async () => {
    renderPage(STUB_ENTRIES);
    const input = screen.getByTestId("add-input");
    const addBtn = screen.getByTestId("add-btn");

    fireEvent.change(input, { target: { value: "mcp/new-tool" } });
    fireEvent.click(addBtn);

    await waitFor(() => {
      expect(screen.getByTestId("remove-btn-mcp/new-tool")).toBeTruthy();
    });
  });

  it("remove button removes an entry from local state", async () => {
    renderPage(STUB_ENTRIES);
    const removeBtn = screen.getByTestId("remove-btn-mcp/searxng");
    fireEvent.click(removeBtn);

    await waitFor(() => {
      expect(screen.queryByTestId("remove-btn-mcp/searxng")).toBeNull();
    });
  });

  it("save button calls mutation with current entries", async () => {
    renderPage(STUB_ENTRIES);

    // Add an entry to make it dirty.
    const input = screen.getByTestId("add-input");
    fireEvent.change(input, { target: { value: "mcp/extra" } });
    fireEvent.click(screen.getByTestId("add-btn"));

    await waitFor(() => {
      const saveBtn = screen.getByTestId("save-btn") as HTMLButtonElement;
      expect(saveBtn.disabled).toBe(false);
    });

    fireEvent.click(screen.getByTestId("save-btn"));

    expect(mockMutate).toHaveBeenCalledTimes(1);
    // Guaranteed non-null by the toHaveBeenCalledTimes(1) check just above.
    const calledWith = mockMutate.mock.calls[0]![0] as Array<{ value: string }>;
    const values = calledWith.map((e) => e.value);
    expect(values).toContain("mcp/extra");
    expect(values).toContain("mcp/searxng");
  });

  it("reset button discards local changes", async () => {
    renderPage(STUB_ENTRIES);

    // Make a change.
    const input = screen.getByTestId("add-input");
    fireEvent.change(input, { target: { value: "mcp/temp" } });
    fireEvent.click(screen.getByTestId("add-btn"));

    await waitFor(() => {
      expect(screen.getByTestId("reset-btn")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("reset-btn"));

    await waitFor(() => {
      expect(screen.queryByTestId("remove-btn-mcp/temp")).toBeNull();
      expect(screen.queryByTestId("reset-btn")).toBeNull();
    });
  });

  it("save button is disabled when no changes are pending", () => {
    renderPage(STUB_ENTRIES);
    const saveBtn = screen.getByTestId("save-btn") as HTMLButtonElement;
    expect(saveBtn.disabled).toBe(true);
  });
});
