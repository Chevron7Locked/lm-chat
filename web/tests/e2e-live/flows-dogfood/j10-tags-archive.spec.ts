/* SPDX-License-Identifier: Apache-2.0 */
/**
 * J10 — chat tags + archive/unarchive (P1-dogfood).
 *
 * RED-ON-REVERT: this journey fails if 0a0ba12 ("feat(chat): tags +
 * chat-level archive") is reverted. Before that commit chats had neither a
 * free-form tag list (ChatTagsMenu.tsx) nor a reversible archive flow
 * (Sidebar.tsx's "Archived" section + `archived_at` + the
 * archive/unarchive routes, `GET /api/chats`'s `include_archived` filter).
 *
 * Drives the Sidebar UI directly (no composer / model turn needed):
 *   1. add a tag via ChatTagsMenu and confirm it persists (chat-detail API)
 *      and a tag-count badge renders on the sidebar row;
 *   2. archive the chat — it disappears from the default sidebar list
 *      (and the default `GET /api/chats`) and appears under the
 *      "Archived" section;
 *   3. unarchive it — it returns to the default list/API response and the
 *      (now-empty) Archived section unmounts.
 *
 * UI mechanics + persisted state only — this journey needs a real backend
 * but never touches LM Studio, so it has no model-speed dependency.
 */
import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  createChatViaRequest,
} from "../flows/_flow-helpers";

const TAG_NAME = "dogfood-j10";

test(
  "j10: tagging and archiving/unarchiving a chat persists server-side and updates the sidebar",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    test.setTimeout(120_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    const chatId = await createChatViaRequest(
      page,
      backendURL,
      "Tags Archive Test",
    );
    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page
      .getByPlaceholder(/Message/)
      .waitFor({ state: "visible", timeout: 15_000 });

    const tagsPrefix = `sidebar-chat-${String(chatId)}-tags`;
    const archiveBtn = page.getByTestId(`sidebar-chat-${String(chatId)}-archive`);
    const unarchiveBtn = page.getByTestId(
      `sidebar-chat-${String(chatId)}-unarchive`,
    );
    const archivedSection = page.getByTestId("sidebar-archived-section");
    const archivedRow = page.getByTestId(
      `sidebar-archived-chat-${String(chatId)}`,
    );
    const tagBadge = page.getByTestId(
      `sidebar-chat-${String(chatId)}-tags-badge`,
    );
    // The sidebar row's action bar (tags trigger + archive button) is
    // hover-revealed (sidebar.css: `.lmchat-chat-item:hover + ...actions`), so
    // hover the chat row before clicking any of its actions — otherwise the
    // chat-title link intercepts the click (a real mouse user hovers first).
    const chatRow = page.locator(`a[href="/chats/${String(chatId)}"]`);

    // --- (1) TAGS ------------------------------------------------------
    await expect(archiveBtn).toBeVisible({ timeout: 15_000 });
    await chatRow.hover();
    await page.getByTestId(`${tagsPrefix}-trigger`).click();
    await expect(page.getByTestId(`${tagsPrefix}-menu`)).toBeVisible({
      timeout: 5_000,
    });
    await page.getByTestId(`${tagsPrefix}-input`).fill(TAG_NAME);
    await page.getByTestId(`${tagsPrefix}-submit`).click();

    // The chip renders in the dropdown once the PATCH round-trip lands
    // (ChatTagsMenu is a controlled component fed by the chats query — no
    // optimistic update; see useUpdateChat's onSuccess invalidation).
    await expect(
      page.getByTestId(`${tagsPrefix}-chip-${TAG_NAME}`),
    ).toBeVisible({ timeout: 10_000 });
    // A tag-count badge renders on the row itself once tags.length > 0.
    await expect(tagBadge).toBeVisible({ timeout: 10_000 });
    await expect(tagBadge).toContainText("1");

    // GROUND TRUTH — persisted server-side, not just the query cache.
    const afterTagResp = await page.request.get(
      `${backendURL}/api/chats/${String(chatId)}`,
    );
    expect(afterTagResp.ok()).toBeTruthy();
    const afterTag = (await afterTagResp.json()) as { tags: string[] };
    expect(
      afterTag.tags,
      "the tag was not persisted server-side",
    ).toContain(TAG_NAME);

    // --- (2) ARCHIVE -----------------------------------------------------
    await chatRow.hover();
    await archiveBtn.click();
    // The row (identified by its archive button) leaves the default list —
    // the whole SortableChatItem unmounts and is replaced by
    // ArchivedChatItem under "Archived".
    await expect(archiveBtn).toHaveCount(0, { timeout: 10_000 });

    await expect(archivedSection).toBeVisible({ timeout: 10_000 });
    // Native <details> — expand it to reveal ArchivedChatItem children.
    await archivedSection.locator("summary").click();
    await expect(archivedRow).toBeVisible({ timeout: 10_000 });

    const afterArchiveResp = await page.request.get(
      `${backendURL}/api/chats/${String(chatId)}`,
    );
    expect(afterArchiveResp.ok()).toBeTruthy();
    const afterArchive = (await afterArchiveResp.json()) as {
      archived_at: string | null;
    };
    expect(
      afterArchive.archived_at,
      "archived_at was not set server-side after archiving",
    ).not.toBeNull();

    // The default (non-archived) list must NOT include this chat —
    // GET /api/chats defaults to include_archived=false.
    const defaultListResp = await page.request.get(`${backendURL}/api/chats`);
    expect(defaultListResp.ok()).toBeTruthy();
    const defaultList = (await defaultListResp.json()) as Array<{
      id: number;
    }>;
    expect(
      defaultList.map((c) => c.id),
      "archived chat still appears in the default (non-archived) chats list",
    ).not.toContain(chatId);

    // --- (3) UNARCHIVE -----------------------------------------------------
    await unarchiveBtn.click();
    await expect(archivedRow).toHaveCount(0, { timeout: 10_000 });
    // This was the only archived chat — the whole "Archived" section
    // unmounts (Sidebar.tsx gates it on `archivedChats.length > 0`).
    await expect(archivedSection).toHaveCount(0, { timeout: 10_000 });
    // The row reappears in the default list.
    await expect(archiveBtn).toBeVisible({ timeout: 10_000 });

    const afterUnarchiveResp = await page.request.get(
      `${backendURL}/api/chats/${String(chatId)}`,
    );
    expect(afterUnarchiveResp.ok()).toBeTruthy();
    const afterUnarchive = (await afterUnarchiveResp.json()) as {
      archived_at: string | null;
    };
    expect(
      afterUnarchive.archived_at,
      "archived_at was not cleared server-side after unarchiving",
    ).toBeNull();

    const defaultListResp2 = await page.request.get(`${backendURL}/api/chats`);
    expect(defaultListResp2.ok()).toBeTruthy();
    const defaultList2 = (await defaultListResp2.json()) as Array<{
      id: number;
    }>;
    expect(
      defaultList2.map((c) => c.id),
      "unarchived chat did not return to the default chats list",
    ).toContain(chatId);

    assertNoConsoleErrors(collectErrors(), "j10");
  },
);
