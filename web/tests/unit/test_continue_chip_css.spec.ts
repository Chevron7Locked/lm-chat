/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Continue-chip CSS presence test (F4 closeout, audit 2026-06-10).
 *
 * `.lmchat-continue-chip` was referenced by ChatMessage.tsx but had NO
 * companion rule in chat-message.css — the chip rendered as bare text.
 * This test pins the rule's existence and its required declarations
 * (non-empty background + border on the warning tokens, NOT the danger
 * tokens used by the Stopped chip — the Continue chip is informational,
 * not an error state).
 *
 * jsdom does not resolve var()/color-mix(), so this asserts at the
 * stylesheet-source level (rule block + declaration presence) rather
 * than via getComputedStyle.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dir = dirname(fileURLToPath(import.meta.url));
const cssPath = resolve(__dir, "../../src/styles/chat-message.css");
const css = readFileSync(cssPath, "utf-8");

/** Extract the declaration block for a selector (first match). */
function ruleBlock(selector: string): string {
  const start = css.indexOf(`${selector} {`);
  expect(start, `selector ${selector} not found in chat-message.css`).toBeGreaterThanOrEqual(0);
  const open = css.indexOf("{", start);
  const close = css.indexOf("}", open);
  return css.slice(open + 1, close);
}

describe(".lmchat-continue-chip CSS (F4 closeout)", () => {
  it("defines a rule block for .lmchat-continue-chip", () => {
    expect(css).toContain(".lmchat-continue-chip {");
  });

  it("has non-empty background and border declarations", () => {
    const block = ruleBlock(".lmchat-continue-chip");
    const background = /background:\s*([^;]+);/.exec(block)?.[1]?.trim();
    const border = /border:\s*([^;]+);/.exec(block)?.[1]?.trim();
    expect(background).toBeTruthy();
    expect(border).toBeTruthy();
  });

  it("uses warning tokens, not the Stopped chip's danger tokens", () => {
    const block = ruleBlock(".lmchat-continue-chip");
    expect(block).toContain("--color-warning");
    expect(block).not.toContain("--color-danger");
  });

  it("mirrors the Stopped chip's pill grammar (radius, micro type, uppercase)", () => {
    const block = ruleBlock(".lmchat-continue-chip");
    expect(block).toContain("border-radius: var(--radius-pill");
    expect(block).toContain("font-size: var(--fs-micro)");
    expect(block).toContain("text-transform: uppercase");
  });
});
