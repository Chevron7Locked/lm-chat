/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for P13j chat-export serializers.
 *
 * Pins the contract for:
 *   - `exportChatAsMarkdown` — role headers, timestamps, code fences,
 *     citation footnotes from `[doc:NN]` references.
 *   - `exportChatAsJson` — version envelope + reasoning_content preserved.
 *   - `extractCitations` — first-occurrence order, dedupe.
 *   - `buildExportFilename` — sanitized title + extension.
 */
import { describe, it, expect } from "vitest";
import {
  exportChatAsMarkdown,
  exportChatAsJson,
  extractCitations,
  buildExportFilename,
} from "@/lib/chatExport";

const baseChat = {
  id: 42,
  title: "Test conversation",
  created_at: "2026-05-22T12:00:00Z",
};

const baseMessages = [
  {
    id: 1,
    role: "user",
    content: "Hello, world.",
    created_at: "2026-05-22T12:00:01Z",
    reasoning_content: null,
  },
  {
    id: 2,
    role: "assistant",
    content: "Hi! Here is some code:\n\n```python\nprint('ok')\n```\n\nSee [doc:abc] for more.",
    reasoning_content: "I should explain via code.",
    created_at: "2026-05-22T12:00:02Z",
  },
  {
    id: 3,
    role: "user",
    content: "What about [doc:abc] again and [doc:def]?",
    created_at: "2026-05-22T12:00:03Z",
    reasoning_content: null,
  },
];

describe("extractCitations", () => {
  it("returns citations in first-occurrence order with no duplicates", () => {
    expect(extractCitations("see [doc:a] then [doc:b] and [doc:a]")).toEqual(["a", "b"]);
  });

  it("returns an empty list when no citations are present", () => {
    expect(extractCitations("hello world")).toEqual([]);
  });

  it("ignores empty bracketed citations", () => {
    expect(extractCitations("nothing here [doc:] either")).toEqual([]);
  });
});

describe("exportChatAsMarkdown", () => {
  it("emits role headers, timestamps, and preserved code fences", () => {
    const md = exportChatAsMarkdown(baseChat, baseMessages);
    expect(md).toContain("# Test conversation");
    // Role headers are "You" / "Reply" (not "assistant").
    expect(md).toContain("## You — 2026-05-22T12:00:01Z");
    expect(md).toContain("## Reply — 2026-05-22T12:00:02Z");
    // The banned "Assistant" label MUST NOT appear in user-facing
    // export headers.
    expect(md).not.toContain("## Assistant");
    // Code fence preserved verbatim.
    expect(md).toContain("```python");
    expect(md).toContain("print('ok')");
  });

  it("collects citations across all messages in first-occurrence order", () => {
    const md = exportChatAsMarkdown(baseChat, baseMessages);
    expect(md).toContain("## Citations");
    // abc must come before def (first-occurrence).
    const idxAbc = md.indexOf("`[doc:abc]`");
    const idxDef = md.indexOf("`[doc:def]`");
    expect(idxAbc).toBeGreaterThan(-1);
    expect(idxDef).toBeGreaterThan(-1);
    expect(idxAbc).toBeLessThan(idxDef);
    // Numbered footnotes.
    expect(md).toMatch(/1\. `\[doc:abc\]`/);
    expect(md).toMatch(/2\. `\[doc:def\]`/);
  });

  it("omits the Citations section when none are present", () => {
    const md = exportChatAsMarkdown(baseChat, [
      { id: 1, role: "user", content: "no citations here", created_at: null },
    ]);
    expect(md).not.toContain("## Citations");
  });

  it("falls back to the chat id when title is empty", () => {
    const md = exportChatAsMarkdown(
      { id: 7, title: "", created_at: null },
      []
    );
    expect(md).toContain("# Chat 7");
  });

  it("handles unknown roles by capitalising them", () => {
    const md = exportChatAsMarkdown(baseChat, [
      { id: 1, role: "developer", content: "hi", created_at: null },
    ]);
    expect(md).toContain("## Developer");
  });
});

describe("exportChatAsJson", () => {
  it("emits a version envelope + preserves reasoning_content", () => {
    const json = exportChatAsJson(baseChat, baseMessages);
    const parsed = JSON.parse(json) as Record<string, unknown>;
    expect(parsed.version).toBe(1);
    expect(typeof parsed.exported_at).toBe("string");
    const chat = parsed.chat as Record<string, unknown>;
    expect(chat.id).toBe(42);
    expect(chat.title).toBe("Test conversation");
    const messages = parsed.messages as Array<Record<string, unknown>>;
    expect(messages).toHaveLength(3);
    // The assistant message's reasoning_content survives the round-trip.
    const assistant = messages[1] as Record<string, unknown>;
    expect(assistant.reasoning_content).toBe("I should explain via code.");
  });

  it("normalises null reasoning_content for user/system messages", () => {
    const json = exportChatAsJson(baseChat, [
      { id: 1, role: "user", content: "hi", created_at: null },
    ]);
    const parsed = JSON.parse(json) as { messages: Array<Record<string, unknown>> };
    expect(parsed.messages[0]?.reasoning_content).toBeNull();
  });

  it("is pretty-printed with 2-space indent for git-diffability", () => {
    const json = exportChatAsJson(baseChat, baseMessages);
    // First newline + spaces under the opening brace.
    expect(json.startsWith("{\n  ")).toBe(true);
  });
});

describe("buildExportFilename", () => {
  it("sanitises spaces and emits the correct extension", () => {
    const name = buildExportFilename({ id: 7, title: "Hello World!", created_at: null }, "md");
    expect(name).toBe("hello-world-7.md");
  });

  it("falls back to 'chat' when the title is empty", () => {
    expect(buildExportFilename({ id: 1, title: "", created_at: null }, "json")).toBe("chat-1.json");
  });

  it("truncates very long titles", () => {
    const longTitle = "x".repeat(200);
    const name = buildExportFilename({ id: 9, title: longTitle, created_at: null }, "md");
    // 64-char title cap + "-9.md".
    expect(name.length).toBeLessThanOrEqual(64 + "-9.md".length);
  });
});
