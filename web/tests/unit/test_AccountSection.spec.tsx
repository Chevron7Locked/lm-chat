/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for AccountSection (P13c.2 + P13l.5).
 *
 * Locked behaviours:
 *   - Renders the username + role read-out from authStore.user.
 *   - P13l.5: the username IS the identity display.  No display-name
 *     editor, no deferred banner, no "Display name" heading.
 *   - Submits a form-encoded POST to /api/auth/password with old +
 *     new password fields on a valid submit; clears the form + pushes a
 *     success toast.
 *   - Inline error states (no fetch fired): empty fields, mismatched
 *     confirm, identical new == old.
 *   - 400 server response surfaces the server detail ("old password is
 *     incorrect") in the inline error region.
 *   - Sign-out triggers logout + navigation to /login.
 *
 * The test mounts AccountSection inside a MemoryRouter and stubs
 * `fetch` directly (the api client uses `fetch` under the hood).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
} from "@testing-library/react";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { createElement } from "react";

// ─── Mock toast (push spy reused across tests) ───────────────────────────────

const mockPush = vi.fn();
vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: mockPush }),
  useToastStore: { getState: () => ({ push: mockPush }) },
}));

// ─── Helpers ─────────────────────────────────────────────────────────────────

async function freshAccountSection() {
  vi.resetModules();
  const storeMod = await import("@/stores/authStore");
  // Seed an authenticated user; the production AuthHydrator would set
  // this from /me on app mount.
  storeMod.useAuthStore.setState({
    user: {
      id: 1,
      username: "alice",
      is_admin: false,
      totp_enabled: false,
    },
    isInitializing: false,
    isLoading: false,
    error: null,
  });
  const mod = await import("@/components/AccountSection");
  return mod.AccountSection;
}

async function freshAccountSectionLoggedOut() {
  vi.resetModules();
  const storeMod = await import("@/stores/authStore");
  storeMod.useAuthStore.setState({
    user: null,
    isInitializing: false,
    isLoading: false,
    error: null,
  });
  const mod = await import("@/components/AccountSection");
  return mod.AccountSection;
}

function renderInRouter(Component: () => React.ReactElement) {
  return render(
    createElement(
      MemoryRouter,
      { initialEntries: ["/settings/account"] },
      createElement(
        Routes,
        null,
        createElement(Route, {
          path: "/settings/account",
          element: createElement(Component),
        }),
        createElement(Route, {
          path: "/login",
          element: createElement("div", { "data-testid": "login-page" }, "login"),
        }),
      ),
    ),
  );
}

// ─── Suite ───────────────────────────────────────────────────────────────────

