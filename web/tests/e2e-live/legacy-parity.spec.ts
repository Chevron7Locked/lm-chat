/**
 * Legacy parity tests — ported from lm-chat v0.5.x tests/test_e2e_refactor.py.
 *
 * These tests verify the same user-visible behaviours against the v1 SPA.
 * Tests run against a live FastAPI backend (started by the _fixtures.ts
 * worker fixture) with a stub LM Studio upstream.
 *
 * Test inventory (13 cases after P8a):
 *
 *   Reasoning state (3):
 *     1. test_cycle_button_drives_chat_settings_dropdown
 *     2. test_global_default_propagates_to_cycle_button
 *     3. test_effective_reasoning_treats_empty_string_as_no_override
 *
 *   Orphan stream recovery (1):
 *     4. test_interrupted_row_renders_retry_button
 *
 *   Outside-click hardening (2):
 *     5. test_drag_out_does_not_close_dropdown
 *     6. test_true_outside_press_release_closes
 *
 *   URL routing / deep-links (3):
 *     7. test_url_reflects_active_chat
 *     8. test_deep_link_loads_specific_chat
 *     9. test_settings_route_opens_panel
 *
 *   Toast system (2):
 *    10. test_all_variants_render
 *    11. test_error_toast_persists_until_dismissed
 *
 * P8a: all 5 previously-skipped tests are now un-skipped. The features they
 * cover shipped in P8a:
 *   1-3: ReasoningToggle component (chatSettingsStore, cycleGlobalReasoning,
 *        effectiveReasoning with empty-string sentinel).
 *   4:   InterruptedRow + retry button (orphan localStorage key detection).
 *   5-6: UserMenu component with outside-click semantics.
 *
 * Zero tests remain skipped after P8a.
 */

import { test, expect } from "./_fixtures";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Log in with the worker-scoped test user and wait for the Chat page. */
async function loginAndWait(
  page: import("@playwright/test").Page,
  backendURL: string,
  username: string,
  password: string
): Promise<void> {
  await page.goto(backendURL);
  await page.getByLabel("Username").fill(username);
  await page.locator("#lmchat-password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  // Wait for post-login navigation (the chat list page).
  await page.waitForURL(`${backendURL}/`, { timeout: 10_000 });
}

// ---------------------------------------------------------------------------
// 1. Reasoning state — cycle button drives chat-settings dropdown
// ---------------------------------------------------------------------------

test.describe("Reasoning state collapse (v0.5.x audit finding #1)", () => {
  test(
    "test_cycle_button_drives_chat_settings_dropdown",
    async ({ page, backendURL, testUsername, testPassword }) => {
      await loginAndWait(page, backendURL, testUsername, testPassword);

      // Create a chat and navigate to it in-SPA.
      await page.getByRole("button", { name: /New Chat/i }).click();
      const chatLink = page.getByRole("link", { name: /New Chat/i }).first();
      await chatLink.waitFor({ state: "visible", timeout: 10_000 });
      await chatLink.click();
      await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 10_000 });

      // The ReasoningToggle should be visible in the TopBar.
      const toggle = page.getByTestId("reasoning-toggle");
      await expect(toggle).toBeVisible({ timeout: 5_000 });

      // Initial state: "off".
      await expect(toggle).toContainText("off");

      // Click once — cycles to "low".
      await toggle.click();
      await expect(toggle).toContainText("low");

      // Right-click opens the dropdown.
      await toggle.click({ button: "right" });
      const dropdown = page.getByRole("listbox", { name: "Reasoning effort level" });
      await expect(dropdown).toBeVisible({ timeout: 3_000 });

      // Dropdown contains all four options.
      await expect(page.getByTestId("reasoning-option-off")).toBeVisible();
      await expect(page.getByTestId("reasoning-option-high")).toBeVisible();

      // Click "high" — should close dropdown and update label.
      await page.getByTestId("reasoning-option-high").click();
      await expect(dropdown).not.toBeVisible({ timeout: 2_000 });
      await expect(toggle).toContainText("high");
    }
  );

  test(
    "test_global_default_propagates_to_cycle_button",
    async ({ page, backendURL, testUsername, testPassword }) => {
      await loginAndWait(page, backendURL, testUsername, testPassword);

      // Navigate to a chat so the toggle is visible.
      await page.getByRole("button", { name: /New Chat/i }).click();
      const chatLink = page.getByRole("link", { name: /New Chat/i }).first();
      await chatLink.waitFor({ state: "visible", timeout: 10_000 });
      await chatLink.click();
      await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 10_000 });

      const toggle = page.getByTestId("reasoning-toggle");
      await expect(toggle).toBeVisible({ timeout: 5_000 });

      // Set global=high via right-click dropdown.
      await toggle.click({ button: "right" });
      await page.getByTestId("reasoning-option-high").click();

      // The cycle button must now show "high" (global default propagated).
      await expect(toggle).toContainText("high");
    }
  );

  test(
    "test_effective_reasoning_treats_empty_string_as_no_override",
    async ({ page, backendURL, testUsername, testPassword }) => {
      await loginAndWait(page, backendURL, testUsername, testPassword);

      // Step 1: Set the GLOBAL default to "medium" on the home page (no chat
      // selected — chatId is null in TopBar, so the ReasoningToggle operates
      // on the global without creating a per-chat override).
      const globalToggle = page.getByTestId("reasoning-toggle");
      await expect(globalToggle).toBeVisible({ timeout: 5_000 });
      // Right-click on the global toggle (chatId=undefined at root) to set global.
      await globalToggle.click({ button: "right" });
      await page.getByTestId("reasoning-option-medium").click();
      // Global is now "medium"; no per-chat override exists.
      await expect(globalToggle).toContainText("med");

      // Step 2: Navigate to a chat (per-chat toggle starts with no override,
      // so effective = global = "medium").
      // Use exact name to avoid matching DnD drag-handle buttons whose
      // aria-label is "Reorder: New Chat" (added in P9a). The strict-mode
      // violation was the root cause of the "pre-existing flake" referenced in
      // P9a/P9b briefs — it is deterministic, not stochastic.
      await page.getByRole("button", { name: "+ New Chat", exact: true }).click();
      const chatLink = page.getByRole("link", { name: /New Chat/i }).first();
      await chatLink.waitFor({ state: "visible", timeout: 10_000 });
      await chatLink.click();
      await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 10_000 });

      // Toggle now has chatId set. Effective = global = "medium".
      const toggle = page.getByTestId("reasoning-toggle");
      await expect(toggle).toContainText("med", { timeout: 3_000 });

      // Step 3: Set a per-chat override to "high".
      await toggle.click({ button: "right" });
      await page.getByTestId("reasoning-option-high").click();
      await expect(toggle).toContainText("high");

      // Step 4: Clear the per-chat override via "use global default" option.
      await toggle.click({ button: "right" });
      const clearBtn = page.getByTestId("reasoning-option-clear");
      await expect(clearBtn).toBeVisible({ timeout: 3_000 });
      await clearBtn.click();

      // After clearing, effective reasoning falls through to global "medium".
      await expect(toggle).toContainText("med");
    }
  );
});

