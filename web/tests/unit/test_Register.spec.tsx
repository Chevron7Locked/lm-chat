/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Register page unit tests — render + submit + error handling.
 *
 * Locked behaviours:
 *   - Renders username, password, and "confirm password" fields plus a
 *     "Create account" submit button and a back-to-sign-in link.
 *   - Submitting with matched passwords issues a form-encoded POST to
 *     `/api/auth/register` and navigates to `/login` on a 201 response.
 *   - Submitting with mismatched passwords surfaces an inline error
 *     ("Passwords do not match.") and does NOT call the API.
 *   - A 400 server response surfaces the server-supplied detail
 *     ("username already taken").
 *
 * The test mounts the page inside a MemoryRouter and stubs `fetch`
 * directly (the api client uses `fetch` under the hood). The authStore
 * module is reset between tests so each test gets a fresh instance.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

import Register from "@/pages/Register";

// Helper: re-import the module-level page + store after a module reset so
// the authStore singleton is fresh per test. Each fresh load also clears
// the default `isInitializing=true` flag so the form mounts immediately
// (the production AuthHydrator only flips that flag after the /me probe).
async function freshRegister() {
  vi.resetModules();
  const storeMod = await import("@/stores/authStore");
  storeMod.useAuthStore.setState({ isInitializing: false });
  const mod = await import("@/pages/Register");
  return mod.default;
}

function renderRegister(Page: typeof Register) {
  return render(
    <MemoryRouter initialEntries={["/register"]}>
      <Routes>
        <Route path="/register" element={<Page />} />
        <Route path="/login" element={<div data-testid="login-page">login</div>} />
      </Routes>
    </MemoryRouter>
  );
}

describe("Register", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    cleanup();
  });

  it("renders the create-account form", async () => {
    const Page = await freshRegister();
    renderRegister(Page);
    expect(screen.getByRole("heading", { name: "Create account" })).toBeTruthy();
    expect(screen.getByLabelText("Username")).toBeTruthy();
    expect(screen.getByLabelText("Password")).toBeTruthy();
    expect(screen.getByLabelText("Confirm password")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Create account" })).toBeTruthy();
    expect(screen.getByRole("link", { name: /Back to sign in/ })).toBeTruthy();
  });

  it("renders the bootstrap-admin callout above the form", async () => {
    // Backend auth_service.register() auto-promotes the first registered
    // user to admin.  The Register page gates the callout on
    // /api/auth/setup_status returning { needs_setup: true } (UX-AUDIT-r1
    // Wave 3-H: only show on a fresh deployment; users 2+ never see it).
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ needs_setup: true }), { status: 200 })
    );
    const Page = await freshRegister();
    renderRegister(Page);
    const note = await waitFor(() => screen.getByRole("note"));
    expect(note).toBeTruthy();
    expect(note.textContent).toContain("This account becomes the admin.");
  });

  it("submits a form-encoded POST and auto-logs-in on 201", async () => {
    // Register mounts /api/auth/setup_status on load, then issues
    // /api/auth/register, then auto-logs-in via /api/auth/login so the
    // user doesn't have to retype creds. Mock all three.
    global.fetch = vi.fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ needs_setup: false }), { status: 200 })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ id: 1, username: "alice" }), { status: 201 })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ id: 1, username: "alice", is_admin: false }), { status: 200 })
      );

    const Page = await freshRegister();
    renderRegister(Page);

    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "hunter2pass" } });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "hunter2pass" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    // Non-bootstrap-admin: lands on the home shell rather than /login.
    await waitFor(() => {
      expect(vi.mocked(global.fetch)).toHaveBeenCalledTimes(3);
    });

    // Verify the register POST was form-encoded with username + password.
    // Fetch call 0 = setup_status; call 1 = /api/auth/register; call 2 = /api/auth/login.
    const registerCall = vi.mocked(global.fetch).mock.calls[1];
    expect(registerCall?.[0]).toBe("/api/auth/register");
    const init = registerCall?.[1] as RequestInit | undefined;
    expect(init?.method).toBe("POST");
    const headers = init?.headers as Record<string, string> | undefined;
    expect(headers?.["Content-Type"]).toBe("application/x-www-form-urlencoded");
    const body = init?.body as string | undefined;
    expect(body).toContain("username=alice");
    expect(body).toContain("password=hunter2pass");

    const loginCall = vi.mocked(global.fetch).mock.calls[2];
    expect(loginCall?.[0]).toBe("/api/auth/login");
  });

  it("rejects mismatched passwords without hitting the API", async () => {
    // setup_status is fetched on mount; stub it so the component renders fully.
    // The register endpoint must NOT be called when passwords don't match.
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ needs_setup: false }), { status: 200 })
    );

    const Page = await freshRegister();
    renderRegister(Page);

    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "first" } });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "second" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain("Passwords do not match.");
    });
    // setup_status was called on mount (expected); only that call should have occurred.
    // The register endpoint (/api/auth/register) must NOT have been called.
    const registerCalls = vi.mocked(global.fetch).mock.calls.filter(
      (c) => typeof c[0] === "string" && c[0].includes("/api/auth/register"),
    );
    expect(registerCalls).toHaveLength(0);
  });

  it("surfaces the server detail on 400 (username taken)", async () => {
    // setup_status on mount first, then the 400 from the register POST.
    global.fetch = vi.fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ needs_setup: false }), { status: 200 })
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "username already taken" }), { status: 400 })
      );

    const Page = await freshRegister();
    renderRegister(Page);

    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "pw12345678" } });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "pw12345678" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Create account" }));

    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain("username already taken");
    });
    // Did not navigate to /login.
    expect(screen.queryByTestId("login-page")).toBeNull();
  });
});
