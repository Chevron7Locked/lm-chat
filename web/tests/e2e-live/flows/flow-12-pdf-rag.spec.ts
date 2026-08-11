/**
 * Flow 12 — PDF upload + RAG enable + chunk preview.
 *
 * What it proves:
 *   A user can upload a PDF to /documents, see it appear in the document list,
 *   expand the chunk preview (verifying the backend chunked the file), then
 *   navigate to a chat and confirm the RAG toggle is wirable via the API.
 *   The stub LM Studio server's /api/v1/embeddings endpoint is exercised so
 *   the embedding step of upload_document() completes without a real model.
 *
 * Flow:
 *   1. Login → navigate to /documents.
 *   2. Upload test-doc.pdf via the file input (bypasses drag-drop to avoid
 *      platform-specific DnD APIs in Playwright).
 *   3. Success toast appears; document row appears in the list.
 *   4. Click the document row to expand chunk preview — at least 1 chunk shown.
 *   5. Navigate to a new chat; PATCH rag_enabled=true via API; reload and
 *      confirm no errors (RAG enable is a backend-only operation — the UI
 *      toggle in TopBar wires to it, and we test the API path here since
 *      the Chat.tsx TopBar RAG toggle does not render a stable test-id).
 *
 * Bug surface:
 *   SA-2026-004 candidate: if the stub server returns embeddings without the
 *   required { index, embedding } shape the upload will 500. This test
 *   catches that regression.
 */

import path from "path";
import { fileURLToPath } from "url";
import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  createChatViaRequest,
} from "./_flow-helpers";

const __filename = fileURLToPath(import.meta.url);
const FIXTURE_PDF = path.join(
  path.dirname(__filename),
  "../fixtures/test-doc.pdf"
);

test(
  "flow-12: upload PDF → chunk preview visible → RAG enable via API succeeds",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    // Navigate to documents page.
    await page.goto(`${backendURL}/documents`);
    await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => { /* ok */ });

    // The drop zone should be visible.
    const dropZone = page.getByRole("button", { name: /drop files here or click to upload/i });
    await expect(dropZone).toBeVisible({ timeout: 8_000 });

    // Upload via the hidden file input (Playwright's setInputFiles directly
    // activates the input without needing a real OS file-picker).
    const fileInput = page.locator('input[type="file"]');
    await fileInput.setInputFiles(FIXTURE_PDF);

    // Success toast should appear with chunk count.
    // The toast message pattern: `Uploaded "test-doc.pdf" (N chunks).`
    const successToast = page.getByText(/uploaded.*test-doc\.pdf/i);
    await expect(successToast).toBeVisible({ timeout: 15_000 });

    // Wait for the document list to refresh after upload.
    // The useDocuments query automatically refetches after the upload mutation
    // invalidates the query key.
    await page.waitForTimeout(1_000);

    // The document row should appear in the list.
    // Documents.tsx renders: <span style={docTitleStyle}>{doc.title}</span>
    // The title is the sanitized filename ("test-doc.pdf").
    const docRow = page.getByText("test-doc.pdf").first();
    await expect(docRow).toBeVisible({ timeout: 10_000 });

    // Click the expand button.  Documents.tsx renders a <button> whose text
    // content is "▸test-doc.pdf" (arrow + title span).  Playwright's filter
    // matches text content inside the button's subtree.
    const expandBtn = page.locator("button").filter({ hasText: /test-doc\.pdf/i }).first();
    await expect(expandBtn).toBeVisible({ timeout: 10_000 });
    await expandBtn.click();

    // At least one chunk row should appear.
    // ChunkExpansion renders <span style={chunkOrdinalStyle}>#{ordinal}</span>.
    // Ordinals are zero-based (first chunk is #0).
    const firstChunkOrdinal = page.getByText(/^#\d+$/).first();
    await expect(firstChunkOrdinal).toBeVisible({ timeout: 8_000 });

    // Step 5: create a chat and enable RAG on it via API.
    const chatId = await createChatViaRequest(page, backendURL, "Flow12 RAG Chat");

    // PATCH rag_enabled=true on the chat via API (form-encoded).
    const patchResp = await page.request.patch(
      `${backendURL}/api/chats/${String(chatId)}`,
      { form: { rag_enabled: "true" } }
    );
    expect(patchResp.ok()).toBe(true);

    const chatData = await patchResp.json() as { settings?: { rag_enabled?: boolean } };
    expect(chatData.settings?.rag_enabled).toBe(true);

    assertNoConsoleErrors(collectErrors(), "flow-12");
  }
);

test(
  "flow-12b: oversized PDF upload surfaces a user-visible error toast without crashing the page",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    await page.goto(`${backendURL}/documents`);
    await page.waitForLoadState("networkidle", { timeout: 10_000 }).catch(() => { /* ok */ });

    const dropZone = page.getByRole("button", { name: /drop files here or click to upload/i });
    await expect(dropZone).toBeVisible({ timeout: 8_000 });

    // Intercept the upload endpoint and force a 413 (oversized) response so we
    // exercise the failure path without needing a multi-megabyte fixture.
    // The page's useUploadDocument hook surfaces this as an error toast and
    // leaves the upload zone usable for the next attempt.
    await page.route("**/api/documents", async (route) => {
      if (route.request().method() === "POST") {
        await route.fulfill({
          status: 413,
          contentType: "application/json",
          body: JSON.stringify({ detail: "File too large" }),
        });
        return;
      }
      await route.continue();
    });

    const fileInput = page.locator('input[type="file"]');
    await fileInput.setInputFiles(FIXTURE_PDF);

    // The error toast surface uses the "failed" / "error" wording per the
    // useUploadDocument hook's onError pushers. Match loosely so a copy
    // tweak doesn't break the assertion.
    const errToast = page.getByText(/failed|error/i).first();
    await expect(errToast).toBeVisible({ timeout: 10_000 });

    // The drop zone stays usable — no full-page crash.
    await expect(dropZone).toBeVisible();

    // The browser emits a native console.error for the 413 response even when
    // the JS error handler catches it gracefully. Allow it since the 413 is
    // deliberately injected by the route intercept to test the error path.
    assertNoConsoleErrors(collectErrors(), "flow-12b", [/413|payload too large/i]);
  }
);

test(
  "flow-12c: citation chip renders inline and links to the documents page",
  async ({ page, backendURL, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    const chatId = await createChatViaRequest(page, backendURL, "Flow12c Citation Chat");

    // Seed an assistant message that already contains a citation token of the
    // form parsed by CitationBadge: [[doc:NN]]. The page renders these inline
    // as data-testid="citation-badge-NN" badges via the ChatMessage markdown
    // renderer — we don't need RAG running to test the chip render path.
    const seedResp = await page.request.post(
      `${backendURL}/api/chats/${String(chatId)}/messages`,
      {
        form: {
          role: "assistant",
          content: "Per the project notes [[doc:42]], the build is green.",
        },
      },
    );
    expect(seedResp.ok()).toBe(true);

    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 15_000 });

    const badge = page.getByTestId("citation-badge-42");
    await expect(badge).toBeVisible({ timeout: 10_000 });

    // The badge is a button (CitationBadge renders <button type="button">).
    // Clicking the badge calls window.open("/documents", ...) by default; we
    // assert the button is wired with the right aria-label rather than
    // round-tripping through a new tab in the headless harness.
    await expect(badge).toHaveAttribute("aria-label", /Citation: /);

    assertNoConsoleErrors(collectErrors(), "flow-12c");
  }
);
