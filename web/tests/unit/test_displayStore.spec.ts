/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for displayStore.
 *
 * The store touches localStorage and document.documentElement. Both are
 * available in the jsdom environment used by vitest.
 *
 * Pattern mirrors test_themeStore.spec.ts: use vi.resetModules() + dynamic
 * import to get a fresh store instance for each test so initialization
 * logic re-runs cleanly.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// ─── Helpers ─────────────────────────────────────────────────────────────────

async function freshStore() {
  vi.resetModules();
  const { useDisplayStore } = await import("@/stores/displayStore");
  return useDisplayStore;
}

const ATTR_TEXT_SIZE = "data-text-size";
const ATTR_DENSITY = "data-density";
const ATTR_MESSAGE_STYLE = "data-message-style";

const LS_TEXT_SIZE = "lmchat:text-size";
const LS_DENSITY = "lmchat:density";
const LS_MESSAGE_STYLE = "lmchat:message-style";

// ─── Setup / teardown ────────────────────────────────────────────────────────

beforeEach(() => {
  vi.resetAllMocks();
  localStorage.clear();
  // Clear all three data-attributes between tests.
  document.documentElement.removeAttribute(ATTR_TEXT_SIZE);
  document.documentElement.removeAttribute(ATTR_DENSITY);
  document.documentElement.removeAttribute(ATTR_MESSAGE_STYLE);
});

afterEach(() => {
  vi.restoreAllMocks();
});

// ─── Default values ──────────────────────────────────────────────────────────

describe("displayStore — defaults", () => {
  it("defaults textSize to 'md' when nothing in localStorage", async () => {
    const store = await freshStore();
    expect(store.getState().textSize).toBe("md");
  });

  it("defaults density to 'comfortable' when nothing in localStorage", async () => {
    const store = await freshStore();
    expect(store.getState().density).toBe("comfortable");
  });

  it("defaults messageStyle to 'bubbles' when nothing in localStorage", async () => {
    const store = await freshStore();
    expect(store.getState().messageStyle).toBe("bubbles");
  });
});

// ─── Init applies attributes (even for defaults) ─────────────────────────────

describe("displayStore — init attributes (no FOUC)", () => {
  it("sets data-text-size='md' on documentElement for the default", async () => {
    await freshStore();
    expect(document.documentElement.getAttribute(ATTR_TEXT_SIZE)).toBe("md");
  });

  it("sets data-density='comfortable' on documentElement for the default", async () => {
    await freshStore();
    expect(document.documentElement.getAttribute(ATTR_DENSITY)).toBe("comfortable");
  });

  it("sets data-message-style='bubbles' on documentElement for the default", async () => {
    await freshStore();
    expect(document.documentElement.getAttribute(ATTR_MESSAGE_STYLE)).toBe("bubbles");
  });
});

// ─── Reads stored values ──────────────────────────────────────────────────────

describe("displayStore — reads from localStorage on init", () => {
  it("reads textSize from localStorage", async () => {
    localStorage.setItem(LS_TEXT_SIZE, "lg");
    const store = await freshStore();
    expect(store.getState().textSize).toBe("lg");
    expect(document.documentElement.getAttribute(ATTR_TEXT_SIZE)).toBe("lg");
  });

  it("reads density 'compact' from localStorage", async () => {
    localStorage.setItem(LS_DENSITY, "compact");
    const store = await freshStore();
    expect(store.getState().density).toBe("compact");
    expect(document.documentElement.getAttribute(ATTR_DENSITY)).toBe("compact");
  });

  it("reads messageStyle 'flat' from localStorage", async () => {
    localStorage.setItem(LS_MESSAGE_STYLE, "flat");
    const store = await freshStore();
    expect(store.getState().messageStyle).toBe("flat");
    expect(document.documentElement.getAttribute(ATTR_MESSAGE_STYLE)).toBe("flat");
  });

  it("ignores invalid localStorage values and falls back to default", async () => {
    localStorage.setItem(LS_TEXT_SIZE, "giant"); // invalid
    localStorage.setItem(LS_DENSITY, "airy"); // invalid
    localStorage.setItem(LS_MESSAGE_STYLE, "cards"); // invalid
    const store = await freshStore();
    expect(store.getState().textSize).toBe("md");
    expect(store.getState().density).toBe("comfortable");
    expect(store.getState().messageStyle).toBe("bubbles");
  });
});

// ─── Setters ─────────────────────────────────────────────────────────────────

describe("displayStore — setTextSize", () => {
  it("updates state, sets data-attribute, and persists to localStorage", async () => {
    const store = await freshStore();
    store.getState().setTextSize("sm");
    expect(store.getState().textSize).toBe("sm");
    expect(document.documentElement.getAttribute(ATTR_TEXT_SIZE)).toBe("sm");
    expect(localStorage.getItem(LS_TEXT_SIZE)).toBe("sm");
  });

  it("switches from 'sm' back to 'md'", async () => {
    localStorage.setItem(LS_TEXT_SIZE, "sm");
    const store = await freshStore();
    store.getState().setTextSize("md");
    expect(store.getState().textSize).toBe("md");
    expect(document.documentElement.getAttribute(ATTR_TEXT_SIZE)).toBe("md");
    expect(localStorage.getItem(LS_TEXT_SIZE)).toBe("md");
  });
});

describe("displayStore — setDensity", () => {
  it("updates state, sets data-attribute, and persists to localStorage", async () => {
    const store = await freshStore();
    store.getState().setDensity("compact");
    expect(store.getState().density).toBe("compact");
    expect(document.documentElement.getAttribute(ATTR_DENSITY)).toBe("compact");
    expect(localStorage.getItem(LS_DENSITY)).toBe("compact");
  });

  it("can switch back to 'comfortable'", async () => {
    localStorage.setItem(LS_DENSITY, "compact");
    const store = await freshStore();
    store.getState().setDensity("comfortable");
    expect(store.getState().density).toBe("comfortable");
    expect(document.documentElement.getAttribute(ATTR_DENSITY)).toBe("comfortable");
  });
});

describe("displayStore — setMessageStyle", () => {
  it("updates state, sets data-attribute, and persists to localStorage", async () => {
    const store = await freshStore();
    store.getState().setMessageStyle("flat");
    expect(store.getState().messageStyle).toBe("flat");
    expect(document.documentElement.getAttribute(ATTR_MESSAGE_STYLE)).toBe("flat");
    expect(localStorage.getItem(LS_MESSAGE_STYLE)).toBe("flat");
  });

  it("can switch back to 'bubbles'", async () => {
    localStorage.setItem(LS_MESSAGE_STYLE, "flat");
    const store = await freshStore();
    store.getState().setMessageStyle("bubbles");
    expect(store.getState().messageStyle).toBe("bubbles");
    expect(document.documentElement.getAttribute(ATTR_MESSAGE_STYLE)).toBe("bubbles");
  });
});