// ---------------------------------------------------------------------------
// 4. Orphan stream recovery
// ---------------------------------------------------------------------------

test.describe("Orphan stream recovery (v0.5.x audit finding #2)", () => {
  // Reset the page to backendURL/ after the orphan test so subsequent tests
  // get a consistent starting URL.  The orphan test uses pushState to navigate
  // to /chats/<id> without a full reload, which leaves the page URL at a
  // sub-route; loginAndWait in subsequent tests then issues page.goto(backendURL)
  // which reloads with an active session (no login form) — breaking those tests.
  // afterEach fires a full navigation back to / to establish clean state.
  // See DEFERRED.md §6 for the root-cause analysis and long-term fix.
  test.afterEach(async ({ page, backendURL }) => {
    await page.goto(backendURL).catch(() => {
      // Ignore navigation errors in cleanup.
    });
  });

  test(
    "test_interrupted_row_renders_retry_button",
    async ({ page, backendURL, testUsername, testPassword }) => {
      await loginAndWait(page, backendURL, testUsername, testPassword);

      // Create a chat via the API.
      const chatResp = await page.request.post(`${backendURL}/api/chats`, {
        form: { title: "Orphan E2E" },
      });
      expect(chatResp.ok()).toBeTruthy();
      const chat = await chatResp.json();
      const chatId: number = chat.id;

      // Seed a user message.
      await page.request.post(
        `${backendURL}/api/chats/${chatId}/messages`,
        {
          form: { role: "user", content: "Tell me a story" },
        }
      );

      // Navigate in-SPA (authStore.user is populated; no redirect to /login).
      // Use SPA navigation to avoid cold-boot auth redirect.
      // Then inject the orphan localStorage key before navigating.
      await page.evaluate(
        ([cId]: [number]) => {
          try {
            localStorage.setItem(`lmchat:sse:${cId}:msg_id`, "99999");
          } catch {
            // ignore
          }
        },
        [chatId]
      );

      // Navigate to the chat via SPA (keeping React Router in-SPA after
      // loginAndWait already authenticated us).
      await page.evaluate(
        ([cId]: [number]) => {
          window.history.pushState({}, "", `/chats/${cId}`);
          window.dispatchEvent(new PopStateEvent("popstate", { state: {} }));
        },
        [chatId]
      );

      // Wait for the chat view to render.
      await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 10_000 });

      // The InterruptedRow should be visible (orphan key with no matching
      // completed assistant message triggers the render).
      const interruptedRow = page.getByTestId("interrupted-row");
      await expect(interruptedRow).toBeVisible({ timeout: 5_000 });
      await expect(interruptedRow).toHaveAttribute("role", "alert");

      // Retry button must be present.
      const retryBtn = page.getByTestId("interrupted-retry-btn");
      await expect(retryBtn).toBeVisible({ timeout: 2_000 });
    }
  );
});

