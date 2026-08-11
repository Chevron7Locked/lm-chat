/**
 * Unit tests for the AdminAuditLog page (/admin/audit-log).
 *
 * Tests:
 * - renders heading + description
 * - renders rows with time/event/user_id/ip/detail columns
 * - shows the pagination status + total count
 * - Next is disabled on the last page; Previous is disabled on the first
 * - Next click advances the page (re-invokes useAuditLog with a new offset)
 * - typing + submitting the filter form resets to page 0 and passes the
 *   trimmed event string through
 * - Clear button reappears only once a filter is applied, and resets it
 * - shows an error banner when the query fails
 * - shows the empty state when there are no rows
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { createElement } from "react";

// ─── Mocks ──────────────────────────────────────────────────────────────────

const mockUseAuditLog = vi.fn();

vi.mock("@/hooks/useAuditLog", () => ({
  useAuditLog: (params: unknown) => mockUseAuditLog(params),
}));

import AdminAuditLog from "@/pages/AdminAuditLog";
import type { AuditLogRow } from "@/hooks/useAuditLog";

// ─── Fixtures ───────────────────────────────────────────────────────────────

const ROW_1: AuditLogRow = {
  id: 1,
  user_id: 42,
  event: "auth.login.success",
  ip: "127.0.0.1",
  user_agent: "vitest",
  detail: { via: "password" },
  created_at: "2026-07-01T10:00:00Z",
};

const ROW_2: AuditLogRow = {
  id: 2,
  user_id: null,
  event: "admin.user.deleted",
  ip: null,
  user_agent: null,
  detail: null,
  created_at: "2026-07-02T11:30:00Z",
};

beforeEach(() => {
  vi.clearAllMocks();
});

function renderPage(
  items: AuditLogRow[] | undefined,
  opts: {
    total?: number;
    isLoading?: boolean;
    error?: unknown;
  } = {},
) {
  mockUseAuditLog.mockReturnValue({
    data:
      items === undefined
        ? undefined
        : { items, limit: 50, offset: 0, total: opts.total ?? items.length },
    isLoading: opts.isLoading ?? false,
    error: opts.error ?? null,
  });
  return render(createElement(AdminAuditLog));
}

// ─── Tests ──────────────────────────────────────────────────────────────────

describe("AdminAuditLog", () => {
  it("renders the heading and description", () => {
    renderPage([ROW_1]);
    expect(screen.getByRole("heading", { name: /audit log/i })).toBeTruthy();
    expect(screen.getByTestId("audit-log-event-filter")).toBeTruthy();
  });

  it("renders rows with time/event/user_id/ip/detail", () => {
    renderPage([ROW_1, ROW_2]);
    const table = screen.getByTestId("admin-audit-log-table");
    expect(table).toBeTruthy();
    const row1 = screen.getByTestId(`audit-log-row-${String(ROW_1.id)}`);
    expect(row1.textContent).toContain("auth.login.success");
    expect(row1.textContent).toContain("42");
    expect(row1.textContent).toContain("127.0.0.1");
    // null user_id / ip / detail render as em dash.
    const row2 = screen.getByTestId(`audit-log-row-${String(ROW_2.id)}`);
    expect(row2.textContent).toContain("admin.user.deleted");
    expect(row2.textContent).toContain("—");
  });

  it("shows pagination status with the total count", () => {
    renderPage([ROW_1], { total: 120 });
    const status = screen.getByTestId("audit-log-page-status");
    expect(status.textContent).toMatch(/page 1 of 3/i);
    expect(status.textContent).toContain("120 total");
  });

  it("disables Previous on the first page and enables Next when more pages remain", () => {
    renderPage([ROW_1], { total: 120 });
    expect(
      (screen.getByTestId("audit-log-prev-btn") as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(
      (screen.getByTestId("audit-log-next-btn") as HTMLButtonElement).disabled,
    ).toBe(false);
  });

  it("disables Next on the last page", () => {
    renderPage([ROW_1], { total: 1 });
    expect(
      (screen.getByTestId("audit-log-next-btn") as HTMLButtonElement).disabled,
    ).toBe(true);
  });

  it("Next click re-queries with the next offset", () => {
    renderPage([ROW_1], { total: 120 });
    fireEvent.click(screen.getByTestId("audit-log-next-btn"));
    const lastCall = mockUseAuditLog.mock.calls[
      mockUseAuditLog.mock.calls.length - 1
    ]?.[0] as { limit: number; offset: number };
    expect(lastCall).toEqual({ limit: 50, offset: 50, event: undefined });
  });

  it("submitting the filter form applies the trimmed event and resets to page 0", () => {
    renderPage([ROW_1], { total: 120 });
    fireEvent.click(screen.getByTestId("audit-log-next-btn")); // move to page 2 first
    const input = screen.getByTestId(
      "audit-log-event-filter",
    ) as HTMLInputElement;
    fireEvent.change(input, {
      target: { value: "  auth.login.success  " },
    });
    fireEvent.submit(screen.getByTestId("audit-log-filter-btn").closest("form")!);
    const lastCall = mockUseAuditLog.mock.calls[
      mockUseAuditLog.mock.calls.length - 1
    ]?.[0] as { limit: number; offset: number; event: string | undefined };
    expect(lastCall).toEqual({
      limit: 50,
      offset: 0,
      event: "auth.login.success",
    });
    expect(screen.getByTestId("audit-log-clear-btn")).toBeTruthy();
  });

  it("Clear resets the filter", () => {
    renderPage([ROW_1], { total: 120 });
    const input = screen.getByTestId(
      "audit-log-event-filter",
    ) as HTMLInputElement;
    fireEvent.change(input, { target: { value: "auth.login.success" } });
    fireEvent.submit(screen.getByTestId("audit-log-filter-btn").closest("form")!);
    fireEvent.click(screen.getByTestId("audit-log-clear-btn"));
    expect(input.value).toBe("");
    const lastCall = mockUseAuditLog.mock.calls[
      mockUseAuditLog.mock.calls.length - 1
    ]?.[0] as { event: string | undefined };
    expect(lastCall.event).toBeUndefined();
    expect(screen.queryByTestId("audit-log-clear-btn")).toBeNull();
  });

  it("shows an error banner when the query fails", () => {
    renderPage(undefined, { error: { detail: "boom", message: "fallback" } });
    const banner = screen.getByTestId("audit-log-error");
    expect(banner.textContent).toMatch(/boom/);
  });

  it("shows the empty state when there are no rows", () => {
    renderPage([], { total: 0 });
    expect(screen.getByText(/no audit log entries found/i)).toBeTruthy();
    expect(screen.queryByTestId("admin-audit-log-table")).toBeNull();
    expect(screen.queryByTestId("audit-log-pagination")).toBeNull();
  });
});
