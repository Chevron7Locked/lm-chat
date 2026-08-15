/* SPDX-License-Identifier: Apache-2.0 */
/**
 * J11 — PHASE 0: characterize stream-disconnect data loss (live-stream
 * reconnect work).
 *
 * This journey does NOT fix anything. It is a truthful measurement harness
 * for what the app does TODAY when a streaming client disconnects mid-answer,
 * so it can become the regression test for the eventual reconnect fix.
 * Assertions cover only what code-reading + this run found to be STABLE;
 * everything else is logged unambiguously (`J11 DIAG:` / `J11 RESULT:`) for
 * the operator to read out of the run, mirroring J7's diagnostic style.
 *
 * Two disconnect mechanisms are exercised, in two separate chats, because
 * they are different real-world cases:
 *   - scenario "offline": `context.setOffline(true)` — a network drop /
 *     backgrounded client. The tab stays loaded but unreachable for a few
 *     seconds before coming back and reloading.
 *   - scenario "reload":  `page.reload()` with NO prior setOffline — a user
 *     just hits refresh mid-answer. The in-flight fetch is aborted by the
 *     navigation itself.
 *
 * For each scenario we measure, via the API (not the DOM) wherever possible:
 *   1. the assistant row's resting `state`
 *   2. its `content` length (was the partial preserved, and how much)
 *   3. whether the row is returned by GET /api/chats/{id} at all
 *   4. whether content length GROWS across a few samples after the
 *      disconnect — the single most important measurement: growth means
 *      generation survived the client going away; a frozen length means it
 *      died with the request.
 *   5. whether the FE renders the partial after reload.
 *   6. `reasoning_content` length/nullness and `tool_calls` presence/count —
 *      confirmed present on the SAME GET /api/chats/{id} response already
 *      used for state/content (message_service.py's `Message` model carries
 *      both; `ChatWithMessagesResponse.messages` returns them as-is, no
 *      camelCase/field-stripping transform). This is the field that actually
 *      matters for the 2026-08-14 disconnect-salvage fix (streaming_service.py
 *      `_salvage_aborted_row`): `_CoalesceTimer.flush()` NEVER persists
 *      reasoning_content or tool_calls — only the salvage backfill does — so
 *      this is the one measurement in this file that can show whether that
 *      fix is actually reaching production, not just passing in the unit
 *      suite. content length alone cannot show this.
 *   7. a PRE-CUT reasoning baseline, so a post-disconnect reading of 0 is a
 *      comparison ("nothing arrived before the cut either" vs "reasoning
 *      streamed in but wasn't persisted"), not a bare number with no
 *      reference point. See "Pre-cut reasoning baseline" below for how.
 *
 * DOM reasoning-block logging — SKIPPED, not attempted for the POST-cut
 * value. ProcessStream.tsx renders reasoning through two DIFFERENT DOM
 * shapes depending on phase: `[data-testid="process-reasoning-live"]` while
 * streaming BEFORE the answer starts, vs. a togglable collapsed body
 * (className-only, no testid, shares `.lmchat-process-reasoning__text` with
 * the live variant) once the answer is flowing — which is already the case
 * by the time this journey's own "streaming confirmed started" checkpoint
 * fires (it waits for the content caret, which only appears once
 * `message.content !== ""`, i.e. AFTER ProcessStream's own state machine has
 * already auto-collapsed reasoning per its docstring's phase (c)). Reading
 * the collapsed body's text without a dedicated testid means either coupling
 * to a CSS class name or subtracting the toggle button's own "Reasoning"
 * label text out of a container read — both couple this journey to FE
 * implementation detail it doesn't otherwise touch, for a measurement the
 * API-sourced reasoning_content field above already covers authoritatively
 * for the POST-cut side. Not logged; API is the source of truth there.
 *
 * Pre-cut reasoning baseline — HOW, and why not the two options considered
 * first:
 *   - Sampling `reasoning_content` from GET /api/chats/{id} WHILE still
 *     streaming: CONFIRMED IMPOSSIBLE, not assumed. The row is `state='draft'`
 *     for the entire duration it's still streaming, and
 *     message_service.py's `list_for_chat` (the query this endpoint's
 *     `ChatWithMessagesResponse` is built from) excludes `draft` rows
 *     entirely (`WHERE state != 'draft'`) — confirmed by reading that query;
 *     there is no other GET-by-id endpoint that bypasses it
 *     (`routes/messages.py` only exposes PATCH/DELETE by id). A draft row is
 *     simply not visible through this API at all.
 *   - The DOM (`[data-testid="process-reasoning-live"]`): a real, stable
 *     testid DOES exist for the LIVE (pre-answer) reasoning block, unlike
 *     the collapsed one above — but by construction this journey's
 *     "streaming started" checkpoint only fires once content begins (the
 *     stream caret needs `message.content !== ""`), which is exactly when
 *     ProcessStream auto-collapses reasoning out of the live testid's reach.
 *     Sampling it EARLIER (before waiting for content) would work for models
 *     that reason before answering, but ties the measurement to a second,
 *     narrower timing window this journey doesn't otherwise need — skipped
 *     in favor of the more direct option below.
 *   - What's actually used: `installReasoningTee()` (below) wraps
 *     `window.fetch` (via `page.addInitScript`, so it's in place before any
 *     page script runs, across every navigation including reload) to
 *     intercept ONLY the `/api/chat/stream` POST, `ReadableStream.tee()` its
 *     body, hand the app one branch completely unmodified (byte-for-byte —
 *     the app's own consumption is unaffected), and independently parse SSE
 *     frames off the other branch for `reasoning.delta` events, matching the
 *     wire contract `routes/streaming.py`'s own docstring documents
 *     (`event: <type>\ndata: {"type":...,"content":...}`) and
 *     `_format_sse_frame`'s actual serialization. This observes the literal
 *     bytes the page received — not a DOM read, not a proxy, not an
 *     assumption about FE state — so a pre-cut baseline of "0 chars, never
 *     saw a reasoning.delta frame" is real evidence the model emitted none,
 *     and "saw N chars" against a post-cut null is real evidence of loss.
 *
 * Code-reading finding worth flagging up front (see the report, not just
 * this file): the resting state is a RACE between two independent writers —
 * the disconnect watcher's `safe_abort_draft` (streaming_service.py, moves
 * draft → aborted_by_client, writes no content) and stream_chat's outer
 * `finally` salvage-release (`_release_stuck_draft`, moves draft → final IF
 * it still finds the row in 'draft', and in that case writes the FULL
 * in-memory accumulated content, not just what `_CoalesceTimer` last
 * flushed). Whichever transitions the row out of 'draft' first wins — so
 * today's resting state is NOT deterministically one or the other. This
 * journey therefore asserts only `state !== 'draft'`, never a specific
 * resting state, and logs which one actually happened.
 */
