/**
 * Flow 55 — Mid-conversation provider switch.
 *
 * What it proves (route-stubbed):
 *   1. /api/models returns models from two providers: lmstudio and openrouter.
 *   2. Start with an lmstudio model; send a message — stream body carries
 *      the lmstudio model id.
 *   3. Switch the header dropdown to an openrouter model — the PATCH
 *      /api/chats/:id request carries provider=openrouter.
 *   4. Send a second message — stream body carries the openrouter model id
 *      (bare id, not the composite "openrouter::..." prefix).
 *
 * Provider routing mechanism:
 *   - The FE dropdown value is composite "<provider>::<model_id>".
 *   - onModelChange decodes it → provider + model_id → calls
 *     updateChat.mutate({ model_id, provider }).
 *   - The next Composer submit sets payload.model = selectedModel (bare id).
 *   - The backend reads provider from the persisted chat.settings.provider
 *     to route the request to the correct upstream.
 *
 * This spec pins the FE-side contract:
 *   a) PATCH carries provider=openrouter when openrouter model selected.
 *   b) stream payload.model is the bare openrouter model id (no prefix).
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

const CHAT_ID = 55;

function buildSse(rid: string): string {
  return (
    `event: chat.start\ndata: ${JSON.stringify({ response_id: rid })}\n\n` +
    `event: message.delta\ndata: ${JSON.stringify({ delta: "reply" })}\n\n` +
    `event: chat.end\ndata: ${JSON.stringify({ stop_reason: "stop" })}\n\n`
  );
}

test.describe("Flow 55 — Mid-conversation provider switch", () => {
  test.beforeEach(async ({ page }) => {
    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);

    // Two providers: one lmstudio local model + one openrouter cloud model.
    await page.route("**/api/models", (route) => {
      if (route.request().method() !== "GET") return route.fallback();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            key: "qwen3-local",
            display_name: "Qwen 3 (local)",
            provider: "lmstudio",
            loaded_instances: 1,
            loaded_instance_ids: ["qwen3-local"],
            capabilities: { vision: false, trained_for_tool_use: false, reasoning: null, embedding: false },
            max_context_length: 8192,
            loaded_context_length: 0,
            size_bytes: 0,
            params_string: "",
            quantization: null,
          },
          {
            key: "anthropic/claude-3-5-sonnet",
            display_name: "Claude 3.5 Sonnet",
            provider: "openrouter",
            loaded_instances: 0,
            loaded_instance_ids: [],
            capabilities: { vision: true, trained_for_tool_use: true, reasoning: null, embedding: false },
            max_context_length: 200000,
            loaded_context_length: 0,
            size_bytes: 0,
            params_string: "",
            quantization: null,
          },
        ]),
      });
    });
  });

  test("switching to openrouter model: PATCH carries provider=openrouter, stream body carries bare model id", async ({
    page,
  }) => {
    const capturedModels: string[] = [];
    let patchedProvider: string | null = null;
    let streamCallCount = 0;

    // Stream — capture payload.model for each call.
    await page.route("**/api/chat/stream", async (route) => {
      if (route.request().method() !== "POST") return route.fallback();
      const raw = route.request().postData() ?? "{}";
      const body = JSON.parse(raw) as {
        payload?: { model?: string };
      };
      capturedModels.push(body.payload?.model ?? "");
      streamCallCount++;
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: buildSse(`rid-${String(streamCallCount)}`),
      });
    });

    // Unified chats handler — list (matches ?unscoped=true via trailing **),
    // detail, and PATCH (captures provider).
    await page.route("**/api/chats**", async (route) => {
      const method = route.request().method();
      const path = new URL(route.request().url()).pathname;
      if (method === "PATCH" && path === `/api/chats/${String(CHAT_ID)}`) {
        const raw = route.request().postData() ?? "";
        const params = new URLSearchParams(raw);
        if (params.has("provider")) {
          patchedProvider = params.get("provider");
        }
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: CHAT_ID,
            user_id: 1,
            title: "Provider switch test",
            folder: null,
            pinned: false,
            created_at: "2026-06-01T12:00:00Z",
            updated_at: "2026-06-01T12:00:00Z",
            settings: {
              provider: params.get("provider") ?? "lmstudio",
              model_id: params.get("model_id") ?? "qwen3-local",
            },
            display_order: 0,
          }),
        });
      }
      if (method === "GET" && path === "/api/chats") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            {
              id: CHAT_ID,
              user_id: 1,
              title: "Provider switch test",
              folder: null,
              pinned: false,
              created_at: "2026-06-01T12:00:00Z",
              updated_at: "2026-06-01T12:00:00Z",
              settings: { provider: "lmstudio" },
              display_order: 0,
              incognito: false,
              incognito_expires_at: null,
              model_id: "qwen3-local",
            },
          ]),
        });
      }
      if (method === "GET" && path === `/api/chats/${String(CHAT_ID)}`) {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: CHAT_ID,
            user_id: 1,
            title: "Provider switch test",
            messages: [],
            has_more: false,
            settings: { provider: "lmstudio" },
          }),
        });
      }
      return route.fallback();
    });

    await page.goto(`/chats/${String(CHAT_ID)}`);
    const composer = page.getByRole("textbox", { name: "Message" });
    await expect(composer).toBeVisible({ timeout: 10_000 });

    // Turn 1 — lmstudio model (default).
    await composer.fill("first message on lmstudio");
    await page.getByLabel("Send message").click();
    await expect.poll(() => streamCallCount >= 1, { timeout: 8_000 }).toBe(true);
    expect(capturedModels[0]).toBe("qwen3-local");

    // Wait for Composer to re-enable (stream complete).
    await expect(composer).toBeEnabled({ timeout: 8_000 });

    // Switch the dropdown to the openrouter model.
    // The dropdown value is the composite "openrouter::anthropic/claude-3-5-sonnet".
    const modelSelect = page.getByTestId("chat-header-model-select");
    await expect(modelSelect).toBeVisible();
    await modelSelect.selectOption({
      value: "openrouter::anthropic/claude-3-5-sonnet",
    });

    // PATCH should have fired with provider=openrouter.
    await expect
      .poll(() => patchedProvider, { timeout: 4_000 })
      .toBe("openrouter");

    // Turn 2 — openrouter model.  The bare model_id (no composite prefix)
    // must appear in the stream payload.
    await composer.fill("second message on openrouter");
    await page.getByLabel("Send message").click();
    await expect.poll(() => streamCallCount >= 2, { timeout: 8_000 }).toBe(true);
    expect(capturedModels[1]).toBe("anthropic/claude-3-5-sonnet");
  });
});
