/**
 * Flow 51 — Model swap → stream request carries new model_id.
 *
 * What it proves (route-stubbed):
 *   1. The header model dropdown offers multiple models.
 *   2. Select model A, send → stream body carries model A.
 *   3. Select model B, send → stream body carries model B (not A).
 *
 * Key facts:
 *   - The stream endpoint is POST /api/chat/stream with JSON body
 *     { chat_id, payload }.  payload.model is the bare model id
 *     (not the composite provider::model_id used by the dropdown display).
 *   - The dropdown value is the composite "lmstudio::model-id".  Selecting
 *     it fires onModelChange which strips the prefix and sets selectedModel
 *     (plain model id), which Composer forwards as payload.model.
 *   - PATCH /api/chats/:id is called on dropdown change — stub it so the
 *     optimistic update doesn't roll back and clear the selection.
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

const CHAT_ID = 51;

/** Minimal SSE frame sequence: start → delta → end. */
function buildSse(responseId: string): string {
  return (
    `event: chat.start\ndata: ${JSON.stringify({ response_id: responseId })}\n\n` +
    `event: message.delta\ndata: ${JSON.stringify({ delta: "ok" })}\n\n` +
    `event: chat.end\ndata: ${JSON.stringify({ stop_reason: "stop" })}\n\n`
  );
}

test.describe("Flow 51 — Model swap request", () => {
  test.beforeEach(async ({ page }) => {
    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);

    // Two models in the dropdown — both LM Studio (provider defaults to "lmstudio").
    await page.route("**/api/models", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            key: "model-alpha",
            display_name: "Model Alpha",
            provider: "lmstudio",
            loaded_instances: 1,
            loaded_instance_ids: ["model-alpha"],
            capabilities: { vision: false, trained_for_tool_use: false, reasoning: null, embedding: false },
            max_context_length: 4096,
            loaded_context_length: 0,
            size_bytes: 0,
            params_string: "",
            quantization: null,
          },
          {
            key: "model-beta",
            display_name: "Model Beta",
            provider: "lmstudio",
            loaded_instances: 1,
            loaded_instance_ids: ["model-beta"],
            capabilities: { vision: false, trained_for_tool_use: false, reasoning: null, embedding: false },
            max_context_length: 4096,
            loaded_context_length: 0,
            size_bytes: 0,
            params_string: "",
            quantization: null,
          },
        ]),
      });
    });

    // Chat routes.
    await page.route("**/api/chats**", (route) => {
      const method = route.request().method();
      const url = new URL(route.request().url());
      const path = url.pathname;
      if (method === "PATCH" && path === `/api/chats/${String(CHAT_ID)}`) {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: CHAT_ID,
            user_id: 1,
            title: "Model swap test",
            folder: null,
            pinned: false,
            created_at: "2026-06-01T12:00:00Z",
            updated_at: "2026-06-01T12:00:00Z",
            settings: {},
            display_order: 0,
            model_id: null,
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
              title: "Model swap test",
              folder: null,
              pinned: false,
              created_at: "2026-06-01T12:00:00Z",
              updated_at: "2026-06-01T12:00:00Z",
              settings: {},
              display_order: 0,
              incognito: false,
              incognito_expires_at: null,
              model_id: "model-alpha",
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
            title: "Model swap test",
            messages: [],
            has_more: false,
          }),
        });
      }
      return route.fallback();
    });
  });

  test("second send after model swap carries new model id in stream body", async ({ page }) => {
    const capturedModels: string[] = [];
    let callCount = 0;

    // Intercept the REAL stream endpoint: POST /api/chat/stream (JSON body).
    await page.route("**/api/chat/stream", async (route) => {
      if (route.request().method() !== "POST") return route.continue();
      const raw = route.request().postData() ?? "{}";
      const parsed = JSON.parse(raw) as { chat_id?: number; payload?: { model?: string } };
      capturedModels.push(parsed.payload?.model ?? "");
      callCount++;
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: buildSse(`r${String(callCount)}`),
      });
    });

    await page.goto(`/chats/${String(CHAT_ID)}`);
    await expect(page.getByRole("textbox", { name: "Message" })).toBeVisible({
      timeout: 10_000,
    });

    // First send — model-alpha is the pre-selected model.
    await page.getByRole("textbox", { name: "Message" }).fill("first message");
    await page.getByLabel("Send message").click();
    await expect.poll(() => capturedModels.length, { timeout: 8_000 }).toBeGreaterThanOrEqual(1);
    expect(capturedModels[0]).toBe("model-alpha");

    // Wait for the stream to complete so the state resets to idle.
    await expect(page.getByRole("textbox", { name: "Message" })).toBeEnabled({
      timeout: 8_000,
    });

    // Switch to model-beta via the header dropdown.
    // The dropdown value is the composite "lmstudio::model-beta".
    const modelSelect = page.getByTestId("chat-header-model-select");
    await expect(modelSelect).toBeVisible();
    await modelSelect.selectOption({ value: "lmstudio::model-beta" });

    // Second send — must carry model-beta.
    await page.getByRole("textbox", { name: "Message" }).fill("second message");
    await page.getByLabel("Send message").click();
    await expect.poll(() => capturedModels.length, { timeout: 8_000 }).toBeGreaterThanOrEqual(2);
    expect(capturedModels[1]).toBe("model-beta");
  });
});
