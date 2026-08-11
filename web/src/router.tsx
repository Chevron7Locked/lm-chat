/* SPDX-License-Identifier: Apache-2.0 */
/**
 * React Router v7 route configuration.
 *
 * Routes:
 *   /login            → Login (unauthenticated entry point)
 *   /register         → Register (unauthenticated; create-account form)
 *   /                 → Chat (redirect to /login if !user)
 *   /chats/:chatId    → Chat with loaded history
 *   /settings         → Settings (tabbed shell)
 *   /settings/:tab    → Settings (URL-synced tab segment)
 *   /memory           → Memory (standalone route; also available as right panel in Chat)
 *   /documents        → Documents (RAG document management)
 *   /analytics        → Analytics (token/day aggregation dashboard)
 *   /prompts          → PromptLibrary (reusable prompt presets)
 *   /projects         → Projects (all-projects landing page)
 *   /project/:id      → Project (single project view)
 *   /admin/quotas     → AdminQuotas (admin-only; RequireAdmin guards redirect to /)
 *   /admin/models     → AdminModels (admin-only; model lifecycle UI — load/unload)
 *   /admin/integrations → AdminIntegrations (admin-only; MCP integrations registry)
 *   /admin/users      → AdminUsers (admin-only; users + invite)
 *   /admin/audit-log  → AdminAuditLog (admin-only; paginated audit_log viewer)
 *   /share/:token     → SharePublic (UNAUTHENTICATED; read-only public chat)
 *
 * TanStack QueryClientProvider is mounted here so all routes share the cache.
 * ToastContainer is mounted once at the router root.
 *
 * Lazy loading: every authenticated route is lazily imported so the initial
 * login bundle stays small. Login is eager (small; the initial landing).
 */
import { lazy, Suspense, useEffect } from "react";
import {
  createBrowserRouter,
  createRoutesFromElements,
  RouterProvider,
  Route,
  Navigate,
  Outlet,
} from "react-router-dom";
import { QueryClientProvider } from "@tanstack/react-query";
import { queryClient } from "@/lib/queryClient";
import Login from "@/pages/Login";
import Register from "@/pages/Register";
import { ToastContainer } from "@/components/Toast";
import { RequireAdmin } from "@/components/RequireAdmin";
import { RequireAuth } from "@/components/RequireAuth";
import { RequireUnauth } from "@/components/RequireUnauth";
import { AppLayout } from "@/components/layouts/AppLayout";
import { ErrorBoundary } from "@/components/ErrorBoundary";
import { useAuthStore } from "@/stores/authStore";

// Chat + Settings are the two most-navigated surfaces — eager-importing
// them removes the lazy-chunk fetch on
// every first visit, which was rendering the full-viewport "Loading…"
// fallback and reading as a hard page reload. Less-common pages
// (Memory, Documents, Analytics, Prompts, Admin*) stay lazy because
// they're rarely hit.
import Chat from "@/pages/Chat";
import Settings from "@/pages/Settings";
const Memory = lazy(() => import("@/pages/Memory"));
const Documents = lazy(() => import("@/pages/Documents"));
const Analytics = lazy(() => import("@/pages/Analytics"));
const PromptLibrary = lazy(() => import("@/pages/PromptLibrary"));
const SetupLmStudio = lazy(() => import("@/pages/SetupLmStudio"));
const Project = lazy(() => import("@/pages/Project"));
const Projects = lazy(() => import("@/pages/Projects"));
const Help = lazy(() => import("@/pages/Help"));
const Docs = lazy(() => import("@/pages/Docs"));
const AdminQuotas = lazy(() => import("@/pages/AdminQuotas"));
const AdminModels = lazy(() => import("@/pages/AdminModels"));
const AdminIntegrations = lazy(() => import("@/pages/AdminIntegrations"));
const AdminUsers = lazy(() => import("@/pages/AdminUsers"));
const AdminAuditLog = lazy(() => import("@/pages/AdminAuditLog"));
// Admin pages render no chrome of their own; wrap them in the shared shell
// so they get the desktop sidebar + mobile home bar (back-to-chat exit).
const AppShell = lazy(() =>
  import("@/components/AppShell").then((m) => ({ default: m.AppShell })),
);
// Read-only public share view. UNAUTHENTICATED.
const SharePublic = lazy(() => import("@/pages/SharePublic"));

function Loading() {
  // Near-invisible placeholder. The previous full-viewport "Loading…"
  // word made every lazy-chunk fetch read as a hard page reload.
  // Holding the canvas color with no
  // copy lets cached transitions feel instant; uncached chunks just
  // flash the same color the page background was already painting.
  return (
    <div
      aria-hidden="true"
      style={{
        minHeight: "100dvh",
        background: "var(--color-bg)",
      }}
    >
      {/* Hidden a11y label so screen readers still announce a route swap. */}
      <span
        role="status"
        aria-live="polite"
        style={{
          position: "absolute",
          width: 1,
          height: 1,
          margin: -1,
          padding: 0,
          border: 0,
          clip: "rect(0 0 0 0)",
          overflow: "hidden",
        }}
      >
        Loading…
      </span>
    </div>
  );
}

