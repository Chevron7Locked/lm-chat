/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useAnalytics — TanStack Query hooks for the analytics API surface.
 */
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";

// ─── Response shapes ─────────────────────────────────────────────────────────

export interface TopModel {
  model_id: string;
  count: number;
}

export interface DayCount {
  day: string;
  count: number;
}

export interface UserAnalytics {
  total_messages: number;
  total_chats: number;
  messages_last_7_days: number;
  top_models: TopModel[];
  messages_by_day: DayCount[];
}

export interface SystemAnalytics {
  total_users: number;
  total_chats: number;
  total_messages: number;
  messages_last_7_days: number;
  top_models: TopModel[];
}

// ─── Query keys ──────────────────────────────────────────────────────────────

const analyticsKeys = {
  me: ["analytics", "me"] as const,
  system: ["analytics", "system"] as const,
};

// ─── Hooks ───────────────────────────────────────────────────────────────────

/**
 * Fetch per-user analytics.
 *
 * GET /api/analytics/me — auth-gated.
 * Gated on !isInitializing to suppress 401 spam during mount hydration.
 */
export function useMyAnalytics() {
  const { isInitializing, user } = useAuthStore();
  return useQuery<UserAnalytics, ApiError>({
    queryKey: analyticsKeys.me,
    queryFn: () => api.request<UserAnalytics>("/api/analytics/me"),
    enabled: !isInitializing && user !== null,
  });
}

/**
 * Fetch system-wide analytics.
 *
 * GET /api/analytics/system — admin-only.
 * Only fires when the user is an admin (`is_admin === true`).
 */
export function useSystemAnalytics() {
  const { isInitializing, user } = useAuthStore();
  const isAdmin = user?.is_admin === true;
  return useQuery<SystemAnalytics, ApiError>({
    queryKey: analyticsKeys.system,
    queryFn: () => api.request<SystemAnalytics>("/api/analytics/system"),
    enabled: !isInitializing && user !== null && isAdmin,
  });
}