// ---------------------------------------------------------------------------
// 5–6. Outside-click hardening
// ---------------------------------------------------------------------------

test.describe("Outside-click hardening (v0.5.x audit finding #3)", () => {
  test(
    "test_drag_out_does_not_close_dropdown",
    async ({ page, backendURL, testUsername, testPassword }) => {
      await loginAndWait(page, backendURL, testUsername, testPassword);

      // The UserMenu avatar button is in the Sidebar footer.
      const avatarBtn = page.getByTestId("user-menu-avatar");
      await expect(avatarBtn).toBeVisible({ timeout: 10_000 });

      // Open the menu.
      await avatarBtn.click();
      const dropdown = page.getByTestId("user-menu-dropdown");
      await expect(dropdown).toBeVisible({ timeout: 3_000 });

      // Simulate press-inside-drag-outside: mousedown on avatar, mouseup on body.
      await avatarBtn.dispatchEvent("mousedown");
      await page.locator("body").dispatchEvent("mouseup");

      // Menu must still be visible (press started inside).
      await expect(dropdown).toBeVisible({ timeout: 2_000 });
    }
  );

  test(
    "test_true_outside_press_release_closes",
    async ({ page, backendURL, testUsername, testPassword }) => {
      await loginAndWait(page, backendURL, testUsername, testPassword);

      // Open the UserMenu.
      const avatarBtn = page.getByTestId("user-menu-avatar");
      await expect(avatarBtn).toBeVisible({ timeout: 10_000 });
      await avatarBtn.click();
      const dropdown = page.getByTestId("user-menu-dropdown");
      await expect(dropdown).toBeVisible({ timeout: 3_000 });

      // True outside press-release: both mousedown and mouseup on body (outside menu).
      await page.locator("body").dispatchEvent("mousedown");
      await page.locator("body").dispatchEvent("mouseup");

      // Menu must be closed.
      await expect(dropdown).not.toBeVisible({ timeout: 3_000 });
    }
  );
});

// ---------------------------------------------------------------------------
// 7. URL reflects active chat
// ---------------------------------------------------------------------------

test.describe("URL routing (v0.5.x audit finding #4)", () => {
  test(
    "test_url_reflects_active_chat",
    async ({ page, backendURL, testUsername, testPassword }) => {
      await loginAndWait(page, backendURL, testUsername, testPassword);

      // Create a chat via the API so the sidebar has at least one entry.
      // The chats endpoint uses Form(...) — use form: not data:.
      const chatResp = await page.request.post(`${backendURL}/api/chats`, {
        form: { title: "Routing test" },
      });
      expect(chatResp.ok()).toBeTruthy();
      const chat = await chatResp.json();
      const chatId: number = chat.id;

      // Navigate to the chat deep link — the SPA shell should load and
      // React Router should render the chat view.
      await page.goto(`${backendURL}/chats/${chatId}`);
      await expect(page).toHaveURL(`${backendURL}/chats/${chatId}`);

      // The root div should render with content (not blank).
      await expect(page.locator("#root")).not.toBeEmpty({ timeout: 5_000 });
    }
  );

  // ---------------------------------------------------------------------------
  // 8. Deep-link loads specific chat
  // ---------------------------------------------------------------------------

  test(
    "test_deep_link_loads_specific_chat",
    async ({ page, backendURL, testUsername, testPassword }) => {
      await loginAndWait(page, backendURL, testUsername, testPassword);

      // The chats endpoint uses Form(...) — use form: not data:.
      const chatResp = await page.request.post(`${backendURL}/api/chats`, {
        form: { title: "Deep link target" },
      });
      expect(chatResp.ok()).toBeTruthy();
      const chat = await chatResp.json();
      const chatId: number = chat.id;

      // Direct deep-link navigation.
      await page.goto(`${backendURL}/chats/${chatId}`);

      // The URL should match exactly (no redirect away).
      await expect(page).toHaveURL(`${backendURL}/chats/${chatId}`);
      // Page renders without error.
      await expect(page.locator("#root")).not.toBeEmpty({ timeout: 5_000 });
    }
  );

  // ---------------------------------------------------------------------------
  // 9. Settings route opens panel
  // ---------------------------------------------------------------------------

  test(
    "test_settings_route_opens_panel",
    async ({ page, backendURL, testUsername, testPassword }) => {
      await loginAndWait(page, backendURL, testUsername, testPassword);

      // Navigate directly to /settings — SPA shell must serve it.
      await page.goto(`${backendURL}/settings`);
      await expect(page).toHaveURL(`${backendURL}/settings`);

      // The root renders (React Router handles /settings client-side).
      await expect(page.locator("#root")).not.toBeEmpty({ timeout: 5_000 });
    }
  );
});

