/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests — detectLaunchableModes (launch-chip detection helper).
 *
 * Pins:
 * - matches each of the 5 sub-session preset slash commands.
 * - ignores non-preset builtin commands (/compare /compact /prompt /memory).
 * - word-boundary: "/researcher" and path-like "/research/data" don't match.
 * - dedupe: repeated mentions of the same command → one entry.
 * - first-appearance order across different commands.
 * - empty array when no commands are referenced.
 */
import { describe, it, expect } from "vitest";
import { detectLaunchableModes } from "@/lib/launchableModes";

describe("detectLaunchableModes", () => {
  it("matches /research", () => {
    expect(detectLaunchableModes("Try /research for this.")).toEqual([
      "research",
    ]);
  });

  it("matches /code", () => {
    expect(detectLaunchableModes("Try /code for this.")).toEqual(["coder"]);
  });

  it("matches /write", () => {
    expect(detectLaunchableModes("Try /write for this.")).toEqual([
      "creative",
    ]);
  });

  it("matches /analyze", () => {
    expect(detectLaunchableModes("Try /analyze for this.")).toEqual([
      "analyst",
    ]);
  });

  it("matches /architect", () => {
    expect(detectLaunchableModes("Try /architect for this.")).toEqual([
      "architect",
    ]);
  });

  it("ignores /compare, /compact, /prompt, /memory, /general", () => {
    expect(
      detectLaunchableModes(
        "/compare /compact /prompt /memory /general are not chips.",
      ),
    ).toEqual([]);
  });

  it("does not match /researcher (longer word)", () => {
    expect(detectLaunchableModes("Ask a /researcher about this.")).toEqual(
      [],
    );
  });

  it("does not match a path-like /research/data", () => {
    expect(detectLaunchableModes("See /research/data for details.")).toEqual(
      [],
    );
  });

  it("dedupes repeated mentions of the same command", () => {
    expect(
      detectLaunchableModes("/research this, or actually /research that."),
    ).toEqual(["research"]);
  });

  it("preserves first-appearance order across distinct commands", () => {
    expect(
      detectLaunchableModes(
        "You could /architect the system, or just /code it, or /research prior art.",
      ),
    ).toEqual(["architect", "coder", "research"]);
  });

  it("returns an empty array when no commands are referenced", () => {
    expect(detectLaunchableModes("Just a plain reply, no commands here.")).toEqual(
      [],
    );
  });

  it("returns an empty array for empty content", () => {
    expect(detectLaunchableModes("")).toEqual([]);
  });

  it("extracts the 5 modes from a real 'what can you do' reply", () => {
    // Verbatim shape of a live local-model reply (commands wrapped in inline
    // code, en-dash separators, the four non-launchable builtins mixed in) —
    // the chip row must surface exactly the five sub-agent modes, in order,
    // and ignore /compare /compact /prompt /memory.
    const reply = [
      "**General Help & Commands:**",
      "- `/research` – Deep, multi-step research in a clean sub-agent.",
      "- `/architect` – Design systems or plans before building.",
      "- `/code` – Focused coding with isolated context.",
      "- `/write` – Long-form or creative writing.",
      "- `/analyze` – Structured analysis of data/documents.",
      "- `/compare` – Run two models side-by-side on the same prompt.",
      "- `/compact` – Condense long conversations to save context.",
      "- `/prompt` – Insert saved prompts from your library.",
      "- `/memory <text>` – Pin a durable fact for me to remember.",
    ].join("\n");
    expect(detectLaunchableModes(reply)).toEqual([
      "research",
      "architect",
      "coder",
      "creative",
      "analyst",
    ]);
  });
});