import type { Page } from "@playwright/test";
import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  loginAndWait,
  createChatViaRequest,
} from "../flows/_flow-helpers";
import { classifyFleet, configureLmStudio } from "./_dogfood-helpers";

// Model-gated waits (first-token latency on a cold/loaded local model) get the
// same generous budget the rest of this dogfood suite uses — a slow local
// model is expected, never a test failure.
const STREAM_START_TIMEOUT_MS = 1_800_000;

// NOT model-gated — once tokens are flowing, this just needs enough real
// wall-clock time for a few 250ms `_CoalesceTimer` flushes to land before we
// cut the connection, so the "partial content survived" measurement is
// meaningful rather than a coin flip on the very first delta.
const BUFFER_AFTER_STREAM_START_MS = 3_000;

const GROWTH_SAMPLE_COUNT = 4;
const GROWTH_SAMPLE_INTERVAL_MS = 3_000;

// A prompt engineered to keep a local model generating for a while without
// needing any tool calls — long enough that a mid-stream cut lands on a
// genuinely partial answer regardless of model speed.
const LONG_PROMPT =
  "List and describe, in detail, all 20 standard amino acids used in human " +
  "proteins — one full paragraph per amino acid, covering its side-chain " +
  "structure and its biological role. Do not use any tools; just answer " +
  "from what you know, and don't stop until you've covered all 20.";

