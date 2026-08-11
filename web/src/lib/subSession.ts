/* SPDX-License-Identifier: Apache-2.0 */
/**
 * subSession — pure helpers for slash-command sub-sessions.
 *
 * Extracted from `Chat.tsx` so the prompt-build path and the inject-summary
 * fetch are unit-testable without rendering the full chat surface.
 */

/**
 * Build a sub-session system prompt by framing the persona template with
 * date + tool-availability context.
 *
 * Persona templates (`presets.ts`) no longer carry `{{current_date}}` /
 * `{{tools}}` slots — the main-chat path gets that framing from the BE's
 * `[Context]`/`[Capabilities]` blocks, but the sub-session path streams
 * directly via `lm_client.stream()` and never passes through that BE
 * assembly. So here we prepend a `Today is <date>.` line and append a
 * tool-availability block instead of substituting inline tokens.
 *
 * `toolsHint` is keyed off the `integrations` list. If tools ARE wired
 * (MCP chips toggled on the composer + the sub-session route forwards
 * them to LM Studio), the model is told to use them. If no tools, the
 * prompt explicitly tells the model not to narrate searches it can't
 * run. The previous version unconditionally said "no tools available"
 * even when integrations were passed, which made the model refuse to
 * invoke them.
 */
export function buildSubSessionSystemPrompt(
  template: string,
  today: string,
  integrations: string[] = [],
): string {
  let toolsHint: string;
  if (integrations.length > 0) {
    const toolList = integrations.map((id) => `- \`${id}\``).join("\n");
    toolsHint =
      "## TOOLS AVAILABILITY\n\n" +
      "Live tools ARE wired into this session. The following MCP " +
      "integrations are connected and you should USE them when they " +
      "fit the question:\n\n" +
      toolList +
      "\n\nCall the appropriate tool with structured arguments. The " +
      "tool wrapper executes and returns results to you — these are " +
      "real, not narrated. Prefer a live tool result over a guess. " +
      "When a current source would be decisive (versions, prices, " +
      "current events, repository state, document contents), reach " +
      "for the tool first.";
  } else {
    toolsHint =
      "## TOOLS AVAILABILITY\n\n" +
      "No live tools are wired into this sub-session — no web search, " +
      "no file I/O, no code execution. Answer from your training " +
      "knowledge. Do **not** narrate tool calls or pretend to search " +
      "the web; there is no wrapper that will interpret that. When " +
      "evidence is thin, surface the uncertainty explicitly with a " +
      "calibrated confidence band (`high` / `moderate` / `low` / " +
      "`unverified`) rather than presenting speculation as fact. " +
      "Where a current source would be decisive but you can't fetch " +
      "one, name what would resolve the question and stop.";
  }
  const dateLine = `Today is ${today}.`;
  return [dateLine, template.trim(), toolsHint]
    .join("\n\n")
    .replace(/\n{3,}/g, "\n\n");
}

/** Format `new Date()` for the sub-session prompt's leading date line. */
export function formatSubSessionDate(now: Date = new Date()): string {
  return now.toLocaleDateString("en-US", {
    year: "numeric",
    month: "long",
    day: "numeric",
  });
}

/**
 * POST the finalized sub-session summary to the main chat's inject endpoint.
 *
 * Returns `{ ok: true }` only when the server accepted the summary. Any
 * non-2xx response — or a thrown fetch — surfaces as `{ ok: false }` so the
 * caller can keep the sub-session panel open and toast the failure instead
 * of silently dropping the summary.
 */
export async function injectSubSessionSummary(
  chatId: number,
  content: string,
  modelId: string | null,
): Promise<{ ok: boolean }> {
  try {
    const res = await fetch(`/api/chats/${String(chatId)}/inject-message`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content, model_id: modelId }),
    });
    return { ok: res.ok };
  } catch {
    return { ok: false };
  }
}