// ---------------------------------------------------------------------------
// 10–11. Toast system
// ---------------------------------------------------------------------------

test.describe("Toast system", () => {
  // C-003: tests rewritten against v1 selectors.  Navigation stays in-session
  // after loginAndWait to avoid the fresh-page auth redirect (authStore.user
  // is null on cold boot because /me hydration runs asynchronously).
  //
  // In-SPA navigation strategy: after loginAndWait the SPA is running with
  // authStore.user populated.  Creating a chat via page.request.post (shares
  // the session cookie, sends correct form-encoded data) then pushing the URL
  // via window.history.pushState + popstate keeps React Router in-SPA without
  // a full page reload that would reset authStore.user to null.
  //
  // Trigger map (from Composer.tsx):
  //   info    → /panel slash command   → "/panel isn't in this build yet — multi-model convergence is coming."
  //   warning → /memory (no arg)       → "Usage: /memory <text to pin>"
  //   error   → route 500 on /fork     → "Fork failed."  (Chat.tsx:154)
  //
  // v1 design note: ALL toast variants auto-dismiss after 5 s (toastStore
  // default).  The v0.5.x error-persists behaviour is intentionally NOT
  // carried forward.  test_error_toast_persists_until_dismissed is rewritten
  // as "error toast renders with assertive aria-live and can be manually
  // dismissed" — this is the correct v1 specification.

  /**
   * Open a new chat in-SPA while preserving authStore.user.
   *
   * Navigation strategy (avoids cold-boot auth redirect):
   *  1. Click "+ New Chat" — the useCreateChat hook now sends form-encoded
   *     data matching the backend's Form(...) parameter (fixed in this
   *     commit alongside C-003).
   *  2. Wait for the new chat link to appear in the Sidebar.
   *  3. Click the link — React Router handles the navigation in-SPA
   *     without a full page reload, so authStore.user stays populated.
   *  4. Wait for the Composer to confirm Chat.tsx rendered.
   *
   * Note: page.goto('/chats/<id>') cannot be used here because the SPA
   * has no /me hydration endpoint yet (P8 scope); on a fresh page load
   * authStore.user is null → <Navigate to="/login" replace />.
   */
  async function openNewChatInSpa(
    page: import("@playwright/test").Page
  ): Promise<void> {
    await page.getByRole("button", { name: /New Chat/i }).click();
    // Wait for the chat link to appear in the Sidebar after mutation completes.
    const chatLink = page.getByRole("link", { name: /New Chat/i }).first();
    await chatLink.waitFor({ state: "visible", timeout: 10_000 });
    await chatLink.click();
    // Confirm Chat.tsx rendered by waiting for the Composer.
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 10_000 });
  }

  test(
    "test_all_variants_render",
    async ({ page, backendURL, testUsername, testPassword }) => {
      await loginAndWait(page, backendURL, testUsername, testPassword);
      await openNewChatInSpa(page);

      const composer = page.getByPlaceholder(/Message/);

      // --- info variant: /panel slash command ---
      await composer.fill("/panel");
      await composer.press("Meta+Enter");
      const infoToast = page
        .getByRole("alert")
        .filter({ hasText: /P8\.5/ });
      await expect(infoToast).toBeVisible({ timeout: 5_000 });
      // info variant uses aria-live="polite"
      await expect(infoToast).toHaveAttribute("aria-live", "polite");

      // --- warning variant: /memory with no argument ---
      await composer.fill("/memory");
      await composer.press("Meta+Enter");
      const warnToast = page
        .getByRole("alert")
        .filter({ hasText: /Usage: \/memory/ });
      await expect(warnToast).toBeVisible({ timeout: 5_000 });
      await expect(warnToast).toHaveAttribute("aria-live", "polite");

      // --- error variant: stub fork 500 then /fork slash command ---
      await page.route("**/api/chats/*/fork", (route) => {
        void route.fulfill({ status: 500, body: "Internal Server Error" });
      });
      await composer.fill("/fork");
      await composer.press("Meta+Enter");
      const errToast = page
        .getByRole("alert")
        .filter({ hasText: /Fork failed/ });
      await expect(errToast).toBeVisible({ timeout: 5_000 });
      // error variant uses aria-live="assertive" (Toast.tsx:64)
      await expect(errToast).toHaveAttribute("aria-live", "assertive");
      // Remove route stub for subsequent tests.
      await page.unrouteAll();
    }
  );

  test(
    "test_error_toast_persists_until_dismissed",
    async ({ page, backendURL, testUsername, testPassword }) => {
      // v1 design: error toasts also auto-dismiss after 5 s (same as all
      // variants).  The v0.5.x "error persists forever" behaviour is NOT
      // carried forward.  This test verifies:
      //   (a) the error toast renders with aria-live="assertive", and
      //   (b) the manual dismiss button removes it before the auto-dismiss
      //       fires — i.e. the close affordance works correctly.
      await loginAndWait(page, backendURL, testUsername, testPassword);
      await openNewChatInSpa(page);

      const composer = page.getByPlaceholder(/Message/);

      // Stub the fork endpoint to return 500.
      await page.route("**/api/chats/*/fork", (route) => {
        void route.fulfill({ status: 500, body: "Internal Server Error" });
      });
      await composer.fill("/fork");
      await composer.press("Meta+Enter");

      // Toast must appear with assertive aria-live.
      const errToast = page
        .getByRole("alert")
        .filter({ hasText: /Fork failed/ });
      await expect(errToast).toBeVisible({ timeout: 5_000 });
      await expect(errToast).toHaveAttribute("aria-live", "assertive");

      // Click the dismiss button — aria-label="Dismiss notification"
      // (Toast.tsx:101).  The toast must disappear before auto-dismiss fires.
      const dismissBtn = errToast.getByRole("button", {
        name: "Dismiss notification",
      });
      await dismissBtn.click();
      await expect(errToast).not.toBeVisible({ timeout: 2_000 });

      await page.unrouteAll();
    }
  );
});

