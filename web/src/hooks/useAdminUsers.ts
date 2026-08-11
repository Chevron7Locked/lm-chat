/* SPDX-License-Identifier: Apache-2.0 */
/**
 * TanStack Query hooks for the admin-users surface.
 *
 * Routes:
 *   GET    /api/admin/users                            → AdminUser[]
 *   POST   /api/admin/users/{user_id}/role             → AdminUser
 *   POST   /api/admin/users/{user_id}/revoke-sessions  → {status: "ok"}
 *   DELETE /api/admin/users/{user_id}                  → {status: "ok"}
 *   POST   /api/admin/invite                           → AdminInvite
 *
 * Backend lives at src/lmchat/routes/admin.py + routes/auth.py
 * (register_endpoint accepts the invite token via ?token= / X-Setup-Token).
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";
import { useToast } from "@/stores/toastStore";

// ─── Wire shapes ─────────────────────────────────────────────────────────────

export interface AdminUser {
  id: number;
  username: string;
  is_admin: boolean;
  created_at: string;
  updated_at: string;
  /** ISO-8601 datetime of the most recent auth.login.success row in the
   *  audit_log table, or null if the user has never successfully logged in. */
  last_login: string | null;
}

export interface AdminInvite {
  token: string;
  expires_at: string;
}

// ─── Query keys ──────────────────────────────────────────────────────────────

const adminUserKeys = {
  all: ["admin", "users"] as const,
  list: () => [...adminUserKeys.all, "list"] as const,
};

// ─── Queries ─────────────────────────────────────────────────────────────────

/**
 * Admin: fetch all users.
 *
 * Enabled only for admin users.  Returns a plain array (Invariant 3).
 */
export function useAdminUsers() {
  const user = useAuthStore((s) => s.user);
  const isInitializing = useAuthStore((s) => s.isInitializing);
  const enabled =
    !isInitializing &&
    user !== null &&
    (user as { is_admin?: boolean }).is_admin === true;

  return useQuery<AdminUser[], ApiError>({
    queryKey: adminUserKeys.list(),
    queryFn: () => api.request<AdminUser[]>("/api/admin/users?limit=200"),
    enabled,
  });
}

// ─── Mutations ───────────────────────────────────────────────────────────────

export interface SetRoleInput {
  userId: number;
  isAdmin: boolean;
}

/** Admin: promote or demote a user.  Invalidates the user list on success. */
export function useSetUserRole() {
  const qc = useQueryClient();
  const { push } = useToast();

  return useMutation<AdminUser, ApiError, SetRoleInput>({
    mutationFn: ({ userId, isAdmin }) =>
      api.postForm<AdminUser>(`/api/admin/users/${String(userId)}/role`, {
        is_admin: isAdmin ? "true" : "false",
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: adminUserKeys.list() });
    },
    onError: (err) => {
      push({
        variant: "error",
        message: err.detail ?? "Couldn't update that user's role — try again.",
      });
    },
  });
}

/** Admin: revoke all sessions for a user.  No cache invalidation required —
 *  the user list doesn't expose session state. */
export function useRevokeUserSessions() {
  const { push } = useToast();

  return useMutation<{ status: string }, ApiError, number>({
    mutationFn: (userId) =>
      api.postForm<{ status: string }>(
        `/api/admin/users/${String(userId)}/revoke-sessions`,
        {},
      ),
    onSuccess: () => {
      push({ variant: "success", message: "Sessions revoked." });
    },
    onError: (err) => {
      push({
        variant: "error",
        message: err.detail ?? "Couldn't revoke those sessions — try again.",
      });
    },
  });
}

/** Admin: permanently delete a user.  Invalidates the user list on success. */
export function useDeleteUser() {
  const qc = useQueryClient();
  const { push } = useToast();

  return useMutation<{ status: string }, ApiError, number>({
    mutationFn: (userId) =>
      api.request<{ status: string }>(`/api/admin/users/${String(userId)}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: adminUserKeys.list() });
      push({ variant: "success", message: "User deleted." });
    },
    onError: (err) => {
      push({
        variant: "error",
        message: err.detail ?? "Couldn't delete that user — try again.",
      });
    },
  });
}

/** Admin: issue a one-shot invite token (registrant becomes admin). */
export function useIssueAdminInvite() {
  const { push } = useToast();

  return useMutation<AdminInvite, ApiError>({
    mutationFn: () =>
      api.request<AdminInvite>("/api/admin/invite", { method: "POST" }),
    onError: (err) => {
      push({
        variant: "error",
        message: err.detail ?? "Couldn't issue an invite — try again.",
      });
    },
  });
}
