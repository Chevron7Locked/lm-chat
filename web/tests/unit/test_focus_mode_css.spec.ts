/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Focus-mode CSS-source assertions (chat.css).
 *
 * jsdom does not resolve var()/color-mix()/@media, so — like
 * test_continue_chip_css — this pins the *source* contract the design brief
 * requires rather than computed style:
 *   - chrome is hidden when `.is-focus-mode` is on (sidebar / top chrome /
 *     mobile dock).
 *   - the conversation + composer are capped at the ~68ch reading measure.
 *   - motion animates transform + opacity ONLY — never width/height/margin/
 *     padding — and is gated behind the `.lmchat-focus-animated` class.
 *   - a prefers-reduced-motion guard collapses the motion to instant.
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dir = dirname(fileURLToPath(import.meta.url));
const css = readFileSync(resolve(__dir, "../../src/styles/chat.css"), "utf-8");
const globalsCss = readFileSync(
  resolve(__dir, "../../src/globals.css"),
  "utf-8",
);

/** Declaration block for the first rule whose head starts at `selector {`. */
function ruleBlock(selector: string): string {
  const start = css.indexOf(`${selector} {`);
  expect(start, `selector ${selector} not found in chat.css`).toBeGreaterThanOrEqual(0);
  const open = css.indexOf("{", start);
  const close = css.indexOf("}", open);
  return css.slice(open + 1, close);
}

/** Declaration block that follows the first occurrence of a selector-list head. */
function blockAfter(anchor: string): string {
  const start = css.indexOf(anchor);
  expect(start, `anchor ${anchor} not found in chat.css`).toBeGreaterThanOrEqual(0);
  const open = css.indexOf("{", start);
  const close = css.indexOf("}", open);
  return css.slice(open + 1, close);
}

describe("focus mode CSS — chrome is hidden when on", () => {
  it("slides the sidebar slot out of flow via transform + opacity", () => {
    const block = ruleBlock(".is-focus-mode .lmchat-sidebar-slot");
    expect(block).toContain("position: absolute");
    expect(block).toContain("translateX(-100%)");
    expect(block).toContain("opacity: 0");
  });

  it("hides the desktop top chrome via transform + opacity", () => {
    const block = ruleBlock(".is-focus-mode .lmchat-topbar-shell");
    expect(block).toContain("position: absolute");
    expect(block).toContain("translateY(-100%)");
    expect(block).toContain("opacity: 0");
  });

  it("hides the MobileDock in focus mode", () => {
    const block = ruleBlock(".is-focus-mode .lmchat-mobile-dock");
    expect(block).toContain("display: none");
  });
});

describe("focus mode CSS — reading measure", () => {
  it("caps the conversation column at the focus measure", () => {
    const block = ruleBlock(".is-focus-mode .lmchat-messages-area");
    expect(block).toContain("max-width: var(--focus-measure)");
  });

  it("floats the composer at the same measure (no full-width divider/tint)", () => {
    const wrapper = ruleBlock(".is-focus-mode .lmchat-composer-wrapper");
    expect(wrapper).toContain("border-top: none");
    expect(wrapper).toContain("background: transparent");
    const inner = ruleBlock(".is-focus-mode .lmchat-composer-inner");
    expect(inner).toContain("max-width: var(--focus-measure)");
  });

  it("defines a ~68ch focus measure token", () => {
    expect(css).toContain("--focus-measure: 68ch");
  });
});

describe("focus mode CSS — every referenced token is defined", () => {
  // Regression guard: the focus block once referenced `--space-md` / `--space-sm`,
  // which don't exist in this project's semantic scale (it uses --space-glue /
  // --sibling / --group / --chapter and the --spacing-* primitives). An
  // undefined var() silently collapses (height:0, invalid top) and passes every
  // source-contract test above — but breaks live. This pins that every custom
  // property the focus block USES is DEFINED somewhere in the token sources.
  it("references only tokens defined in chat.css or globals.css", () => {
    const focusStart = css.indexOf("FOCUS MODE — reversible");
    expect(focusStart, "focus-mode block marker missing").toBeGreaterThanOrEqual(
      0,
    );
    const focusCss = css.slice(focusStart);

    const defined = new Set<string>();
    for (const src of [css, globalsCss]) {
      for (const m of src.matchAll(/(--[a-z0-9-]+)\s*:/g)) {
        // Group 1 is mandatory in the pattern — always captured on a match.
        const token = m[1];
        if (token === undefined) throw new Error("regex group 1 unexpectedly undefined");
        defined.add(token);
      }
    }

    const used = new Set<string>();
    for (const m of focusCss.matchAll(/var\((--[a-z0-9-]+)/g)) {
      const token = m[1];
      if (token === undefined) throw new Error("regex group 1 unexpectedly undefined");
      used.add(token);
    }

    const undef = [...used].filter((t) => !defined.has(t)).sort();
    expect(undef, `undefined token(s) referenced in focus CSS: ${undef.join(", ")}`).toEqual([]);
  });
});

describe("focus mode CSS — motion discipline", () => {
  it("animates transform + opacity ONLY, gated behind .lmchat-focus-animated", () => {
    // The non-@media transition rule (first occurrence).
    const block = blockAfter(".lmchat-focus-animated .lmchat-sidebar-slot");
    expect(block).toContain("transition");
    expect(block).toContain("transform");
    expect(block).toContain("opacity");
    // NEVER animate layout-thrashing properties.
    expect(block).not.toContain("width");
    expect(block).not.toContain("height");
    expect(block).not.toContain("margin");
    expect(block).not.toContain("padding");
  });

  it("uses ease-out-quart at ~220ms", () => {
    expect(css).toContain("--focus-motion-duration: 220ms");
    const block = blockAfter(".lmchat-focus-animated .lmchat-sidebar-slot");
    expect(block).toContain("var(--ease-out-quart)");
  });

  it("collapses motion to instant under prefers-reduced-motion", () => {
    const guardIdx = css.indexOf("Belt-and-suspenders");
    expect(guardIdx, "reduced-motion guard comment missing").toBeGreaterThanOrEqual(0);
    const region = css.slice(guardIdx, guardIdx + 500);
    expect(region).toContain("prefers-reduced-motion");
    expect(region).toContain(".lmchat-focus-animated");
    expect(region).toContain("transition: none");
  });
});
