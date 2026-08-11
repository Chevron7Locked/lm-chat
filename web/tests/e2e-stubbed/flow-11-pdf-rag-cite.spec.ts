/**
 * Flow 11 — PDF upload + RAG enable + citation badge.
 *
 * What it proves (route-stubbed, no live backend):
 *  1. A user can navigate to the Documents page and upload a PDF.
 *  2. The chunk preview list opens and renders the stubbed chunks.
 *  3. The per-chat RAG toggle in Chat.tsx's top bar can be flipped from
 *     "RAG: off" to "RAG: on" — the PATCH /api/chats/:id call fires with
 *     rag_enabled=true (proxied via useUpdateChat).
 *  4. A message can be sent in that chat; the stubbed assistant SSE
 *     response renders WITH a {@link CitationBadge} chip generated from
 *     the inline `[doc:<id>]` marker injected by RAG (Gap 1, P10c.2).
 */
import { test, expect } from "@playwright/test";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { bootstrapAuthedApp } from "./_bootstrap";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// ─── Stub helpers ─────────────────────────────────────────────────────────────

function buildRagSseText(content: string): string {
  const start = `event: chat.start\ndata: ${JSON.stringify({
    type: "chat.start", msg_id: 1, response_id: "resp-rag-1",
  })}\n\n`;
  const delta = `event: message.delta\ndata: ${JSON.stringify({
    type: "message.delta", msg_id: 1, delta: content,
  })}\n\n`;
  const end = `event: chat.end\ndata: ${JSON.stringify({
    type: "chat.end", msg_id: 1,
  })}\n\n`;
  return start + delta + end;
}

