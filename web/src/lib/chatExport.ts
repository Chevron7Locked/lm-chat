/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Chat export serializers.
 *
 * Pure-frontend transforms from the in-memory message cache to:
 *   - human-readable Markdown (`exportChatAsMarkdown`)
 *   - machine-readable JSON  (`exportChatAsJson`)
 *
 * The Markdown output preserves:
 *   - role headers (`## You`, `## Reply`, `## System`, `## Tool`) —
 *     re-labelled away from "Assistant" (wire role names in the JSON
 *     export branch stay the OpenAI-spec `"assistant"`).
 *   - ISO-8601 timestamps when present
 *   - existing fenced code blocks (model code stays in its fence)
 *   - any `[doc:NN]` citation references collected as numbered footnotes
 *     at the end of the document
 *
 * The JSON output is the FULL message record array (reasoning_content
 * preserved); it round-trips back through the API shape, enabling
 * lossless git-store / archive / diff of conversations.
 *
 * `triggerDownload` is a thin wrapper around the standard `Blob + <a>`
 * pattern, factored out so the call sites in `Chat.tsx` stay tiny and
 * vitest can spy the download trigger without hitting the DOM.
 *
 * No backend round-trip — these helpers run synchronously against
 * already-cached data.
 */

/**
 * One message as the chat-export serializers consume it.  Subset of the
 * full `Message` shape from `types/api.ts` (id / chat_id / role / content
 * / reasoning_content / created_at), kept narrow so consumers can pass
 * either the on-the-wire record or the slimmed `PublicMessage` shape.
 */
export interface ExportMessage {
  id: number;
  role: string;
  content: string;
  /** Optional reasoning trace (preserved in JSON; omitted from Markdown). */
  reasoning_content?: string | null;
  /** ISO-8601 timestamp.  Optional; when present it lands in the MD header. */
  created_at?: string | null;
}

/** Metadata that wraps the exported messages. */
export interface ExportChat {
  id: number;
  title: string;
  /** ISO-8601 timestamp of chat creation; used in the MD preamble. */
  created_at?: string | null;
}

// User-visible label for the AI side: "Reply" (not "Assistant").
// The wire key stays "assistant" because OpenAI / LM Studio role
// tokens are exempt; only the rendered Markdown heading changes.
const _ROLE_LABELS: Record<string, string> = {
  user: "You",
  assistant: "Reply",
  system: "System",
  tool: "Tool",
};

function _roleLabel(role: string): string {
  return _ROLE_LABELS[role] ?? role.charAt(0).toUpperCase() + role.slice(1);
}

/**
 * Find every `[doc:NN]` (or `[doc:abc]`) reference in *content* and
 * return them in first-occurrence order with no duplicates.  Used to
 * build the citation footnote section of the Markdown export.
 */
export function extractCitations(content: string): string[] {
  const re = /\[doc:([^\]]+)\]/g;
  const seen = new Set<string>();
  const order: string[] = [];
  let m: RegExpExecArray | null;
  while ((m = re.exec(content)) !== null) {
    const id = m[1] ?? "";
    if (id === "" || seen.has(id)) continue;
    seen.add(id);
    order.push(id);
  }
  return order;
}

/**
 * Serialize a chat (+ its messages) as Markdown.
 *
 * Layout:
 *   # <chat title>
 *   _Exported YYYY-MM-DD from LM Chat_
 *
 *   ## You — <timestamp>
 *   <content>
 *
 *   ## Reply — <timestamp>
 *   <content>
 *
 *   ## Citations
 *   1. `[doc:abc]`
 *   2. `[doc:def]`
 *
 * Content blocks are emitted verbatim — fenced code blocks the model
 * produced stay fenced.  We do NOT double-escape fenced regions; the
 * reply's content already contains the backticks.
 */
