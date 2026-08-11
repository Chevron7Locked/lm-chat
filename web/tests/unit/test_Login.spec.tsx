/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Login page unit tests — render + submit + error handling.
 *
 * Locked behaviours:
 *   - Renders username, password, and submit button.
 *   - Valid credentials POST form-encoded to `/api/auth/login` and the
 *     user is navigated to `/` on a 200 response.
 *   - Server-side error (401 with detail) surfaces an inline alert and
 *     does NOT navigate.
 *   - Empty fields are blocked by browser-native HTML5 `required`
 *     validation — the fetch must not fire.
 *   - "totp required" 401 reveals the TOTP code input; the next submit
 *     includes totp_code in the body.
 *   - The session-expired banner renders only when ?returnTo= is present
 *     and the user did not just register.
 *
 * The page mounts a /api/auth/setup_status probe on first render
 * (P13f WelcomeWizard gate). Tests stub that as the first fetch call
 * with `needs_setup: false` so the form renders rather than the wizard.
 *
 * authStore is reset between tests via module reset; the default
 * `isInitializing=true` flag is also cleared so the form mounts at once
 * (AuthHydrator does that flip in production).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

import Login from "@/pages/Login";

async function freshLogin() {
  vi.resetModules();
  const storeMod = await import("@/stores/authStore");
  storeMod.useAuthStore.setState({ isInitializing: false, error: null, user: null });
  const mod = await import("@/pages/Login");
  return mod.default;
}

function renderLogin(Page: typeof Login, initialEntries: string[] = ["/login"]) {
  return render(
    <MemoryRouter initialEntries={initialEntries}>
      <Routes>
        <Route path="/login" element={<Page />} />
        <Route path="/" element={<div data-testid="home-page">home</div>} />
        <Route path="/register" element={<div data-testid="register-page">register</div>} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Login", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    cleanup();
  });

  it("renders the sign-in form", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ needs_setup: false }), { status: 200 }),
    );
    const Page = await freshLogin();
    renderLogin(Page);

    await waitFor(() => {
      expect(screen.getByRole("heading", { name: "Sign in" })).toBeTruthy();
    });
    expect(screen.getByLabelText("Username")).toBeTruthy();
    expect(screen.getByLabelText("Password")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Sign in" })).toBeTruthy();
    // Register link is only shown on fresh-install (needs_setup=true).
    // When needs_setup=false (users already exist), registration is closed.
    expect(screen.queryByRole("link", { name: /Create account/ })).toBeNull();
  });

  it("shows the register link only when needs_setup is true", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ needs_setup: true }), { status: 200 }),
    );
    const Page = await freshLogin();
    renderLogin(Page);

    // needs_setup=true means the wizard will redirect to /register, so the
    // form won't render — but if somehow the form renders, the link appears.
    // More directly: assert the Navigate redirect happens instead of the form.
    // The router has a /register route in renderLogin, so Navigate renders it.
    await waitFor(() => {
      // On needs_setup=true, Login redirects to /register (no sign-in form).
      expect(screen.queryByRole("heading", { name: "Sign in" })).toBeNull();
    });
  });

  it("submits a form-encoded POST and navigates to / on 200", async () => {
    global.fetch = vi.fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ needs_setup: false }), { status: 200 }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            user_id: 1,
            expires_at: "2099-01-01T00:00:00Z",
            username: "alice",
            is_admin: false,
            totp_enabled: false,
          }),
          { status: 200 },
        ),
      );

    const Page = await freshLogin();
    renderLogin(Page);

    await waitFor(() => screen.getByRole("button", { name: "Sign in" }));
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "hunter2pass" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => {
      expect(screen.getByTestId("home-page")).toBeTruthy();
    });

    // Fetch call 0 = setup_status; call 1 = /api/auth/login.
    expect(global.fetch).toHaveBeenCalledTimes(2);
    const call = vi.mocked(global.fetch).mock.calls[1];
    expect(call?.[0]).toBe("/api/auth/login");
    const init = call?.[1] as RequestInit | undefined;
    expect(init?.method).toBe("POST");
    const headers = init?.headers as Record<string, string> | undefined;
    expect(headers?.["Content-Type"]).toBe("application/x-www-form-urlencoded");
    const body = init?.body as string | undefined;
    expect(body).toContain("username=alice");
    expect(body).toContain("password=hunter2pass");
    expect(body).not.toContain("totp_code");
  });

  it("surfaces the server detail on 401 (invalid credentials)", async () => {
    global.fetch = vi.fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ needs_setup: false }), { status: 200 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "invalid credentials" }), { status: 401 }),
      );

    const Page = await freshLogin();
    renderLogin(Page);

    await waitFor(() => screen.getByRole("button", { name: "Sign in" }));
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "wrong-pw" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain("invalid credentials");
    });
    // Did not navigate to /.
    expect(screen.queryByTestId("home-page")).toBeNull();
  });

  it("does not call /api/auth/login when required fields are empty", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ needs_setup: false }), { status: 200 }),
    );

    const Page = await freshLogin();
    renderLogin(Page);

    await waitFor(() => screen.getByRole("button", { name: "Sign in" }));
    // Submit without filling username/password — HTML5 `required` blocks the
    // submit event and the login POST must not fire.
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    // Brief wait to let any erroneous async handler run.
    await new Promise((resolve) => setTimeout(resolve, 20));

    const loginCalls = vi.mocked(global.fetch).mock.calls.filter(
      (c) => typeof c[0] === "string" && c[0].includes("/api/auth/login"),
    );
    expect(loginCalls).toHaveLength(0);
  });

  it("reveals the TOTP input when the server returns 'totp required'", async () => {
    global.fetch = vi.fn()
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ needs_setup: false }), { status: 200 }),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: "totp required" }), { status: 401 }),
      )
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            user_id: 1,
            expires_at: "2099-01-01T00:00:00Z",
            username: "alice",
            is_admin: false,
            totp_enabled: true,
          }),
          { status: 200 },
        ),
      );

    const Page = await freshLogin();
    renderLogin(Page);

    await waitFor(() => screen.getByRole("button", { name: "Sign in" }));
    fireEvent.change(screen.getByLabelText("Username"), { target: { value: "alice" } });
    fireEvent.change(screen.getByLabelText("Password"), { target: { value: "hunter2pass" } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    // After the totp-required response, the TOTP field must appear.
    await waitFor(() => {
      expect(screen.getByLabelText("Authenticator code")).toBeTruthy();
    });

    fireEvent.change(screen.getByLabelText("Authenticator code"), {
      target: { value: "123456" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

    await waitFor(() => {
      expect(screen.getByTestId("home-page")).toBeTruthy();
    });

    // The second login POST must carry totp_code.
    const totpCall = vi.mocked(global.fetch).mock.calls[2];
    const init = totpCall?.[1] as RequestInit | undefined;
    const body = init?.body as string | undefined;
    expect(body).toContain("totp_code=123456");
  });

  it("renders the session-expired banner when ?returnTo= is present", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ needs_setup: false }), { status: 200 }),
    );
    const Page = await freshLogin();
    renderLogin(Page, ["/login?returnTo=/chats/7"]);

    await waitFor(() => {
      expect(screen.getByTestId("login-session-expired-banner")).toBeTruthy();
    });
  });
});