interface AssistantSnapshot {
  id: number;
  state: string;
  contentLen: number;
  /** true iff `reasoning_content` was JSON null/undefined (not merely empty). */
  reasoningIsNull: boolean;
  /** 0 when reasoningIsNull; otherwise the string length. */
  reasoningLen: number;
  /** true iff `tool_calls` was JSON null/undefined (not merely an empty array). */
  toolCallsIsNull: boolean;
  /** 0 when toolCallsIsNull; otherwise the array length. */
  toolCallsCount: number;
}

interface GrowthSample {
  tMs: number;
  visible: boolean;
  state: string | null;
  contentLen: number;
  reasoningIsNull: boolean | null;
  reasoningLen: number;
  toolCallsIsNull: boolean | null;
  toolCallsCount: number;
}

/** GET /api/chats/{id} and return the LAST assistant row, or null if none. */
async function fetchLatestAssistant(
  page: Page,
  backendURL: string,
  chatId: number,
): Promise<AssistantSnapshot | null> {
  const resp = await page.request.get(
    `${backendURL}/api/chats/${String(chatId)}`,
    { timeout: 15_000 },
  );
  if (!resp.ok()) return null;
  const detail = (await resp.json()) as {
    messages: Array<{
      id: number;
      role: string;
      state: string;
      content: string;
      // Confirmed present on this endpoint's response — message_service.py's
      // Message model carries both, unstripped. null/undefined are BOTH
      // treated as "not present" below (the field is optional server-side
      // pre-migration and pydantic's None serializes to JSON null either way).
      reasoning_content?: string | null;
      tool_calls?: Array<Record<string, unknown>> | null;
    }>;
  };
  const last = detail.messages.filter((m) => m.role === "assistant").at(-1);
  if (last === undefined) return null;
  const reasoningIsNull = last.reasoning_content === null || last.reasoning_content === undefined;
  const toolCallsIsNull = last.tool_calls === null || last.tool_calls === undefined;
  return {
    id: last.id,
    state: last.state,
    contentLen: last.content.length,
    reasoningIsNull,
    reasoningLen: reasoningIsNull ? 0 : last.reasoning_content!.length,
    toolCallsIsNull,
    toolCallsCount: toolCallsIsNull ? 0 : last.tool_calls!.length,
  };
}

/**
 * Poll the assistant row `count` times, `intervalMs` apart, logging each
 * sample. While the row is still `draft`, `list_for_chat` filters it out
 * entirely (message_service.py), so `visible=false` there is expected and
 * NOT itself evidence of data loss — only samples where `visible=true` are
 * comparable for growth.
 */
async function sampleGrowth(
  page: Page,
  backendURL: string,
  chatId: number,
  label: string,
  count: number,
  intervalMs: number,
): Promise<GrowthSample[]> {
  const samples: GrowthSample[] = [];
  const t0 = Date.now();
  for (let i = 0; i < count; i++) {
    const snap = await fetchLatestAssistant(page, backendURL, chatId).catch(
      () => null,
    );
    const sample: GrowthSample = {
      tMs: Date.now() - t0,
      visible: snap !== null,
      state: snap?.state ?? null,
      contentLen: snap?.contentLen ?? 0,
      reasoningIsNull: snap?.reasoningIsNull ?? null,
      reasoningLen: snap?.reasoningLen ?? 0,
      toolCallsIsNull: snap?.toolCallsIsNull ?? null,
      toolCallsCount: snap?.toolCallsCount ?? 0,
    };
    samples.push(sample);
    console.log(
      `J11 DIAG [${label}] t=${String(sample.tMs)}ms visible=${String(sample.visible)} ` +
        `state=${sample.state ?? "n/a"} contentLen=${String(sample.contentLen)} ` +
        `reasoningLen=${String(sample.reasoningLen)} ` +
        `(null=${String(sample.reasoningIsNull ?? "n/a")}) ` +
        `toolCalls=${String(sample.toolCallsCount)} ` +
        `(null=${String(sample.toolCallsIsNull ?? "n/a")})`,
    );
    if (i < count - 1) {
      await new Promise((r) => setTimeout(r, intervalMs));
    }
  }
  return samples;
}

