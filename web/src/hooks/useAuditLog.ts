/* SPDX-License-Identifier: Apache-2.0 */
/**
 * TanStack Query hook for the admin audit-log viewer.
 *
 * Route:
 *   GET /api/admin/audit-log?limit=&offset=&event= → AuditLogPage
 *
 * Backend lives at src/lmchat/routes/admin.py (get_audit_log). Rows are
 * ordered by created_at descending; `event` is an optional exact-match
 * filter on the AuditEvent taxonomy string. `limit` defaults to 50 and is
 * clamped to 200 server-side.
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";
import type { components } from "@/types/api";

// ─── Wire shapes (generated) ─────────────────────────────────────────────────

export type AuditLogRow = components["schemas"]["AuditLogResponse"];
export type AuditLogPage = components["schemas"]["AuditLogPage"];

export interface AuditLogParams {
  limit: number;
  offset: number;
  /** Optional exact-match filter on the `event` column; "" / undefined = all events. */
  event?: string | undefined;
}

// ─── Query keys ──────────────────────────────────────────────────────────────

const auditLogKeys = {
  all: ["admin", "audit-log"] as const,
  list: (params: AuditLogParams) =>
    [...auditLogKeys.all, "list", params] as const,
};

// ─── Queries ─────────────────────────────────────────────────────────────────

/**
 * Admin: fetch one page of audit_log rows.
 *
 * Enabled only for admin users. `params` drives both the query key and the
 * request query string, so paging or changing the event filter refetches
 * automatically. `placeholderData` keeps the current page's rows on screen
 * while the next page loads instead of flashing to the loading state.
 */
export function useAuditLog(params: AuditLogParams) {
  const user = useAuthStore((s) => s.user);
  const isInitializing = useAuthStore((s) => s.isInitializing);
  const enabled =
    !isInitializing &&
    user !== null &&
    (user as { is_admin?: boolean }).is_admin === true;

  return useQuery<AuditLogPage, ApiError>({
    queryKey: auditLogKeys.list(params),
    queryFn: () => {
      const qs = new URLSearchParams();
      qs.set("limit", String(params.limit));
      qs.set("offset", String(params.offset));
      if (params.event !== undefined && params.event !== "") {
        qs.set("event", params.event);
      }
      return api.request<AuditLogPage>(
        `/api/admin/audit-log?${qs.toString()}`,
      );
    },
    enabled,
    placeholderData: (prev) => prev,
  });
}