describe("AccountSection", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mockPush.mockClear();
    cleanup();
  });

  it("renders username read-out as the user's identity (P13l.5)", async () => {
    const Page = await freshAccountSection();
    renderInRouter(Page);

    expect(screen.getByTestId("settings-account-section")).toBeTruthy();
    expect(screen.getByText("alice")).toBeTruthy();
  });

  it("does not surface a display-name editor or deferred banner (P13l.5)", async () => {
    const Page = await freshAccountSection();
    renderInRouter(Page);

    // P13l.5: the username IS the identity display.  The prior
    // P13c.2 "deferred" banner is gone and no display-name input
    // exists in the DOM.
    expect(screen.queryByTestId("account-display-name-deferred")).toBeNull();
    expect(screen.queryByText(/display name/i)).toBeNull();
    expect(screen.queryByLabelText(/display name/i)).toBeNull();
    expect(screen.queryByPlaceholderText(/display name/i)).toBeNull();
  });

  it("shows the admin badge for admin users", async () => {
    vi.resetModules();
    const storeMod = await import("@/stores/authStore");
    storeMod.useAuthStore.setState({
      user: {
        id: 2,
        username: "root",
        is_admin: true,
        totp_enabled: false,
      },
      isInitializing: false,
      isLoading: false,
      error: null,
    });
    const mod = await import("@/components/AccountSection");
    renderInRouter(mod.AccountSection);
    expect(screen.getByText("Admin")).toBeTruthy();
  });

  it("renders the sign-in prompt when no user is authenticated", async () => {
    const Page = await freshAccountSectionLoggedOut();
    renderInRouter(Page);
    expect(screen.getByText(/Sign in to manage your account/i)).toBeTruthy();
    expect(screen.queryByTestId("account-change-password-form")).toBeNull();
  });

  it("submits a form-encoded POST to /api/auth/password and clears the form", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ status: "ok" }), { status: 200 }),
      );

    const Page = await freshAccountSection();
    renderInRouter(Page);

    const oldInput = screen.getByTestId("account-old-password") as HTMLInputElement;
    const newInput = screen.getByTestId("account-new-password") as HTMLInputElement;
    const confInput = screen.getByTestId("account-confirm-password") as HTMLInputElement;

    fireEvent.change(oldInput, { target: { value: "oldpw1234" } });
    fireEvent.change(newInput, { target: { value: "newpw5678" } });
    fireEvent.change(confInput, { target: { value: "newpw5678" } });
    fireEvent.click(screen.getByTestId("account-change-password-submit"));

    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith({
        variant: "success",
        message: "Password changed.",
      });
    });

    // Verify request shape.
    expect(global.fetch).toHaveBeenCalledTimes(1);
    const call = vi.mocked(global.fetch).mock.calls[0];
    expect(call?.[0]).toBe("/api/auth/password");
    const init = call?.[1];
    expect(init?.method).toBe("POST");
    const headers = init?.headers as Record<string, string> | undefined;
    expect(headers?.["Content-Type"]).toBe("application/x-www-form-urlencoded");
    const body = init?.body as string | undefined;
    expect(body).toContain("old_password=oldpw1234");
    expect(body).toContain("new_password=newpw5678");

    // Form was cleared.
    expect(oldInput.value).toBe("");
    expect(newInput.value).toBe("");
    expect(confInput.value).toBe("");
  });

  it("rejects empty fields without hitting the API", async () => {
    global.fetch = vi.fn();
    const Page = await freshAccountSection();
    renderInRouter(Page);

    fireEvent.click(screen.getByTestId("account-change-password-submit"));

    await waitFor(() => {
      expect(screen.getByTestId("account-pw-error").textContent).toContain(
        "All password fields are required.",
      );
    });
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("rejects mismatched confirm without hitting the API", async () => {
    global.fetch = vi.fn();
    const Page = await freshAccountSection();
    renderInRouter(Page);

    fireEvent.change(screen.getByTestId("account-old-password"), {
      target: { value: "oldpw1234" },
    });
    fireEvent.change(screen.getByTestId("account-new-password"), {
      target: { value: "newpw5678" },
    });
    fireEvent.change(screen.getByTestId("account-confirm-password"), {
      target: { value: "different9" },
    });
    fireEvent.click(screen.getByTestId("account-change-password-submit"));

    await waitFor(() => {
      expect(screen.getByTestId("account-pw-error").textContent).toContain(
        "Passwords do not match.",
      );
    });
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("rejects identical new+old passwords without hitting the API", async () => {
    global.fetch = vi.fn();
    const Page = await freshAccountSection();
    renderInRouter(Page);

    fireEvent.change(screen.getByTestId("account-old-password"), {
      target: { value: "samepw1234" },
    });
    fireEvent.change(screen.getByTestId("account-new-password"), {
      target: { value: "samepw1234" },
    });
    fireEvent.change(screen.getByTestId("account-confirm-password"), {
      target: { value: "samepw1234" },
    });
    fireEvent.click(screen.getByTestId("account-change-password-submit"));

    await waitFor(() => {
      expect(screen.getByTestId("account-pw-error").textContent).toContain(
        "New password must differ from the current password.",
      );
    });
    expect(global.fetch).not.toHaveBeenCalled();
  });

  it("surfaces the server detail on a 400 (old password incorrect)", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(
          JSON.stringify({ detail: "old password is incorrect" }),
          { status: 400 },
        ),
      );

    const Page = await freshAccountSection();
    renderInRouter(Page);

    fireEvent.change(screen.getByTestId("account-old-password"), {
      target: { value: "wrongpw" },
    });
    fireEvent.change(screen.getByTestId("account-new-password"), {
      target: { value: "newpw5678" },
    });
    fireEvent.change(screen.getByTestId("account-confirm-password"), {
      target: { value: "newpw5678" },
    });
    fireEvent.click(screen.getByTestId("account-change-password-submit"));

    await waitFor(() => {
      expect(screen.getByTestId("account-pw-error").textContent).toContain(
        "old password is incorrect",
      );
    });
    expect(mockPush).not.toHaveBeenCalled();
  });

  it("signs out and navigates to /login on success", async () => {
    global.fetch = vi
      .fn()
      .mockResolvedValue(
        new Response(JSON.stringify({ status: "ok" }), { status: 200 }),
      );

    const Page = await freshAccountSection();
    renderInRouter(Page);

    // UX-AUDIT-r1 Wave 3-H (UX-013): sign-out is now a two-step flow to
    // prevent accidental session drops.  First click switches to a confirm
    // prompt; second click (on the confirm button) commits the logout.
    fireEvent.click(screen.getByTestId("settings-account-signout"));

    // Confirm prompt is now visible.
    await waitFor(() => {
      expect(screen.getByTestId("settings-account-signout-confirm")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("settings-account-signout-confirm"));

    await waitFor(() => {
      expect(screen.getByTestId("login-page")).toBeTruthy();
    });
    expect(global.fetch).toHaveBeenCalledWith(
      "/api/auth/logout",
      expect.objectContaining({ method: "POST" }),
    );
  });
});