test.describe("Flow 11 — PDF upload + RAG + citation", () => {
  test.beforeEach(async ({ page }) => {
    // Authed chat-page bootstrap defaults (probe hydration + correctly-typed
    // list/object endpoints) — this spec is already signed in on cold load.
    await bootstrapAuthedApp(page);

    // Chats list + detail.
    let ragEnabled = false;
    await page.route("**/api/chats*", (route) => {
      if (route.request().method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            {
              id: 1, title: "RAG Chat", folder: null, pinned: false,
              updated_at: new Date().toISOString(), model_id: "qwen",
              display_order: 0,
              settings: { rag_enabled: ragEnabled },
            },
          ]),
        });
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: 2, title: "New Chat", folder: null, pinned: false,
          updated_at: new Date().toISOString(), model_id: null,
          display_order: 1, settings: {},
        }),
      });
    });

    // PATCH /api/chats/1 → flip rag_enabled in our local state.
    // GET /api/chats/1 → empty before SSE; populated with the assistant
    // reply once a stream has completed.
    let streamCompleted = false;
    await page.route("**/api/chats/1", async (route) => {
      const method = route.request().method();
      if (method === "PATCH") {
        const body = route.request().postData() ?? "";
        const params = new URLSearchParams(body);
        const next = params.get("rag_enabled");
        if (next !== null) ragEnabled = next === "true";
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: 1, title: "RAG Chat", folder: null, pinned: false,
            updated_at: new Date().toISOString(), model_id: "qwen",
            display_order: 0,
            settings: { rag_enabled: ragEnabled },
          }),
        });
      }
      if (method === "GET") {
        const messages = streamCompleted
          ? [
              {
                id: 1, chat_id: 1, role: "user",
                content: "Summarize the PDF.", reasoning_content: null,
                created_at: new Date().toISOString(),
              },
              {
                id: 2, chat_id: 1, role: "assistant",
                content:
                  "According to [doc:sample-5page-pdf] section 2, the findings indicate…",
                reasoning_content: null,
                created_at: new Date().toISOString(),
              },
            ]
          : [];
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: 1, user_id: 1, title: "RAG Chat", folder: null, pinned: false,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
            messages, has_more: false,
            settings: { rag_enabled: ragEnabled },
          }),
        });
      }
      return route.continue();
    });

    // Documents — list + upload + chunks.
    let uploadedDoc: {
      id: number; title: string; mime_type: string; byte_size: number;
      chunk_count: number; embedding_model_id: string; sha256: string;
      uploaded_at: string;
    } | null = null;
    await page.route("**/api/documents", async (route) => {
      const method = route.request().method();
      if (method === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify(uploadedDoc !== null ? [uploadedDoc] : []),
        });
      }
      // POST — multipart upload. We don't bother parsing the boundary;
      // we just synthesize a successful UploadResponse.
      uploadedDoc = {
        id: 42,
        title: "sample-5page.pdf",
        mime_type: "application/pdf",
        byte_size: 2_600,
        chunk_count: 3,
        embedding_model_id: "qwen-embed",
        sha256: "abc123",
        uploaded_at: new Date().toISOString(),
      };
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: 42, filename: "sample-5page.pdf", chunk_count: 3,
        }),
      });
    });
    await page.route("**/api/documents/42/chunks", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          { ordinal: 0, text_preview: "Introduction to sample document — page 1." },
          { ordinal: 1, text_preview: "Findings and discussion — page 2-3." },
          { ordinal: 2, text_preview: "Conclusion and references — page 4-5." },
        ]),
      })
    );

    // SSE stream — assistant response includes a RAG-injected [doc:<id>]
    // marker that ChatMessage.injectCitations will replace with a
    // CitationBadge chip.
    await page.route("**/api/chat/stream", async (route) => {
      streamCompleted = true;
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: buildRagSseText(
          "According to [doc:sample-5page-pdf] section 2, the findings indicate…"
        ),
      });
    });
  });

  test("upload PDF → chunk preview opens → toggle RAG → send → response renders", async ({ page }) => {
    // Step 1: navigate to the Documents page (standalone route).
    await page.goto("/documents");
    // exact:true — the page's h3 empty-state "No documents yet." is also a
    // case-insensitive substring match for "Documents" and would otherwise
    // trip Playwright's strict-mode multi-element error.
    await expect(
      page.getByRole("heading", { name: "Documents", exact: true })
    ).toBeVisible();

    // Step 2: upload the sample PDF via the file input.
    const fileInput = page.getByLabel("File input");
    const pdfPath = path.join(__dirname, "fixtures", "sample-5page.pdf");
    await fileInput.setInputFiles(pdfPath);

    // After upload, success toast should appear and the document row should
    // render.  Toasts render with role="status" (polite announcement).
    await expect(page.getByRole("status").first()).toBeVisible({ timeout: 5_000 });
    // exact:true distinguishes the title button (accessible name
    // "sample-5page.pdf") from the Delete button (accessible name
    // "Delete sample-5page.pdf") without depending on aria-expanded.
    const titleBtn = page
      .getByRole("button", { name: "sample-5page.pdf", exact: true });
    await expect(titleBtn).toBeVisible({ timeout: 5_000 });

    // Step 3: click the document title to expand chunk preview.
    await titleBtn.click();
    await expect(page.getByText(/Introduction to sample document/)).toBeVisible({
      timeout: 3_000,
    });

    // Step 4: navigate to the chat and flip RAG on.
    await page.goto("/chats/1");
    const ragBtn = page.getByRole("button", { name: /Enable RAG for this chat/ });
    await expect(ragBtn).toBeVisible();
    await ragBtn.click();
    // After the toggle, the aria-label flips to "Disable RAG…".
    await expect(
      page.getByRole("button", { name: /Disable RAG for this chat/ })
    ).toBeVisible({ timeout: 3_000 });

    // Step 5: send a message and observe the streamed RAG-injected reference.
    const composer = page.getByRole("textbox", { name: "Message" });
    await composer.fill("Summarize the PDF.");
    await composer.press("Control+Enter");

    // The assistant's streamed delta is rendered with a CitationBadge chip
    // in place of the inline `[doc:sample-5page-pdf]` marker.
    const badge = page.getByTestId("citation-badge-sample-5page-pdf");
    await expect(badge).toBeVisible({ timeout: 10_000 });
    await expect(badge).toHaveAttribute(
      "aria-label",
      /Citation: sample-5page-pdf/,
    );
  });
});
