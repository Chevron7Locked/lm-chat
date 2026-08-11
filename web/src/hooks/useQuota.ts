/* SPDX-License-Identifier: Apache-2.0 */
/**
 * TanStack Query hooks for the quota API surface.
 *
 * Routes:
 *   GET /api/quotas/me              → QuotaResponse (current user)
 *   GET /api/admin/quotas           → QuotaSummary[] (admin only)
 *   PATCH /api/admin/quotas/{uid}   → QuotaSummary  (admin only)
 */
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";
import { useToast } from "@/stores/toastStore";

// ─── Wire shapes ─────────────────────────────────────────────────────────────

export interface QuotaResponse {
  tokens_per_day: number;
  requests_per_day: number;
  tokens_consumed_today: number;
  requests_consumed_today: number;
  /** ISO-8601 datetime when the quota resets (next UTC midnight). */
  resets_at: string;
}

export interface QuotaSummary {
  user_id: number;
  tokens_per_day: number;
  requests_per_day: number;
}

// ─── Query keys ──────────────────────────────────────────────────────────────

export const quotaKeys = {
  all: ["quotas"] as const,
  me: () => [...quotaKeys.all, "me"] as const,
  adminList: () => [...quotaKeys.all, "admin", "list"] as const,
};

// ─── Hooks ───────────────────────────────────────────────────────────────────

/**
 * Fetch the current user's quota and today's usage.
 *
 * Enabled only when the user is authenticated.
 *
 * On a 429 response from any API call, the global API client throws an
 * ApiError with status 429.  The caller (Settings page) surfaces this
 * via the toast system.
 */
export function useQuotaMe() {
  // Use separate primitive selectors to avoid creating a new object on every
  // render.  TanStack Query re-renders the component whenever the query result
  // changes; if the selector returned a new object each call, React would see
  // a fresh reference on every render and trigger an infinite update loop.
  const user = useAuthStore((s) => s.user);
  const isInitializing = useAuthStore((s) => s.isInitializing);
  const enabled = !isInitializing && user !== null;

  return useQuery<QuotaResponse, ApiError>({
    queryKey: quotaKeys.me(),
    queryFn: () => api.request<QuotaResponse>("/api/quotas/me"),
    enabled,
    staleTime: 30_000, // 30 seconds
  });
}

/**
 * Admin: fetch all users' quota rows.
 *
 * Enabled only for admin users.  Returns a plain array (Invariant 3).
 */
export function useAdminQuotaList() {
  // Use separate primitive selectors — same stable-identity reason as useQuotaMe.
  const user = useAuthStore((s) => s.user);
  const isInitializing = useAuthStore((s) => s.isInitializing);
  const enabled =
    !isInitializing &&
    user !== null &&
    (user as { is_admin?: boolean }).is_admin === true;

  return useQuery<QuotaSummary[], ApiError>({
    queryKey: quotaKeys.adminList(),
    queryFn: () => api.request<QuotaSummary[]>("/api/admin/quotas"),
    enabled,
  });
}

// ─── Mutation input shape ────────────────────────────────────────────────────

export interface UpdateQuotaInput {
  userId: number;
  tokensPerDay: number;
  requestsPerDay: number;
}

/**
 * Admin: update a user's quota limits.
 *
 * On success, invalidates the admin quota list.
 * On error, surfaces a toast.
 */
export function useUpdateQuota() {
  const qc = useQueryClient();
  const { push } = useToast();

  return useMutation<QuotaSummary, ApiError, UpdateQuotaInput>({
    mutationFn: ({ userId, tokensPerDay, requestsPerDay }) =>
      api.postForm<QuotaSummary>(`/api/admin/quotas/${String(userId)}`, {
        tokens_per_day: String(tokensPerDay),
        requests_per_day: String(requestsPerDay),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: quotaKeys.adminList() });
    },
    onError: (err) => {
      push({
        variant: "error",
        message: err.detail ?? "Couldn't update the quota — try again.",
      });
    },
  });
}