/** Compare the first and last VISIBLE samples and log an unambiguous verdict. */
function logGrowthVerdict(samples: GrowthSample[], label: string): void {
  const visible = samples.filter((s) => s.visible);
  if (visible.length < 2) {
    console.log(
      `J11 RESULT [${label}]: fewer than 2 samples saw the row out of 'draft' ` +
        `in this window — cannot determine growth vs freeze from this sample set.`,
    );
    return;
  }
  const first = visible[0]!;
  const last = visible[visible.length - 1]!;
  const delta = last.contentLen - first.contentLen;
  const verdict =
    delta > 0
      ? "GENERATION CONTINUED IN THE BACKGROUND"
      : "GENERATION FROZE WITH THE REQUEST (no growth observed)";
  console.log(
    `J11 RESULT [${label}]: content ${String(first.contentLen)} → ${String(last.contentLen)} chars ` +
      `over ${String(last.tMs - first.tMs)}ms (state='${last.state ?? "?"}') → ${verdict} | ` +
      `reasoningLen=${String(last.reasoningLen)} (null=${String(last.reasoningIsNull ?? "n/a")}) ` +
      `toolCalls=${String(last.toolCallsCount)} (null=${String(last.toolCallsIsNull ?? "n/a")})`,
  );
}

interface ReasoningTeeSnapshot {
  /** Ever saw a non-empty reasoning.delta content field on this fetch. */
  sawAny: boolean;
  /** Sum of all reasoning.delta content lengths observed so far. */
  chars: number;
}

/**
 * Install the SSE-tee described in the file docstring's "Pre-cut reasoning
 * baseline" section. MUST be called before the first `page.goto` of a
 * scenario whose pre-cut reasoning this journey wants to measure —
 * `page.addInitScript` re-injects on every navigation for the rest of this
 * page's lifetime (reload included), so one call up front covers both
 * scenarios and both disconnect mechanisms.
 */
async function installReasoningTee(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const w = window as typeof window & {
      __j11Reasoning?: { sawAny: boolean; chars: number };
    };
    const capture = { sawAny: false, chars: 0 };
    w.__j11Reasoning = capture;
    const originalFetch = window.fetch.bind(window);
    window.fetch = (async (
      input: RequestInfo | URL,
      init?: RequestInit,
    ): Promise<Response> => {
      const resp = await originalFetch(input, init);
      const url = input instanceof Request ? input.url : String(input);
      if (!url.includes("/api/chat/stream") || resp.body === null) {
        return resp;
      }
      // Tee: the app gets one branch, byte-for-byte identical to an
      // untouched fetch — this observer never alters what useSSE.ts
      // consumes. The other branch is read independently, below.
      const [appStream, obsStream] = resp.body.tee();
      void (async () => {
        const reader = obsStream.getReader();
        const decoder = new TextDecoder();
        let buf = "";
        try {
          for (;;) {
            const { done, value } = await reader.read();
            if (done) break;
            // {stream:true}: don't corrupt a multi-byte UTF-8 char split
            // across two chunks.
            buf += decoder.decode(value, { stream: true });
            // SSE frames are "\n\n"-terminated (routes/streaming.py /
            // _format_sse_frame) — an embedded newline in a data field is
            // JSON-escaped ("\\n"), never a literal byte, so this split is
            // exact, matching the app's own readSseStream.ts framing.
            const frames = buf.split("\n\n");
            buf = frames.pop() ?? "";
            for (const frame of frames) {
              if (!frame.includes("reasoning.delta")) continue;
              const m = /data: (.*)/.exec(frame);
              if (m === null || m[1] === undefined) continue;
              try {
                const obj = JSON.parse(m[1]) as { content?: unknown };
                if (typeof obj.content === "string" && obj.content !== "") {
                  capture.sawAny = true;
                  capture.chars += obj.content.length;
                }
              } catch {
                /* malformed/partial frame — skip, best-effort observer only */
              }
            }
          }
        } catch {
          /* observer stream errored (e.g. the context went offline) —
             best-effort only; never throw into the app's own fetch path. */
        }
      })();
      return new Response(appStream, {
        status: resp.status,
        statusText: resp.statusText,
        headers: resp.headers,
      });
    }) as typeof window.fetch;
  });
}

/** Read the current pre-cut reasoning snapshot captured by installReasoningTee. */
async function readReasoningTeeSnapshot(
  page: Page,
): Promise<ReasoningTeeSnapshot> {
  return page.evaluate(() => {
    const w = window as typeof window & {
      __j11Reasoning?: { sawAny: boolean; chars: number };
    };
    return w.__j11Reasoning ?? { sawAny: false, chars: 0 };
  });
}

