/* SPDX-License-Identifier: Apache-2.0 */
/**
 * RequireUnauth — guard for routes that ONLY make sense for an
 * unauthenticated visitor (`/login`, `/register`).
 *
 * Without this guard, an already-signed-in user could navigate to
 * `/register` and silently swap into a new identity (the audit caught
 * Register re-registering + auto-logging-in with zero confirmation).
 * `/login` while signed in would also serve a stale sign-in form that
 * either no-ops or invites credential confusion.
 *
 * On hit, redirects to `/` (or the last-known destination if a
 * `returnTo` query param is present and same-origin).
 */
import type { ReactNode } from "react";
import { Navigate, useSearchParams } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import { sanitizeReturnTo } from "@/lib/returnTo";

interface RequireUnauthProps {
  children: ReactNode;
}

export function RequireUnauth({ children }: RequireUnauthProps) {
  const user = useAuthStore((s) => s.user);
  const isInitializing = useAuthStore((s) => s.isInitializing);
  const [searchParams] = useSearchParams();

  // While /me hydration is in flight, render nothing — same approach
  // as RequireAuth, prevents a one-frame flash of the wrong shell.
  if (isInitializing) return null;

  if (user !== null) {
    const dest = sanitizeReturnTo(searchParams.get("returnTo")) ?? "/";
    return <Navigate to={dest} replace />;
  }

  return <>{children}</>;
}
