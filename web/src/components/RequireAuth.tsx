/* SPDX-License-Identifier: Apache-2.0 */
/**
 * RequireAuth — route guard for authenticated pages.
 *
 * Renders children only when a session exists. Unauthenticated visitors
 * are redirected to /login with the current path captured as `returnTo`
 * so they bounce back after sign-in.
 *
 * Without this, logging out from /settings (or any other authenticated
 * page) left the user staring at a re-rendered settings shell with a
 * sign-in banner — the page never unmounted. This wraps every
 * authenticated route so logout (or any other auth-loss event)
 * deterministically navigates to /login.
 */
import type { ReactNode } from "react";
import { useEffect, useRef } from "react";
import { Navigate } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import { useToastStore } from "@/stores/toastStore";

interface RequireAuthProps {
  children: ReactNode;
}

export function RequireAuth({ children }: RequireAuthProps) {
  const user = useAuthStore((s) => s.user);
  const isInitializing = useAuthStore((s) => s.isInitializing);
  const needsSetup = useAuthStore((s) => s.needsSetup);
  const wasAuthenticated = useRef<boolean>(false);

  // On first render where user is truthy, mark that we've been authenticated.
  // Ref mutation is fine during render — it's idempotent.
  if (user !== null && !wasAuthenticated.current) {
    wasAuthenticated.current = true;
  }

  // Push the session-expired toast after commit so it never fires during
  // render (avoids React strict-mode double-toast).
  useEffect(() => {
    if (user === null && wasAuthenticated.current) {
      useToastStore.getState().push({
        variant: "info",
        message: "Session expired — sign in again.",
      });
    }
  }, [user]);

  // Render nothing during /me hydration so we don't briefly bounce to
  // /login before the session cookie resolves.
  if (isInitializing) return null;

  if (user === null) {
    // Fresh install (zero users on the server): there is nothing to sign
    // into, so route straight to the registration wizard instead of
    // bouncing through /login. Mirrors Login.tsx's own setup_status
    // redirect, but avoids the visible /login waypoint entirely.
    if (needsSetup) {
      return <Navigate to="/register" replace />;
    }
    const here = window.location.pathname + window.location.search;
    const dest =
      here && here !== "/"
        ? `/login?returnTo=${encodeURIComponent(here)}`
        : "/login";
    return <Navigate to={dest} replace />;
  }

  return <>{children}</>;
}