/**
 * Compare the pre-cut SSE-observed reasoning baseline against the post-
 * disconnect persisted value and log an unambiguous verdict — a bare
 * post-cut number has no reference point; this makes it a comparison.
 */
function logReasoningComparison(
  label: string,
  preCut: ReasoningTeeSnapshot,
  postCutLen: number,
  postCutIsNull: boolean,
): void {
  let verdict: string;
  if (!preCut.sawAny) {
    verdict =
      "no reasoning.delta observed before the cut — the model most likely " +
      "emitted none this turn (NOT evidence of a salvage failure)";
  } else if (postCutIsNull || postCutLen === 0) {
    verdict =
      "REASONING WAS OBSERVED PRE-CUT BUT IS ABSENT POST-DISCONNECT — " +
      "possible salvage failure, investigate";
  } else if (postCutLen < preCut.chars) {
    verdict =
      "reasoning observed pre-cut AND persisted post-disconnect, but SHORTER " +
      "than what was observed streaming in — check for partial loss";
  } else {
    verdict =
      "reasoning observed pre-cut and persisted post-disconnect — consistent";
  }
  console.log(
    `J11 RESULT [${label}]: reasoning pre-cut(SSE-observed)=${String(preCut.chars)} chars ` +
      `(sawAny=${String(preCut.sawAny)}) → post-disconnect(persisted)=${String(postCutLen)} ` +
      `chars (null=${String(postCutIsNull)}) → ${verdict}`,
  );
}

async function waitForModelSelector(page: Page): Promise<void> {
  await page.waitForFunction(
    () => {
      const sel = document.querySelector<HTMLSelectElement>(
        '[data-testid="chat-header-model-select"]',
      );
      return sel !== null && sel.value !== "" && sel.options.length > 1;
    },
    null,
    { timeout: 30_000 },
  );
}

/** Fill the composer, submit, and wait for a content-bearing delta to render. */
async function sendPromptUntilStreaming(
  page: Page,
  prompt: string,
): Promise<void> {
  const composer = page.getByPlaceholder(/Message/);
  await composer.waitFor({ state: "visible", timeout: 15_000 });
  await composer.fill(prompt);
  await page.keyboard.press("Enter");
  // The caret only renders once message.content !== "" AND streaming===true
  // (ChatMessage.tsx) — this IS "at least one content delta rendered".
  await expect(
    page.getByTestId("chat-message-stream-caret"),
  ).toBeVisible({ timeout: STREAM_START_TIMEOUT_MS });
  await page.waitForTimeout(BUFFER_AFTER_STREAM_START_MS);
}

