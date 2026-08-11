/* SPDX-License-Identifier: Apache-2.0 */
/**
 * RequireAdmin — route guard for admin-only pages.
 *
 * Renders children only when the authenticated user has `is_admin === true`.
 *
 * Unauthenticated visitors still redirect to
 * /login (so they can come back authenticated), but non-admin authenticated
 * users now see an in-place "Admin access required" page instead of being
 * silently bounced to the Chat home.  A silent redirect+toast had no way to
 * signal that the bounce was a deliberate guard rather than a bug; a
 * dedicated page surface makes the gate self-explanatory and gives the
 * user a route back.
 */
import type { CSSProperties, ReactNode } from "react";
import { Link, Navigate } from "react-router-dom";
import { Lock } from "lucide-react";
import { useAuthStore } from "@/stores/authStore";

interface RequireAdminProps {
  children: ReactNode;
}

export function RequireAdmin({ children }: RequireAdminProps) {
  const user = useAuthStore((s) => s.user);
  const isInitializing = useAuthStore((s) => s.isInitializing);

  // While the /me hydration is in-flight, render nothing to avoid a flash
  // of the access-required page before the session cookie is resolved.
  if (isInitializing) return null;

  // Anonymous: kick to login with the current URL as returnTo so they
  // bounce back to the admin page after sign-in.
  if (user === null) {
    const here = window.location.pathname + window.location.search;
    const dest =
      here && here !== "/"
        ? `/login?returnTo=${encodeURIComponent(here)}`
        : "/login";
    return <Navigate to={dest} replace />;
  }

  // Authenticated but not admin: show an explicit gate page.
  if (!user.is_admin) {
    return (
      <div style={pageStyle} data-testid="require-admin-denied">
        <div style={cardStyle}>
          <div style={iconStyle} aria-hidden="true">
            <Lock size={32} aria-hidden />
          </div>
          <h1 style={titleStyle}>Admin access required</h1>
          <p style={copyStyle}>
            This area is restricted to administrators. You're signed in as{" "}
            <strong>{user.username}</strong>, which does not have admin
            permissions on this deployment.
          </p>
          <p style={copyStyle}>
            Contact your administrator if you believe you should have access, or
            head back to the chat.
          </p>
          <Link to="/" style={linkBtnStyle} data-testid="require-admin-back">
            ← Back to chat
          </Link>
        </div>
      </div>
    );
  }

  return <>{children}</>;
}

const pageStyle: CSSProperties = {
  minHeight: "100dvh",
  display: "grid",
  placeItems: "center",
  padding: "var(--space-chapter) var(--space-group)",
  background: "var(--color-bg)",
};

const cardStyle: CSSProperties = {
  maxWidth: 480,
  width: "100%",
  padding: "var(--space-chapter)",
  background: "var(--color-surface)",
  border: "1px solid var(--color-border)",
  borderRadius: "var(--radius-lg)",
  boxShadow: "var(--shadow-md)",
  display: "flex",
  flexDirection: "column",
  alignItems: "center",
  gap: "var(--space-sibling-relaxed)",
  textAlign: "center",
};

const iconStyle: CSSProperties = {
  fontSize: "2.5rem",
  lineHeight: 1,
};

const titleStyle: CSSProperties = {
  margin: 0,
  fontSize: "1.25rem",
  fontFamily: "var(--font-display)",
  fontWeight: 600,
  color: "var(--color-text)",
};

const copyStyle: CSSProperties = {
  margin: 0,
  fontSize: "0.875rem",
  color: "var(--color-text-muted)",
  lineHeight: 1.5,
};

const linkBtnStyle: CSSProperties = {
  marginTop: "var(--space-glue-relaxed)",
  padding: "8px var(--space-group)",
  background: "var(--color-accent)",
  color: "var(--color-text-on-accent)",
  border: "none",
  borderRadius: "var(--radius-md)",
  fontSize: "0.9375rem",
  fontWeight: 600,
  textDecoration: "none",
};