export function exportChatAsMarkdown(
  chat: ExportChat,
  messages: readonly ExportMessage[],
): string {
  const lines: string[] = [];
  lines.push(`# ${chat.title || `Chat ${String(chat.id)}`}`);
  lines.push("");
  lines.push(`_Exported ${new Date().toISOString()} from LM Chat_`);
  lines.push("");
  if (chat.created_at != null && chat.created_at !== "") {
    lines.push(`_Created ${chat.created_at}_`);
    lines.push("");
  }

  // Collect citations across every message in first-occurrence order.
  const allCitations: string[] = [];
  const seenCitations = new Set<string>();

  for (const msg of messages) {
    const label = _roleLabel(msg.role);
    const ts =
      msg.created_at != null && msg.created_at !== ""
        ? ` — ${msg.created_at}`
        : "";
    lines.push(`## ${label}${ts}`);
    lines.push("");
    lines.push(msg.content || "");
    lines.push("");
    for (const id of extractCitations(msg.content || "")) {
      if (!seenCitations.has(id)) {
        seenCitations.add(id);
        allCitations.push(id);
      }
    }
  }

  if (allCitations.length > 0) {
    lines.push("## Citations");
    lines.push("");
    allCitations.forEach((id, idx) => {
      lines.push(`${String(idx + 1)}. \`[doc:${id}]\``);
    });
    lines.push("");
  }

  return lines.join("\n");
}

/**
 * Serialize a chat (+ its messages) as a self-contained JSON document.
 *
 * Schema:
 *   {
 *     "version": 1,
 *     "exported_at": "2026-05-22T...",
 *     "chat": { id, title, created_at },
 *     "messages": [ { id, role, content, reasoning_content, created_at }, ... ]
 *   }
 *
 * `reasoning_content` is preserved (one of the explicit export scope items).
 * Output is pretty-printed (2-space indent) so the resulting file is
 * git-diffable.
 */
export function exportChatAsJson(
  chat: ExportChat,
  messages: readonly ExportMessage[],
): string {
  const doc = {
    version: 1,
    exported_at: new Date().toISOString(),
    chat: {
      id: chat.id,
      title: chat.title,
      created_at: chat.created_at ?? null,
    },
    messages: messages.map((m) => ({
      id: m.id,
      role: m.role,
      content: m.content,
      reasoning_content: m.reasoning_content ?? null,
      created_at: m.created_at ?? null,
    })),
  };
  return JSON.stringify(doc, null, 2);
}

/**
 * Build a download filename for a chat export.
 *
 * Replaces filesystem-unfriendly characters in the title with hyphens
 * and appends the chat id + extension.  Pure helper so vitest can pin
 * the naming contract.
 */
export function buildExportFilename(
  chat: ExportChat,
  ext: "md" | "json",
): string {
  const safeTitle =
    (chat.title || "chat")
      .replace(/[^\w\s.-]/g, "")
      .trim()
      .replace(/\s+/g, "-")
      .toLowerCase()
      .slice(0, 64) || "chat";
  return `${safeTitle}-${String(chat.id)}.${ext}`;
}

/**
 * Trigger a browser download of *content* as *filename*.
 *
 * Uses the standard Blob + anchor pattern.  In environments where the
 * DOM is not available (vitest jsdom WITHOUT explicit window mocking)
 * this is a no-op so unit tests that only verify the serializer don't
 * have to stub URL.createObjectURL.
 *
 * Returns true when a download was triggered, false when the DOM was
 * unavailable.
 */
function triggerDownload(
  filename: string,
  content: string,
  mimeType: string,
): boolean {
  if (typeof document === "undefined" || typeof URL === "undefined")
    return false;
  if (typeof URL.createObjectURL !== "function") return false;
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  // Some browsers (notably older Safari) require the anchor to be in
  // the DOM before .click() fires reliably.
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  // setTimeout to revoke so the click handler has a chance to read the
  // blob URL before it's released.
  setTimeout(() => {
    URL.revokeObjectURL(url);
  }, 0);
  return true;
}

/**
 * Convenience: serialize + trigger a Markdown download in one call.
 */
export function downloadChatAsMarkdown(
  chat: ExportChat,
  messages: readonly ExportMessage[],
): boolean {
  return triggerDownload(
    buildExportFilename(chat, "md"),
    exportChatAsMarkdown(chat, messages),
    "text/markdown;charset=utf-8",
  );
}

/**
 * Convenience: serialize + trigger a JSON download in one call.
 */
export function downloadChatAsJson(
  chat: ExportChat,
  messages: readonly ExportMessage[],
): boolean {
  return triggerDownload(
    buildExportFilename(chat, "json"),
    exportChatAsJson(chat, messages),
    "application/json;charset=utf-8",
  );
}
