/**
 * Flow 53 — Preset payload verification.
 *
 * What it proves (route-stubbed):
 *   For the /code (coder) and /research presets:
 *   1. Activate the sub-agent via its slash command in the Composer.
 *   2. The sub-session panel opens (persona label visible).
 *   3. Send a message inside the sub-session — the intercepted sub-session
 *      stream request carries the preset's distinctive system_prompt phrase.
 *
 * Cross-check source: web/src/lib/presets.ts
 *   coder:    system_prompt starts with "Software engineering mode"
 *   research: system_prompt starts with "Research mode"
 *
 * Mechanism (verified against the live app, 2026-06-20 new model):
 *   - A preset slash command (/code, /research) in the Composer calls
 *     onPresetActivate → Chat.tsx opens a clean-context SUB-SESSION.
 *     It does NOT write active_preset and does NOT show the Composer badge.
 *   - The next message routes to
 *     POST /api/chats/:id/sub-session/stream (multipart/form-data), NOT the
 *     main /api/chat/stream.  Its `system_prompt` form field is built from
 *     the preset template by subSession.buildSubSessionSystemPrompt, which
 *     preserves the preset body verbatim (framed with a leading date line
 *     and a trailing tool-availability block — presets carry no inline
 *     {{tools}}/{{current_date}} tokens).
 *   - The sub-session form body carries model_id / provider / system_prompt /
 *     messages_json / integrations.  It does NOT carry a `temperature` field
 *     on the wire — so this spec asserts the system_prompt phrase + model_id
 *     (the real transmitted contract) rather than a temperature that is not
 *     sent on the sub-session path.
 */
import { test, expect, type Page, type Route } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

/** Build a minimal SSE turn (start → delta → end). */
function buildSse(rid: string): string {
  return (
    `event: chat.start\ndata: ${JSON.stringify({ response_id: rid })}\n\n` +
    `event: message.delta\ndata: ${JSON.stringify({ delta: "reply" })}\n\n` +
    `event: chat.end\ndata: ${JSON.stringify({ stop_reason: "stop" })}\n\n`
  );
}

/** Standard beforeEach: bootstrap auth + the chat this spec targets. */
async function stubCommon(
  page: Page,
  chatId: number,
) {
  // Authed chat-page bootstrap defaults (probe hydration + correctly-typed
  // list/object endpoints, including a "qwen3" default model matching
  // this chat's model_id).
  await bootstrapAuthedApp(page);

  // Chat routes — a single **/api/chats** handler so the trailing ** also
  // matches the ?unscoped=true / ?project_id=… query variants that
  // useChatsDirect issues.  (A bare "**/api/chats" would NOT match a URL
  // with a query string and the list would fall through to bootstrap's
  // default [], breaking currentChat resolution.)
  await page.route("**/api/chats**", async (route: Route) => {
    const method = route.request().method();
    const path = new URL(route.request().url()).pathname;
    if (method === "PATCH" && path === `/api/chats/${String(chatId)}`) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: chatId,
          user_id: 1,
          title: "Preset test",
          folder: null,
          pinned: false,
          created_at: "2026-06-01T12:00:00Z",
          updated_at: "2026-06-01T12:00:00Z",
          settings: {},
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
            id: chatId,
            user_id: 1,
            title: "Preset test",
            folder: null,
            pinned: false,
            created_at: "2026-06-01T12:00:00Z",
            updated_at: "2026-06-01T12:00:00Z",
            settings: {},
            display_order: 0,
            incognito: false,
            incognito_expires_at: null,
            model_id: "qwen3",
          },
        ]),
      });
    }
    if (method === "GET" && path === `/api/chats/${String(chatId)}`) {
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: chatId,
          user_id: 1,
          title: "Preset test",
          messages: [],
          has_more: false,
        }),
      });
    }
    // rag_mode + anything else → defer to bootstrap's defaults.
    return route.fallback();
  });
}

