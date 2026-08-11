/**
 * Flow 8 — Slash `/clear` command.
 *
 * What it proves:
 *   When a user types /clear and submits:
 *     1. A confirmation dialog appears (ConfirmDialog in Chat.tsx).
 *     2. Clicking "Confirm" dismisses the dialog.
 *     3. An info toast appears (current implementation: "Clear not yet wired
 *        to backend (P8)." — this is the P8-stub toast defined in Chat.tsx:483).
 *     4. Clicking "Cancel" dismisses the dialog without any toast.
 *
 * Source: Chat.tsx handleClear → setConfirmClear(true) → ConfirmDialog renders.
 * On confirm: setConfirmClear(false) + push info toast "Clear not yet wired…"
 * On cancel: setConfirmClear(false) only.
 *
 * Note: The backend clear endpoint (DELETE all messages) is P8 work; the
 * current implementation shows an info toast acknowledging this. The flow
 * test verifies the UX path (dialog → confirm → toast) not backend state.
 */

import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  createChatViaRequest,
  seedMessage,
} from "./_flow-helpers";

test(
  "flow-08: /clear shows confirm dialog; Confirm triggers info toast",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    // Create a chat with a message so there is content to "clear".
    const chatId = await createChatViaRequest(page, backendURL, "Clear Flow08");
    await seedMessage(page, backendURL, chatId, "user", "Message before clear");

    // Navigate to the chat.
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 15_000 });

    const composer = page.getByPlaceholder(/Message/);

    // Submit /clear.
    await composer.fill("/clear");
    await composer.press("Meta+Enter");

    // ConfirmDialog should appear. It has role="alertdialog" and aria-modal.
    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toBeVisible({ timeout: 8_000 });

    // Dialog text confirms the destructive clear (F2 — implemented 2026-06-17).
    await expect(dialog).toContainText("permanently deleted", { timeout: 3_000 });

    // Confirm buttons: "Cancel" and "Confirm".
    // Scope button lookups to the dialog to avoid DnD sortable button conflicts.
    const cancelBtn = dialog.getByRole("button", { name: "Cancel", exact: true });
    const confirmBtn = dialog.getByRole("button", { name: "Confirm", exact: true });
    await expect(cancelBtn).toBeVisible({ timeout: 3_000 });
    await expect(confirmBtn).toBeVisible({ timeout: 3_000 });

    // Click Confirm.
    await confirmBtn.click();

    // Dialog dismissed.
    await expect(dialog).not.toBeVisible({ timeout: 3_000 });

    // F2 (2026-06-17): /clear is implemented — a success toast confirms the
    // result ("Cleared N message(s)." or "Chat is already empty.").
    const resultToast = page
      .getByRole("alert")
      .filter({ hasText: /cleared|already empty/i });
    await expect(resultToast).toBeVisible({ timeout: 8_000 });

    assertNoConsoleErrors(collectErrors(), "flow-08");
  }
);

test(
  "flow-08b: /clear shows confirm dialog; Cancel dismisses without toast",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    const chatId = await createChatViaRequest(page, backendURL, "Clear Cancel Flow08b");
    await seedMessage(page, backendURL, chatId, "user", "Do not clear me");

    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 15_000 });

    const composer = page.getByPlaceholder(/Message/);

    // Submit /clear.
    await composer.fill("/clear");
    await composer.press("Meta+Enter");

    const dialog = page.getByRole("alertdialog");
    await expect(dialog).toBeVisible({ timeout: 8_000 });

    // Click Cancel — use exact: true to avoid matching DnD sortable buttons
    // that may also have "Cancel" in their aria-label region.
    await dialog.getByRole("button", { name: "Cancel", exact: true }).click();

    // Dialog dismissed.
    await expect(dialog).not.toBeVisible({ timeout: 3_000 });

    // No toast should appear after Cancel.
    const anyToast = page.getByRole("alert");
    await expect(anyToast).not.toBeVisible({ timeout: 2_000 });

    // Original message still visible (no clear occurred).
    await expect(page.getByText("Do not clear me")).toBeVisible({ timeout: 5_000 });

    assertNoConsoleErrors(collectErrors(), "flow-08b");
  }
);
