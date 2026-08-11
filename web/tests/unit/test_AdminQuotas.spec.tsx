/* SPDX-License-Identifier: Apache-2.0 */
/**
 * AdminQuotas page unit tests — render + admin-gate.
 *
 * Locked behaviours:
 *   - The page renders the "Quotas" heading and the system-default copy.
 *   - When the admin list returns an empty array, the empty-state copy
 *     renders.
 *   - When the list resolves with rows, a row per user is rendered with
 *     the formatted tokens/day and requests/day values.
 *   - When the admin hook surfaces an error (the backend gate disables the
 *     query for non-admins), the error path renders the failure copy.
 *   - Clicking "Edit" on a row reveals two number inputs and the Save/Cancel
 *     action pair; clicking Save calls updateQuota.mutate with the row id
 *     and the parsed integer values.
 *
 * useQuota hooks are mocked at module level so the suite does not need a
 * TanStack Query provider.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";

import type { QuotaSummary } from "@/hooks/useQuota";

// ─── Mock useQuota hooks ─────────────────────────────────────────────────────

const mockUseAdminQuotaList = vi.fn();
const mockUpdateMutate = vi.fn();
const mockRefetch = vi.fn();

vi.mock("@/hooks/useQuota", () => ({
  useAdminQuotaList: () => mockUseAdminQuotaList(),
  useUpdateQuota: () => ({ mutate: mockUpdateMutate, isPending: false }),
}));

// ─── Fixtures ────────────────────────────────────────────────────────────────

const ROW_A: QuotaSummary = {
  user_id: 1,
  tokens_per_day: 100_000,
  requests_per_day: 1_000,
};
const ROW_B: QuotaSummary = {
  user_id: 2,
  tokens_per_day: 50_000,
  requests_per_day: 500,
};

async function freshAdminQuotas() {
  vi.resetModules();
  const mod = await import("@/pages/AdminQuotas");
  return mod.default;
}

describe("AdminQuotas", () => {
  beforeEach(() => {
    mockUseAdminQuotaList.mockReset();
    mockUpdateMutate.mockReset();
    mockRefetch.mockReset();
    cleanup();
  });

  it("renders the page heading and the system-default copy", async () => {
    mockUseAdminQuotaList.mockReturnValue({
      data: [],
      isLoading: false,
      error: null,
      refetch: mockRefetch,
    });
    const Page = await freshAdminQuotas();
    render(<Page />);

    expect(screen.getByRole("heading", { name: "Quotas" })).toBeTruthy();
    expect(
      screen.getByText(/Users without an explicit quota row use system defaults/i),
    ).toBeTruthy();
  });

  it("renders the empty-state copy when no rows exist", async () => {
    mockUseAdminQuotaList.mockReturnValue({
      data: [],
      isLoading: false,
      error: null,
      refetch: mockRefetch,
    });
    const Page = await freshAdminQuotas();
    render(<Page />);

    expect(
      screen.getByText(/No explicit quota rows\. All users use system defaults\./i),
    ).toBeTruthy();
  });

  it("renders one table row per quota record with formatted numbers", async () => {
    mockUseAdminQuotaList.mockReturnValue({
      data: [ROW_A, ROW_B],
      isLoading: false,
      error: null,
      refetch: mockRefetch,
    });
    const Page = await freshAdminQuotas();
    render(<Page />);

    // Two data rows + one header row.
    expect(screen.getAllByRole("row")).toHaveLength(3);
    // Formatted with locale separators.
    expect(screen.getByText(ROW_A.tokens_per_day.toLocaleString())).toBeTruthy();
    expect(screen.getByText(ROW_A.requests_per_day.toLocaleString())).toBeTruthy();
    expect(screen.getByText(ROW_B.tokens_per_day.toLocaleString())).toBeTruthy();
  });

  it("surfaces the error copy when useAdminQuotaList errors out", async () => {
    // The backend gate (non-admin) lands in this state via the hook surface.
    mockUseAdminQuotaList.mockReturnValue({
      data: undefined,
      isLoading: false,
      error: { detail: "forbidden", message: "403" },
      refetch: mockRefetch,
    });
    const Page = await freshAdminQuotas();
    render(<Page />);

    expect(screen.getByText(/Couldn't load quotas:/i)).toBeTruthy();
    expect(screen.getByText(/forbidden/)).toBeTruthy();
  });

  it("clicking Edit then Save calls updateQuota with parsed integer values", async () => {
    mockUseAdminQuotaList.mockReturnValue({
      data: [ROW_A],
      isLoading: false,
      error: null,
      refetch: mockRefetch,
    });
    const Page = await freshAdminQuotas();
    render(<Page />);

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));

    const tokensInput = screen.getByLabelText(/Tokens per day for user 1/i);
    const requestsInput = screen.getByLabelText(/Requests per day for user 1/i);
    expect(tokensInput).toBeTruthy();
    expect(requestsInput).toBeTruthy();

    fireEvent.change(tokensInput, { target: { value: "200000" } });
    fireEvent.change(requestsInput, { target: { value: "2000" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));

    expect(mockUpdateMutate).toHaveBeenCalledWith(
      { userId: 1, tokensPerDay: 200_000, requestsPerDay: 2_000 },
      expect.objectContaining({ onSuccess: expect.any(Function) }),
    );
  });
});
