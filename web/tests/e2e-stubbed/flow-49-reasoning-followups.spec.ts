/**
 * Flow 49 — Reasoning collapsible + follow-up chips.
 *
 * What it proves (route-stubbed):
 *   1. Stub SSE stream with reasoning + follow-up suggestions.
 *   2. Reasoning block collapses/expands on toggle.
 *   3. Follow-up chip click sends its text.
 */
import { test, expect } from "@playwright/test";

const AUTH_ME = { user_id: 1, username: "alice", is_admin: false, expires_at: "2026-12-01T00:00:00Z" };

test.describe("Flow 49 — Reasoning collapsible + follow-up chips", () => {
  test.beforeEach(async ({ page }) => {
    await page.route("**/api/auth/login", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(AUTH_ME) })
    );
    await page.route("**/api/auth/me/probe", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...AUTH_ME, needs_setup: false, totp_enabled: false, email: null, display_name: null, avatar_url: null }) })
    );
    await page.route("**/api/auth/setup_status", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ needs_setup: false }) })
    );
    await page.route("**/api/auth/me", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify(AUTH_ME) })
    );
    await page.route("**/api/models", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([
        { id: "test-model", object: "model", owned_by: "test" },
      ]) })
    );
    await page.route("**/api/settings/lmstudio", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ base_url: "http://localhost:1234", default_model: "", api_key_set: false, source_base_url: "unset", source_api_key: "unset", source_default_model: "unset", key_pruned: false, auth_failed: false }) })
    );
    await page.route("**/api/folders", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
    );
    await page.route("**/api/projects", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
    );
    await page.route("**/api/quotas/me", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ tokens_per_day: 50000, requests_per_day: 500, tokens_consumed_today: 0, requests_consumed_today: 0, resets_at: "2026-12-02T00:00:00Z" }) })
    );
    await page.route("**/api/memory/pins", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([]) })
    );
    await page.route("**/api/prompts", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
    );
    await page.route("**/api/integrations/available", (route) =>
      route.fulfill({ status: 200, contentType: "application/json", body: "[]" })
    );
    // SSE stream — returns reasoning + content with followup comment.
    await page.route("**/api/chats/49/stream", (route) => {
      if (route.request().method() !== "POST") return route.continue();
      const sse =
        "event: chat.start\ndata: {\"response_id\":\"r1\"}\n\n" +
        "event: reasoning.start\ndata: {}\n\n" +
        "event: reasoning.delta\ndata: {\"delta\":\"Let me think about quantum computing...\"}\n\n" +
        "event: reasoning.end\ndata: {}\n\n" +
        "event: message.delta\ndata: {\"delta\":\"Quantum computing uses qubits to process information in superposition.\"}\n\n" +
        "event: message.delta\ndata: {\"delta\":\"\\n\\n<!--followups:[\\\"What is superposition?\\\",\\\"How does entanglement work?\\\",\\\"Can I build one?\\\"]-->\"}\n\n" +
        "event: chat.end\ndata: {\"stop_reason\":\"stop\",\"total_output_tokens\":150}\n\n";
      return route.fulfill({ status: 200, contentType: "text/event-stream", body: sse });
    });

    // Stub the chat query (after refetch) to return the assistant message with reasoning + followups.
    await page.route("**/api/chats/49/messages**", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          messages: [
            { id: 491, chat_id: 49, role: "user", content: "Tell me about quantum computing", reasoning_content: null, created_at: "2026-06-01T12:00:01Z", state: "final", response_id: null, model_id: null },
            { id: 492, chat_id: 49, role: "assistant", content: "Quantum computing uses qubits to process information in superposition.\n\n<!--followups:[\"What is superposition?\",\"How does entanglement work?\",\"Can I build one?\"]-->", reasoning_content: "Let me think about quantum computing... Qubits exist in superposition states, entanglement allows correlations, and quantum gates manipulate them.", created_at: "2026-06-01T12:00:02Z", state: "final", response_id: "r1", model_id: null },
          ],
          has_more: false,
        }),
      });
    });

    // Chat catch-all: must come AFTER specific chat sub-path routes.
    await page.route("**/api/chats**", (route) => {
      const method = route.request().method();
      const url = new URL(route.request().url());
      const path = url.pathname;
      if (method === "GET" && path === "/api/chats") {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify([
          { id: 49, user_id: 1, title: "Reasoning chat", folder: null, pinned: false, created_at: "2026-06-01T12:00:00Z", updated_at: "2026-06-01T12:00:00Z", settings: {}, display_order: 0, incognito: false, incognito_expires_at: null, model_id: null },
        ]) });
      }
      if (method === "GET" && path === "/api/chats/49") {
        return route.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({
          id: 49, user_id: 1, title: "Reasoning chat", messages: [
            { id: 491, chat_id: 49, role: "user", content: "Tell me about quantum computing", reasoning_content: null, created_at: "2026-06-01T12:00:01Z", state: "final", response_id: null, model_id: null },
          ], has_more: false,
        }) });
      }
      // Let through to more specific handlers or real backend
      return route.continue();
    });
  });

  test.fixme("reasoning SSE lifecycle test: chat page crashed with error boundary due to malformed messages response; needs correct message wire shape matching the real API", async ({ page }) => {
    await page.goto("/chats/49");
    // Use role selector to avoid matching send button's aria-label.
    await expect(page.getByRole("textbox", { name: "Message" })).toBeVisible({ timeout: 10000 });
    await expect(page.getByTestId("topbar-overflow-trigger")).toBeVisible();

    // Type a message and send to trigger the stream.
    const textarea = page.getByRole("textbox", { name: "Message" });
    await textarea.fill("Tell me about quantum computing");
    await page.getByLabel("Send message").click();

    // Wait for the unified process surface to appear (during streaming).
    // ThinkingBlock → ProcessStream: reasoning now lives in one calm surface.
    const reasoningBlock = page.locator(".lmchat-process-reasoning");
    await expect(reasoningBlock).toBeVisible({ timeout: 10000 });

    // Once the answer has started the reasoning collapses to a "Reasoning"
    // toggle line; aria-expanded="false" by default (collapsed).
    const toggleBtn = reasoningBlock.locator("button").first();
    await expect(toggleBtn).toHaveAttribute("aria-expanded", "false");

    // Click to expand.
    await toggleBtn.click();
    await expect(toggleBtn).toHaveAttribute("aria-expanded", "true");

    // Wait for stream completion and follow-up chips to appear.
    const followupGroup = page.locator("[aria-label='Follow-up suggestions']");
    await expect(followupGroup).toBeVisible({ timeout: 10000 });

    // Assert follow-up chips are present.
    const chips = followupGroup.locator("[data-testid='followup-chip']");
    await expect(chips).toHaveCount(3);

    // Click the first follow-up chip and assert it sends the text.
    const firstChip = chips.first();
    await expect(firstChip).toContainText("What is superposition?");
    await firstChip.click();

    // After clicking, the follow-up text should appear in the textarea.
    await expect(textarea).toHaveValue(/What is superposition/);
  });
});