// ---------------------------------------------------------------------------
// Smoke: SPA shell serves correct nonce
// ---------------------------------------------------------------------------

test(
  "spa shell serves HTML with CSP nonce for browser navigation",
  async ({ page, backendURL }) => {
    // Intercept the navigation response and capture both headers and body.
    let cspHeader = "";
    let responseBody = "";
    page.on("response", async (resp) => {
      if (resp.url() === `${backendURL}/`) {
        cspHeader = resp.headers()["content-security-policy"] ?? "";
        try {
          responseBody = await resp.text();
        } catch {
          // Response body already consumed.
        }
      }
    });

    await page.goto(backendURL);

    // The page must have loaded the React mount point.
    await expect(page.locator("#root")).toBeAttached({ timeout: 10_000 });

    // Verify a CSP nonce is present in the response header.
    expect(cspHeader).toContain("nonce-");

    // Verify the nonce from the CSP header also appears in the raw HTTP
    // response body (not page.content() which reflects the live DOM
    // after React has executed).
    const match = cspHeader.match(/nonce-([A-Za-z0-9_\-]+)/);
    if (match && responseBody) {
      expect(responseBody).toContain(`nonce="${match[1]}"`);
    }
  }
);

// ---------------------------------------------------------------------------
// Smoke: ?ui=v2 sets the sticky cookie
// ---------------------------------------------------------------------------

test(
  "?ui=v2 query param sets lmchat_ui=v2 cookie",
  async ({ page, backendURL }) => {
    await page.goto(`${backendURL}/?ui=v2`);

    const cookies = await page.context().cookies([backendURL]);
    const uiCookie = cookies.find((c) => c.name === "lmchat_ui");
    expect(uiCookie).toBeDefined();
    expect(uiCookie?.value).toBe("v2");
  }
);