test(
  "j11: a mid-stream client disconnect — state, content survival, and " +
    "whether generation continues in the background (PHASE 0, no fix yet)",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    test.setTimeout(5_400_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    const fleet = await classifyFleet(page, backendURL);
    await configureLmStudio(page, backendURL, fleet.fastId);
    // Installed ONCE — persists across every navigation on this page for the
    // rest of the test, both scenarios' reloads included.
    await installReasoningTee(page);

    // -----------------------------------------------------------------
    // Scenario "offline" — context.setOffline(true): network drop while
    // the tab stays loaded, then reconnect + reload.
    // -----------------------------------------------------------------
    const chatIdOffline = await createChatViaRequest(
      page,
      backendURL,
      "J11 Offline Disconnect",
    );
    await page.goto(`${backendURL}/chats/${String(chatIdOffline)}`);
    await waitForModelSelector(page);
    await sendPromptUntilStreaming(page, LONG_PROMPT);

    const domLenBeforeOffline = (
      await page
        .locator('[data-message-role="assistant"]')
        .last()
        .innerText()
        .catch(() => "")
    ).length;
    console.log(
      `J11 DIAG [offline]: streaming confirmed started; approx DOM content ` +
        `chars before cut=${String(domLenBeforeOffline)}`,
    );

    // Snapshot BEFORE the cut — setOffline(true) doesn't destroy the page
    // context, but the reasoning tee stops accumulating the instant the
    // fetch errors, so this is the last point where "pre-cut" is unambiguous.
    const preCutReasoningOffline = await readReasoningTeeSnapshot(page);
    console.log(
      `J11 DIAG [offline]: pre-cut reasoning (SSE-observed)=` +
        `${String(preCutReasoningOffline.chars)} chars (sawAny=` +
        `${String(preCutReasoningOffline.sawAny)})`,
    );

    await page.context().setOffline(true);
    console.log("J11 DIAG [offline]: context.setOffline(true) issued");
    // Give the browser time to actually fail/close the in-flight fetch and
    // the server's disconnect watcher (500ms poll tick) a chance to observe it.
    await page.waitForTimeout(3_000);

    // Flip back online BEFORE sampling: this does not resurrect the aborted
    // fetch (browsers don't resume dead sockets on setOffline(false)), so any
    // growth observed from here on is genuinely server-side generation that
    // happened with NO client attached — the exact measurement requested —
    // without relying on any assumption about whether page.request bypasses
    // setOffline(true) (it likely does, since it's a separate Node-side HTTP
    // client, not routed through Chromium's network stack, but this design
    // doesn't need that assumption to hold).
    await page.context().setOffline(false);
    console.log("J11 DIAG [offline]: context.setOffline(false) — sampling begins");

    const preReloadSamples = await sampleGrowth(
      page,
      backendURL,
      chatIdOffline,
      "offline/pre-reload",
      GROWTH_SAMPLE_COUNT,
      GROWTH_SAMPLE_INTERVAL_MS,
    );
    logGrowthVerdict(preReloadSamples, "offline/pre-reload");

    await page.reload();
    await waitForModelSelector(page);
    await page.waitForTimeout(1_500); // let any restore-on-load settle

    const postReloadOffline = await fetchLatestAssistant(
      page,
      backendURL,
      chatIdOffline,
    );
    expect(
      postReloadOffline,
      "assistant row for the disconnected turn was not returned by " +
        "GET /api/chats/{id} at all after reload — total data loss",
    ).not.toBeNull();
    // Only a stable, code-verified invariant: SOMETHING moves the row out of
    // 'draft' (either the disconnect watcher's abort or the outer finally's
    // salvage-release — see the file docstring on the race between them).
    // A row stuck in 'draft' forever would also 409 the chat's next stream
    // attempt, which the reaper's 5-minute sweep exists specifically to
    // prevent — so this should hold well before that safety net even fires.
    expect(
      postReloadOffline!.state,
      "row must not still be 'draft' after the disconnect settles + a reload",
    ).not.toBe("draft");
    expect(
      postReloadOffline!.contentLen,
      "partial content was NOT preserved across the disconnect (several " +
        "seconds of real streaming happened before the cut)",
    ).toBeGreaterThan(0);

    const caretGoneOffline = !(await page
      .getByTestId("chat-message-stream-caret")
      .isVisible()
      .catch(() => false));
    const bubbleVisibleOffline = await page
      .locator('[data-message-role="assistant"]')
      .last()
      .isVisible()
      .catch(() => false);
    expect(
      bubbleVisibleOffline,
      "FE did not render the persisted partial content after reload",
    ).toBe(true);

    console.log(
      `J11 RESULT [offline]: resting state='${postReloadOffline!.state}' | ` +
        `content chars=${String(postReloadOffline!.contentLen)} | ` +
        `reasoning chars=${String(postReloadOffline!.reasoningLen)} ` +
        `(null=${String(postReloadOffline!.reasoningIsNull)}) | ` +
        `tool_calls=${String(postReloadOffline!.toolCallsCount)} ` +
        `(null=${String(postReloadOffline!.toolCallsIsNull)}) | ` +
        `stream caret gone=${String(caretGoneOffline)} | ` +
        `assistant bubble rendered=${String(bubbleVisibleOffline)}`,
    );
    logReasoningComparison(
      "offline",
      preCutReasoningOffline,
      postReloadOffline!.reasoningLen,
      postReloadOffline!.reasoningIsNull,
    );

    const postReloadSamplesOffline = await sampleGrowth(
      page,
      backendURL,
      chatIdOffline,
      "offline/post-reload",
      GROWTH_SAMPLE_COUNT,
      GROWTH_SAMPLE_INTERVAL_MS,
    );
    logGrowthVerdict(postReloadSamplesOffline, "offline/post-reload");

    // -----------------------------------------------------------------
    // Scenario "reload" — page.reload() with NO prior setOffline: the
    // in-flight fetch is aborted purely by navigation (a user hitting
    // refresh mid-answer). Separate chat so it can't 409 against the
    // still-settling offline-scenario chat.
    // -----------------------------------------------------------------
    const chatIdReload = await createChatViaRequest(
      page,
      backendURL,
      "J11 Reload Disconnect",
    );
    await page.goto(`${backendURL}/chats/${String(chatIdReload)}`);
    await waitForModelSelector(page);
    await sendPromptUntilStreaming(page, LONG_PROMPT);

    const domLenBeforeReload = (
      await page
        .locator('[data-message-role="assistant"]')
        .last()
        .innerText()
        .catch(() => "")
    ).length;
    console.log(
      `J11 DIAG [reload]: streaming confirmed started; approx DOM content ` +
        `chars before cut=${String(domLenBeforeReload)}`,
    );

    // Snapshot BEFORE the cut — page.reload() destroys the page context (and
    // the tee's accumulated state with it), so this MUST happen first.
    const preCutReasoningReload = await readReasoningTeeSnapshot(page);
    console.log(
      `J11 DIAG [reload]: pre-cut reasoning (SSE-observed)=` +
        `${String(preCutReasoningReload.chars)} chars (sawAny=` +
        `${String(preCutReasoningReload.sawAny)})`,
    );

    await page.reload();
    console.log("J11 DIAG [reload]: page.reload() issued as the sole disconnect trigger");
    await waitForModelSelector(page);
    await page.waitForTimeout(1_500);

    const postReloadReload = await fetchLatestAssistant(
      page,
      backendURL,
      chatIdReload,
    );
    expect(
      postReloadReload,
      "assistant row for the disconnected turn was not returned by " +
        "GET /api/chats/{id} at all after the reload — total data loss",
    ).not.toBeNull();
    expect(
      postReloadReload!.state,
      "row must not still be 'draft' after a bare page.reload() disconnect",
    ).not.toBe("draft");
    expect(
      postReloadReload!.contentLen,
      "partial content was NOT preserved across a bare page.reload() disconnect",
    ).toBeGreaterThan(0);

    const caretGoneReload = !(await page
      .getByTestId("chat-message-stream-caret")
      .isVisible()
      .catch(() => false));
    const bubbleVisibleReload = await page
      .locator('[data-message-role="assistant"]')
      .last()
      .isVisible()
      .catch(() => false);
    expect(
      bubbleVisibleReload,
      "FE did not render the persisted partial content after the reload",
    ).toBe(true);

    console.log(
      `J11 RESULT [reload]: resting state='${postReloadReload!.state}' | ` +
        `content chars=${String(postReloadReload!.contentLen)} | ` +
        `reasoning chars=${String(postReloadReload!.reasoningLen)} ` +
        `(null=${String(postReloadReload!.reasoningIsNull)}) | ` +
        `tool_calls=${String(postReloadReload!.toolCallsCount)} ` +
        `(null=${String(postReloadReload!.toolCallsIsNull)}) | ` +
        `stream caret gone=${String(caretGoneReload)} | ` +
        `assistant bubble rendered=${String(bubbleVisibleReload)}`,
    );
    logReasoningComparison(
      "reload",
      preCutReasoningReload,
      postReloadReload!.reasoningLen,
      postReloadReload!.reasoningIsNull,
    );

    const postReloadSamplesReload = await sampleGrowth(
      page,
      backendURL,
      chatIdReload,
      "reload/post-reload",
      GROWTH_SAMPLE_COUNT,
      GROWTH_SAMPLE_INTERVAL_MS,
    );
    logGrowthVerdict(postReloadSamplesReload, "reload/post-reload");

    // Deliberately NOT calling assertNoConsoleErrors: both disconnect
    // mechanisms are EXPECTED to produce a caught fetch/reader error in
    // useSSE's catch block (an intentional, non-AbortError failure path) —
    // that would be a false failure here, not a regression signal.
    const errors = collectErrors();
    console.log(
      `J11 DIAG: ${String(errors.length)} console error(s)/pageerror(s) collected ` +
        `across both scenarios (not asserted — disconnects are expected to log).`,
    );
  },
);
