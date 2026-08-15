/**
 * Unit tests for the sub-session prompt-build path.
 *
 * Covers ISSUE-17 + ISSUE-19 (derivative), updated for the token-leak fix:
 * persona templates (`presets.ts`) no longer carry `{{current_date}}` /
 * `{{tools}}` slots — they used to ship to the model as literal bytes on
 * the main-chat path, which never substituted them. `buildSubSessionSystemPrompt`
 * now frames the persona body itself: a leading `Today is <date>.` line and
 * a trailing tool-availability block, rather than substituting inline tokens.
 */
import { describe, it, expect } from "vitest";
import {
  buildSubSessionSystemPrompt,
  formatSubSessionDate,
} from "@/lib/subSession";
import { PRESETS } from "@/lib/presets";

describe("buildSubSessionSystemPrompt", () => {
  const today = "May 31, 2026";

  it("prepends a 'Today is <date>.' line ahead of the persona body", () => {
    const out = buildSubSessionSystemPrompt("Help me.", today);
    expect(out.startsWith("Today is May 31, 2026.")).toBe(true);
    expect(out).toContain("Help me.");
  });

  it("appends the tool-availability block after the persona body", () => {
    const template = "Pre.\n\nPost.";
    const out = buildSubSessionSystemPrompt(template, today);
    expect(out).toContain("Pre.");
    expect(out).toContain("Post.");
    expect(out.endsWith("resolve the question and stop.")).toBe(true);
  });

  it("frames a real preset (research) with date + tools, body verbatim", () => {
    const research = PRESETS.research;
    expect(research).toBeDefined();
    if (research === undefined) throw new Error("expected PRESETS.research to be defined");
    const built = buildSubSessionSystemPrompt(research.system_prompt, today);
    expect(built.startsWith(`Today is ${today}.`)).toBe(true);
    expect(built).toContain(research.system_prompt.trim());
    expect(built).toContain("## TOOLS AVAILABILITY");
  });

  it("frames a real preset (coder) with date + tools, body verbatim", () => {
    const coder = PRESETS.coder;
    expect(coder).toBeDefined();
    if (coder === undefined) throw new Error("expected PRESETS.coder to be defined");
    const built = buildSubSessionSystemPrompt(coder.system_prompt, today);
    expect(built.startsWith(`Today is ${today}.`)).toBe(true);
    expect(built).toContain(coder.system_prompt.trim());
    expect(built).toContain("## TOOLS AVAILABILITY");
  });

  it("collapses triple-or-more newlines at the persona-body seams", () => {
    const template = "A.\n\n\n\nB.";
    const out = buildSubSessionSystemPrompt(template, today);
    expect(out).not.toMatch(/\n{3,}/);
    expect(out).toContain("A.\n\nB.");
  });

  // ── F3 regression: integrations-branching contract (2026-06-09) ───────────
  //
  // The 06-07 bug had `startSubSession` baking the rendered prompt with an
  // empty integrations list, so the model ALWAYS read the "no tools"
  // branch even when integrations were forwarded to the BE. The fix is to
  // rebuild the prompt at every handleSubmit with the current
  // integrations list. These tests pin the conditional contract that
  // makes that rebuild meaningful.

  it("emits the no-tools block when integrations is empty (default)", () => {
    const template = "Pre.\n\nPost.";
    const out = buildSubSessionSystemPrompt(template, today, []);
    expect(out).toContain("No live tools are wired");
    expect(out).not.toContain("Live tools ARE wired");
  });

  it("emits the tools-wired block + each integration id when integrations is non-empty", () => {
    const template = "Pre.\n\nPost.";
    const out = buildSubSessionSystemPrompt(template, today, [
      "mcp/context7",
      "mcp/firecrawl",
    ]);
    expect(out).toContain("Live tools ARE wired");
    expect(out).toContain("mcp/context7");
    expect(out).toContain("mcp/firecrawl");
    expect(out).not.toContain("No live tools are wired");
  });

  it("toggles between branches when the same template is rebuilt with different integrations", () => {
    const template = "Pre.\n\nPost.";
    const empty = buildSubSessionSystemPrompt(template, today, []);
    const wired = buildSubSessionSystemPrompt(template, today, [
      "mcp/firecrawl",
    ]);
    expect(empty).toContain("No live tools are wired");
    expect(wired).toContain("Live tools ARE wired");
    expect(empty).not.toBe(wired);
  });
});

describe("formatSubSessionDate", () => {
  it("formats a Date as 'Month D, YYYY' (en-US long)", () => {
    const out = formatSubSessionDate(new Date(2026, 4, 31)); // May 31, 2026
    expect(out).toBe("May 31, 2026");
  });

  it("returns a date-shaped string for the current call", () => {
    const out = formatSubSessionDate();
    // Must include a 4-digit year and a comma — guarantees the replacement
    // produced something meaningful for the prompt.
    expect(out).toMatch(/\d{4}/);
    expect(out).toContain(",");
  });
});
