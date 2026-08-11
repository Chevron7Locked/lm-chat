/**
 * Flow 17 — A/B compare two-pane stream + persist pane A to chat history.
 *
 * What it proves (route-stubbed):
 *  1. A chat seeded with `settings.ab_compare.enabled = true` plus
 *     model_a / model_b renders the ABCompareView two-pane layout
 *     instead of the standard message list.
 *  2. Sending a prompt fires a single POST /api/ab/stream that streams
 *     interleaved events tagged pane="a" or pane="b".  Each pane's
 *     contentDeltas accumulate independently in the UI.
 *  3. After both panes terminate, clicking pane A's "Use this response"
 *     button calls POST /api/chats/1/messages with pane A's content,
 *     toggles A/B compare off via PATCH /api/chats/1, and re-renders the
 *     single-stream message list with the appended assistant turn
 *     visible (Gap 3, P10c.2).
 */
import { test, expect } from "@playwright/test";
import { bootstrapAuthedApp } from "./_bootstrap";

test.describe("Flow 17 — A/B compare two-pane stream + persist pane A", () => {
  test("two panes stream → pick pane A → message appended + AB disabled", async ({ page }) => {
    // Authed chat-page bootstrap defaults; this spec is already signed in
    // on cold load.
    await bootstrapAuthedApp(page);

    // Mutable test state: A/B compare flag + appended messages.  These
    // mutate when the SUT issues PATCH /api/chats/1 (ab_compare_enabled)
    // and POST /api/chats/1/messages (assistant-turn persistence).
    let abEnabled = true;
    interface PersistedMessage {
      id: number; chat_id: number; role: "user" | "assistant" | "system" | "tool";
      content: string; reasoning_content: string | null; created_at: string;
    }
    const persistedMessages: PersistedMessage[] = [];

    // Chats list — chat 1 has A/B compare enabled.
    await page.route("**/api/chats*", (route) => {
      if (route.request().method() === "GET") {
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify([
            {
              id: 1, title: "AB Chat", folder: null, pinned: false,
              updated_at: new Date().toISOString(), model_id: "qwen",
              display_order: 0,
              settings: {
                ab_compare: {
                  enabled: abEnabled,
                  model_a: "qwen",
                  model_b: "llama",
                },
              },
            },
          ]),
        });
      }
      return route.continue();
    });

    // POST /api/chats/1/messages — append handler (Gap 3).
    await page.route("**/api/chats/1/messages", async (route) => {
      const body = route.request().postData() ?? "";
      const params = new URLSearchParams(body);
      const role = params.get("role") ?? "assistant";
      const content = params.get("content") ?? "";
      const nextId = persistedMessages.length + 1;
      const msg: PersistedMessage = {
        id: nextId,
        chat_id: 1,
        role: role as PersistedMessage["role"],
        content,
        reasoning_content: null,
        created_at: new Date().toISOString(),
      };
      persistedMessages.push(msg);
      return route.fulfill({
        status: 201,
        contentType: "application/json",
        body: JSON.stringify(msg),
      });
    });

    await page.route("**/api/chats/1", async (route) => {
      const method = route.request().method();
      if (method === "PATCH") {
        const body = route.request().postData() ?? "";
        const params = new URLSearchParams(body);
        const next = params.get("ab_compare_enabled");
        if (next !== null) abEnabled = next === "true";
        return route.fulfill({
          status: 200,
          contentType: "application/json",
          body: JSON.stringify({
            id: 1, title: "AB Chat", folder: null, pinned: false,
            updated_at: new Date().toISOString(), model_id: "qwen",
            display_order: 0,
            settings: {
              ab_compare: { enabled: abEnabled, model_a: "qwen", model_b: "llama" },
            },
          }),
        });
      }
      return route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          id: 1, user_id: 1, title: "AB Chat", folder: null, pinned: false,
          created_at: new Date().toISOString(),
          updated_at: new Date().toISOString(),
          messages: persistedMessages, has_more: false,
          settings: {
            ab_compare: { enabled: abEnabled, model_a: "qwen", model_b: "llama" },
          },
        }),
      });
    });

    // /api/ab/stream — emit interleaved pane-A + pane-B events terminating
    // in ab.end for each pane.
    await page.route("**/api/ab/stream", (route) => {
      const frames: string[] = [];
      frames.push(
        `event: chat.start\ndata: ${JSON.stringify({
          type: "chat.start", pane: "a", response_id: "respA",
        })}\n\n`
      );
      frames.push(
        `event: chat.start\ndata: ${JSON.stringify({
          type: "chat.start", pane: "b", response_id: "respB",
        })}\n\n`
      );
      frames.push(
        `event: message.delta\ndata: ${JSON.stringify({
          type: "message.delta", pane: "a", delta: "ALPHA reply",
        })}\n\n`
      );
      frames.push(
        `event: message.delta\ndata: ${JSON.stringify({
          type: "message.delta", pane: "b", delta: "BRAVO reply",
        })}\n\n`
      );
      frames.push(
        `event: ab.end\ndata: ${JSON.stringify({
          type: "ab.end", pane: "a", response_id: "respA",
        })}\n\n`
      );
      frames.push(
        `event: ab.end\ndata: ${JSON.stringify({
          type: "ab.end", pane: "b", response_id: "respB",
        })}\n\n`
      );
      return route.fulfill({
        status: 200,
        contentType: "text/event-stream",
        body: frames.join(""),
      });
    });

    await page.goto("/chats/1");

    // The two-pane view should be visible.
    await expect(page.getByLabel("A/B model comparison")).toBeVisible({
      timeout: 5_000,
    });

    // Send a prompt — triggers POST /api/ab/stream.
    const composer = page.getByRole("textbox", { name: "Message" });
    await composer.fill("Which is better?");
    await composer.press("Control+Enter");

    // Both panes should stream and complete.
    await expect(page.getByText("ALPHA reply")).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText("BRAVO reply")).toBeVisible({ timeout: 10_000 });

    // After both panes complete, each pane renders a "Use <label> response"
    // button.  Click pane A's.
    const useAbtn = page.getByRole("button", { name: /Use qwen response/ });
    await expect(useAbtn).toBeVisible({ timeout: 5_000 });
    await useAbtn.click();

    // Chat.tsx onSelectA → useAppendMessage → POST /api/chats/1/messages,
    // then PATCH /api/chats/1 ab_compare_enabled=false.  The success toast
    // confirms persistence; the chat reverts to single-stream mode showing
    // pane A's content as an assistant message in history.  Success toasts
    // render with role="status" (polite announcement), not role="alert".
    await expect(page.getByRole("status").filter({ hasText: /Model A/ })).toBeVisible({
      timeout: 5_000,
    });

    // Wait for AB compare to flip off — the two-pane view should disappear.
    await expect(page.getByLabel("A/B model comparison")).not.toBeVisible({
      timeout: 5_000,
    });

    // Pane A's content ("ALPHA reply") is now a persisted assistant turn in
    // the chat's message history.
    await expect(page.getByText("ALPHA reply")).toBeVisible({ timeout: 5_000 });
  });
});