test.describe("Flow 53 — Preset payload verification", () => {
  // Activating a preset via its slash command (/code, /research) opens a
  // clean-context SUB-SESSION (Composer.dispatchSlashCommand →
  // onPresetActivate → Chat.tsx startSubSession).  The next message therefore
  // routes to POST /api/chats/:id/sub-session/stream — a multipart/form-data
  // request whose `system_prompt` field is built from the preset template
  // (subSession.buildSubSessionSystemPrompt frames it with a leading date
  // line and a trailing tool-availability block, preserving the preset's
  // body verbatim — presets carry no inline {{tools}}/{{current_date}}
  // tokens).  We assert the preset's
  // distinctive system-prompt phrase reaches that field, plus the resolved
  // model_id.
  //
  // NOTE ON TEMPERATURE: the sub-session stream wire (useSubSessionSSE.stream)
  // sends `model_id` / `provider` / `system_prompt` / `messages_json` /
  // `integrations` — it does NOT carry `temperature` on the form body (the
  // preset temperature is applied server-side / baked into the prompt build).
  // So this spec verifies the preset's system_prompt phrase reaches the wire
  // (the load-bearing contract) rather than asserting a temperature field that
  // genuinely is not transmitted on the sub-session path.

  test("coder preset (/code) → sub-session stream carries 'Software engineering mode' system prompt", async ({
    page,
  }) => {
    const chatId = 531;
    await stubCommon(page, chatId);

    let subBody: string | null = null;
    await page.route("**/api/chats/*/sub-session/stream", (route: Route) => {
      if (route.request().method() !== "POST") return route.fallback();
      subBody = route.request().postData() ?? "";
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: buildSse("rid-531"),
      });
    });

    await page.goto(`/chats/${String(chatId)}`);
    const composer = page.getByRole("textbox", { name: "Message" });
    await expect(composer).toBeVisible({ timeout: 10_000 });

    // Launch the Coder sub-agent via /code slash command.
    await composer.fill("/code");
    await composer.press("Control+Enter");

    // The sub-session panel should appear — slash command does NOT set
    // active_preset, so the Composer preset badge must NOT appear.
    await expect(page.locator(".lmchat-subsession-label")).toBeVisible({ timeout: 6_000 });
    await expect(page.locator(".lmchat-subsession-label")).toContainText(/Coder/i);
    await expect(page.getByTestId("composer-preset-badge")).toHaveCount(0);

    // Send a follow-up message inside the sub-session — routes through the sub-session stream.
    await composer.fill("write me a hello world");
    await composer.press("Control+Enter");

    await expect
      .poll(() => subBody !== null, { timeout: 10_000 })
      .toBe(true);

    const raw = subBody as unknown as string;
    // Cross-check: presets.ts coder.system_prompt starts with "Software engineering mode".
    expect(raw).toContain("Software engineering mode");
    // The resolved model id is forwarded on the sub-session form body.
    expect(raw).toContain("model_id");
    expect(raw).toContain("qwen3");
  });

  test("research preset (/research) → sub-session stream carries 'Research mode' system prompt", async ({
    page,
  }) => {
    const chatId = 532;
    await stubCommon(page, chatId);

    let subBody: string | null = null;
    await page.route("**/api/chats/*/sub-session/stream", (route: Route) => {
      if (route.request().method() !== "POST") return route.fallback();
      subBody = route.request().postData() ?? "";
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: buildSse("rid-532"),
      });
    });

    await page.goto(`/chats/${String(chatId)}`);
    const composer = page.getByRole("textbox", { name: "Message" });
    await expect(composer).toBeVisible({ timeout: 10_000 });

    // Launch the Research sub-agent via /research slash command.
    await composer.fill("/research");
    await composer.press("Control+Enter");

    // The sub-session panel should appear; no Composer badge (no active_preset set).
    await expect(page.locator(".lmchat-subsession-label")).toBeVisible({ timeout: 6_000 });
    await expect(page.locator(".lmchat-subsession-label")).toContainText(/Research/i);
    await expect(page.getByTestId("composer-preset-badge")).toHaveCount(0);

    // Send a follow-up inside the sub-session — routes through the sub-session stream.
    await composer.fill("summarize quantum computing papers");
    await composer.press("Control+Enter");

    await expect
      .poll(() => subBody !== null, { timeout: 10_000 })
      .toBe(true);

    const raw = subBody as unknown as string;
    // Cross-check: presets.ts research.system_prompt starts with "Research mode".
    expect(raw).toContain("Research mode");
    expect(raw).toContain("model_id");
    expect(raw).toContain("qwen3");
  });
});