/** Runs authStore.refresh() exactly once on mount to hydrate from session
 * cookie via GET /api/auth/me. Must be inside QueryClientProvider so any
 * dependent hooks that fire after hydration have access to the query cache. */
function AuthHydrator() {
  const refresh = useAuthStore((s) => s.refresh);
  useEffect(() => {
    void refresh();
  }, []); // only once on mount — refresh is stable (Zustand action)
  return null;
}

/** Root layout: hydrate auth, wrap all routes in the error boundary +
 *  lazy-load Suspense, and mount the toast container once. Rendered as
 *  the parent route so it persists across navigations. */
function RootLayout() {
  return (
    <>
      {/* WCAG 2.4.1 (Bypass Blocks) — satisfied via the <main id="main-content">
          landmark in AppShell + page-level <main> elements; modern AT navigates
          by landmark rather than a visible skip-link. The explicit skip-link
          was removed because it surfaced false-positively on iOS Safari touch
          (the skip-link received tap events and appeared as a visible button). */}
      <AuthHydrator />
      <ErrorBoundary label="this page">
        <Suspense fallback={<Loading />}>
          <Outlet />
        </Suspense>
      </ErrorBoundary>
      <ToastContainer />
    </>
  );
}

// Data router (createBrowserRouter) — required so useBlocker works for
// the unsaved-changes guard in LmStudioSection. Route tree unchanged;
// just hosted under a RootLayout parent + the data-router runtime.
const router = createBrowserRouter(
  createRoutesFromElements(
    <Route element={<RootLayout />}>
      {/* Unauthenticated-only: signed-in users get redirected to home
          (or their sanitized returnTo). Stops silent identity swap via
          re-registering and stops a stale sign-in form rendering for
          already-logged-in users. */}
      <Route
        path="/login"
        element={
          <RequireUnauth>
            <Login />
          </RequireUnauth>
        }
      />
      <Route
        path="/register"
        element={
          <RequireUnauth>
            <Register />
          </RequireUnauth>
        }
      />
      {/* Unauthenticated read-only share view. */}
      <Route path="/share/:token" element={<SharePublic />} />
      <Route
        path="/"
        element={
          <RequireAuth>
            <Chat />
          </RequireAuth>
        }
      />
      <Route
        path="/chats/:chatId"
        element={
          <RequireAuth>
            <Chat />
          </RequireAuth>
        }
      />
      {/* AppLayout parent: Sidebar lives ONCE here and persists across
          every navigation between these routes — no remount, no
          collapse-state reset, no "page reload feel". Each child route
          renders only its main column. Chat (/, /chats/:id) is
          intentionally excluded because its mobile drawer + topbar are
          deeply intertwined with chat-specific state; lifting those
          into AppLayout is a separate refactor. */}
      <Route
        element={
          <RequireAuth>
            <AppLayout />
          </RequireAuth>
        }
      >
        <Route path="/settings" element={<Settings />} />
        <Route path="/settings/:tab" element={<Settings />} />
        <Route path="/memory" element={<Memory />} />
        <Route path="/documents" element={<Documents />} />
        <Route path="/analytics" element={<Analytics />} />
        <Route path="/prompts" element={<PromptLibrary />} />
        <Route
          path="/setup/lm-studio"
          element={
            <RequireAdmin>
              <SetupLmStudio />
            </RequireAdmin>
          }
        />
        <Route path="/help" element={<Help />} />
        <Route path="/docs" element={<Docs />} />
        <Route path="/docs/:slug" element={<Docs />} />
        {/* Project view page. */}
        <Route path="/project/:id" element={<Project />} />
        {/* All-projects landing page. */}
        <Route path="/projects" element={<Projects />} />
      </Route>
      <Route
        element={
          <RequireAdmin>
            <AppLayout />
          </RequireAdmin>
        }
      >
        <Route
          path="/admin/quotas"
          element={
            <AppShell>
              <AdminQuotas />
            </AppShell>
          }
        />
        <Route
          path="/admin/models"
          element={
            <AppShell>
              <AdminModels />
            </AppShell>
          }
        />
        <Route
          path="/admin/integrations"
          element={
            <AppShell>
              <AdminIntegrations />
            </AppShell>
          }
        />
        <Route
          path="/admin/users"
          element={
            <AppShell>
              <AdminUsers />
            </AppShell>
          }
        />
        <Route
          path="/admin/audit-log"
          element={
            <AppShell>
              <AdminAuditLog />
            </AppShell>
          }
        />
      </Route>
      {/* Catch-all: redirect unknown paths to home. */}
      <Route path="*" element={<Navigate to="/" replace />} />
    </Route>,
  ),
);

export function AppRouter() {
  return (
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  );
}
