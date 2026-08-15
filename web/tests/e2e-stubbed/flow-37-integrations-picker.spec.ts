/**
 * Flow 37 — P13h remote MCP server picker.
 *
 * What it proves (route-stubbed):
 *  1. The admin configures two integrations via /api/integrations/available
 *     — one with enabled_by_default=true, one with enabled_by_default=false.
 *  2. On opening a new chat, the composer chip-row renders both, with the
 *     default-on integration pre-selected (aria-checked=true) and the
 *     opt-in one unselected.
 *  3. The user toggles the opt-in integration on, then sends a message.
 *  4. The outbound POST /api/chat/stream body carries the union of both
 *     integration IDs in payload.integrations.
 *
 * The stream itself is fulfilled with a synthetic SSE response so the
 * page state moves through to message.end without an actual LM Studio
 * dependency.
 */
import { test, expect, type Request } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";
import type { ChatStreamPayload } from "@/hooks/useSSE";

// ─── SSE helper ──────────────────────────────────────────────────────────────

function buildSseText(content: string): string {
  const chatStart = `event: chat.start\ndata: ${JSON.stringify({
    type: "chat.start",
    msg_id: 1,
    response_id: "resp-1",
  })}\n\n`;
  const delta = `event: message.delta\ndata: ${JSON.stringify({
    type: "message.delta",
    msg_id: 1,
    delta: content,
  })}\n\n`;
  const chatEnd = `event: chat.end\ndata: ${JSON.stringify({
    type: "chat.end",
    msg_id: 1,
  })}\n\n`;
  return chatStart + delta + chatEnd;
}

// ─── Test ────────────────────────────────────────────────────────────────────

const NOW = "2026-05-22T12:00:00Z";

test.describe("Flow 37 — P13h MCP integrations picker", () => {
  test("default-on seeds composer + per-message toggles ride on payload.integrations", async ({
    page,
  }) => {
    // Authed as an admin — bootstrap covers array/object defaults; this
    // spec is already signed in on cold load.
    await bootstrapAuthedApp(page, { isAdmin: true });

    // Composer.tsx gates the integration-pill row on
    // modelCapabilities.trained_for_tool_use — bootstrap's default model
    // has that false, so override with a tool-trained model.
    await page.route("**/api/models", (route) => {
      if (route.request().method() !== "GET") return route.fallback();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            key: "qwen3",
            display_name: "Qwen 3",
            provider: "lmstudio",
            loaded_instances: 1,
            loaded_instance_ids: ["qwen3"],
            capabilities: { vision: false, trained_for_tool_use: true, reasoning: null, embedding: false },
            max_context_length: 8192,
            loaded_context_length: 0,
            size_bytes: 0,
            params_string: "",
            quantization: null,
          },
        ]),
      });
    });

    // ── Integrations registry: admin configured 2 entries ──────────────
    // The first is default-on (enabled_by_default=true), the second is
    // opt-in (enabled_by_default=false).  Composer must pre-select the
    // first on mount and leave the second unselected.
    await page.route("**/api/integrations/available", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify([
          {
            id: 1,
            value: "mcp/searxng",
            sort_order: 0,
            enabled_by_default: true,
            created_at: NOW,
            updated_at: NOW,
          },
          {
            id: 2,
            value: "mcp/filesystem",
            sort_order: 1,
            enabled_by_default: false,
            created_at: NOW,
            updated_at: NOW,
          },
        ]),
      }),
    );

    // ── Chats list + single chat ───────────────────────────────────────
    const chatSummary = {
      id: 1,
      title: "Picker chat",
      folder: null,
      pinned: false,
      updated_at: NOW,
      model_id: null,
    };
    await page.route("**/api/chats*", (route) => {
      if (route.request().method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([chatSummary]),
        });
      }
      return route.continue();
    });
    await page.route("**/api/chats/1", (route) => {
      if (route.request().method() !== "GET") return route.continue();
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: 1,
          user_id: 1,
          title: "Picker chat",
          folder: null,
          pinned: false,
          created_at: NOW,
          updated_at: NOW,
          messages: [],
        }),
      });
    });

    // ── Stream capture: capture the POST body, then fulfill an SSE ─────
    const capturedStreamBody: { value: Record<string, unknown> | null } = { value: null };
    await page.route("**/api/chat/stream", async (route, request: Request) => {
      try {
        const raw = request.postData();
        if (raw !== null) {
          capturedStreamBody.value = JSON.parse(raw);
        }
      } catch {
        // ignore
      }
      await route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: buildSseText("ok"),
      });
    });

    // ── Land on root already authed → open the chat ────────────────────
    await page.goto("/");

    await page.getByText("Picker chat").click();
    await page.waitForURL("**/chats/1");

    // ── Assert default-on seed ─────────────────────────────────────────
    const searxng = page.getByTestId("integration-pill-mcp/searxng");
    const filesystem = page.getByTestId("integration-pill-mcp/filesystem");
    await expect(searxng).toBeVisible();
    await expect(filesystem).toBeVisible();
    await expect(searxng).toHaveAttribute("aria-checked", "true");
    await expect(filesystem).toHaveAttribute("aria-checked", "false");

    // ── Toggle the opt-in integration on ────────────────────────────────
    await filesystem.click();
    await expect(filesystem).toHaveAttribute("aria-checked", "true");

    // ── Send a message ─────────────────────────────────────────────────
    const composer = page.getByRole("textbox", { name: "Message" });
    await composer.fill("hello");
    await composer.press("Control+Enter");

    // Wait for the stream to be initiated.
    await page.waitForFunction(() => true, { timeout: 2000 }).catch(() => undefined);
    await expect.poll(() => capturedStreamBody.value !== null, { timeout: 5000 }).toBe(true);

    // ── Assert the outbound payload carries BOTH integrations ──────────
    expect(capturedStreamBody.value).not.toBeNull();
    if (capturedStreamBody.value === null) throw new Error("expected capturedStreamBody to be captured");
    const body = capturedStreamBody.value as { payload?: ChatStreamPayload };
    expect(body.payload?.integrations).toEqual([
      "mcp/searxng",
      "mcp/filesystem",
    ]);
  });
});
