/**
 * Unit tests for P13k AdminUsers page.
 *
 * Tests:
 * - renders heading + Invite admin button
 * - shows error banner when query fails
 * - renders user rows with role badge + created_at + last_login
 * - promote button triggers useSetUserRole with isAdmin=true
 * - demote button is shown for admins and triggers isAdmin=false
 * - revoke-sessions button triggers useRevokeUserSessions
 * - delete flow: first click reveals confirm; confirm triggers useDeleteUser
 * - delete + demote disabled for the signed-in admin
 * - Invite admin opens the modal with the issued token + copy button
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, render, screen, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

// ─── Mocks ──────────────────────────────────────────────────────────────────

interface MockAdminUsersResult {
  data: AdminUser[] | undefined;
  isLoading: boolean;
  error: { detail: string; message: string } | null;
}

const mockUseAdminUsers = vi.fn<() => MockAdminUsersResult>();
const mockSetRoleMutate = vi.fn();
const mockRevokeMutate = vi.fn();
const mockDeleteMutate = vi.fn();
const mockInviteMutate = vi.fn();

vi.mock("@/hooks/useAdminUsers", () => ({
  useAdminUsers: () => mockUseAdminUsers(),
  useSetUserRole: () => ({ mutate: mockSetRoleMutate, isPending: false }),
  useRevokeUserSessions: () => ({ mutate: mockRevokeMutate, isPending: false }),
  useDeleteUser: () => ({ mutate: mockDeleteMutate, isPending: false }),
  useIssueAdminInvite: () => ({ mutate: mockInviteMutate, isPending: false }),
}));

const mockPush = vi.fn();
vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: mockPush }),
}));

// authStore: return a current-user object. id=99 → "currently signed-in admin".
vi.mock("@/stores/authStore", () => ({
  useAuthStore: (selector: (s: unknown) => unknown) =>
    selector({
      user: { id: 99, username: "me", is_admin: true },
      isInitializing: false,
    }),
}));

import AdminUsers from "@/pages/AdminUsers";
import type { AdminUser } from "@/hooks/useAdminUsers";

// ─── Fixtures ───────────────────────────────────────────────────────────────

const SELF_ADMIN: AdminUser = {
  id: 99,
  username: "me",
  is_admin: true,
  created_at: "2026-05-22T10:00:00Z",
  updated_at: "2026-05-22T10:00:00Z",
  last_login: "2026-05-22T11:00:00Z",
};

const OTHER_ADMIN: AdminUser = {
  id: 1,
  username: "alice",
  is_admin: true,
  created_at: "2026-05-01T10:00:00Z",
  updated_at: "2026-05-01T10:00:00Z",
  last_login: "2026-05-22T11:00:00Z",
};

const REGULAR: AdminUser = {
  id: 2,
  username: "bob",
  is_admin: false,
  created_at: "2026-05-10T10:00:00Z",
  updated_at: "2026-05-10T10:00:00Z",
  last_login: null,
};

beforeEach(() => {
  vi.clearAllMocks();
});

function renderPage(
  users: AdminUser[] | undefined,
  opts: { isLoading?: boolean; error?: { detail: string; message: string } | null } = {},
) {
  mockUseAdminUsers.mockReturnValue({
    data: users,
    isLoading: opts.isLoading ?? false,
    error: opts.error ?? null,
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    createElement(QueryClientProvider, { client: qc }, createElement(AdminUsers)),
  );
}

// ─── Tests ──────────────────────────────────────────────────────────────────

describe("AdminUsers", () => {
  it("renders the heading and Invite admin button", () => {
    renderPage([SELF_ADMIN, OTHER_ADMIN, REGULAR]);
    expect(screen.getByRole("heading", { name: /admin.*users/i })).toBeTruthy();
    expect(screen.getByTestId("invite-admin-btn")).toBeTruthy();
  });

  it("renders user rows with role badges and last_login", () => {
    renderPage([SELF_ADMIN, OTHER_ADMIN, REGULAR]);
    expect(screen.getByTestId("admin-users-table")).toBeTruthy();
    // Self + other admin → admin badge.
    expect(screen.getByTestId(`role-admin-${String(SELF_ADMIN.id)}`)).toBeTruthy();
    expect(screen.getByTestId(`role-admin-${String(OTHER_ADMIN.id)}`)).toBeTruthy();
    // Regular → user badge.
    expect(screen.getByTestId(`role-user-${String(REGULAR.id)}`)).toBeTruthy();
    // Last login renders an em dash for null.
    const regularRow = screen.getByTestId(`user-row-${String(REGULAR.id)}`);
    expect(regularRow.textContent).toContain("—");
  });

  it("shows error banner when the query fails", () => {
    renderPage(undefined, {
      error: { detail: "boom", message: "fallback" },
    });
    const banner = screen.getByTestId("users-error");
    expect(banner.textContent).toMatch(/boom/);
  });

  it("promote button triggers useSetUserRole with isAdmin=true", () => {
    renderPage([SELF_ADMIN, REGULAR]);
    const btn = screen.getByTestId(`promote-btn-${String(REGULAR.id)}`);
    fireEvent.click(btn);
    expect(mockSetRoleMutate).toHaveBeenCalledTimes(1);
    expect(mockSetRoleMutate).toHaveBeenCalledWith({
      userId: REGULAR.id,
      isAdmin: true,
    });
  });

  it("demote button is shown for admins and triggers isAdmin=false", () => {
    renderPage([SELF_ADMIN, OTHER_ADMIN]);
    const btn = screen.getByTestId(`demote-btn-${String(OTHER_ADMIN.id)}`);
    fireEvent.click(btn);
    expect(mockSetRoleMutate).toHaveBeenCalledWith({
      userId: OTHER_ADMIN.id,
      isAdmin: false,
    });
  });

  it("self-demote is disabled", () => {
    renderPage([SELF_ADMIN]);
    const btn = screen.getByTestId(
      `demote-btn-${String(SELF_ADMIN.id)}`,
    ) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("revoke-sessions button calls the mutation", () => {
    renderPage([SELF_ADMIN, REGULAR]);
    fireEvent.click(screen.getByTestId(`revoke-btn-${String(REGULAR.id)}`));
    expect(mockRevokeMutate).toHaveBeenCalledWith(REGULAR.id);
  });

  it("delete is two-step: first click reveals confirm; confirm calls mutation", () => {
    renderPage([SELF_ADMIN, REGULAR]);
    fireEvent.click(screen.getByTestId(`delete-btn-${String(REGULAR.id)}`));
    const confirm = screen.getByTestId(`delete-confirm-btn-${String(REGULAR.id)}`);
    expect(confirm).toBeTruthy();
    fireEvent.click(confirm);
    expect(mockDeleteMutate).toHaveBeenCalledTimes(1);
    expect(mockDeleteMutate.mock.calls[0]?.[0]).toBe(REGULAR.id);
  });

  it("self-delete is disabled", () => {
    renderPage([SELF_ADMIN]);
    const btn = screen.getByTestId(
      `delete-btn-${String(SELF_ADMIN.id)}`,
    ) as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("Invite admin opens the modal with the issued token", () => {
    renderPage([SELF_ADMIN]);
    fireEvent.click(screen.getByTestId("invite-admin-btn"));
    expect(mockInviteMutate).toHaveBeenCalledTimes(1);
    // Simulate the onSuccess callback that the page wires up.
    const onSuccess = (
      mockInviteMutate.mock.calls[0]?.[1] as
        | { onSuccess?: (p: { token: string; expires_at: string }) => void }
        | undefined
    )?.onSuccess;
    expect(onSuccess).toBeTruthy();
    act(() => {
      onSuccess?.({ token: "tok_abc_xyz", expires_at: "2026-05-23T11:00:00Z" });
    });
    // The modal should now be visible.
    expect(screen.getByTestId("invite-modal")).toBeTruthy();
    const linkBox = screen.getByTestId("invite-token-link");
    expect(linkBox.textContent).toMatch(/tok_abc_xyz/);
    expect(linkBox.textContent).toMatch(/\/register\?token=/);
  });
});
