/**
 * Unit tests for themeStore.
 *
 * The store touches localStorage and document.documentElement. We stub both.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";

// Stub matchMedia before importing the store.
function mockMatchMedia(prefersLight: boolean) {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation((query: string) => ({
      matches: query.includes("light") ? prefersLight : !prefersLight,
      media: query,
      onchange: null,
      addEventListener: vi.fn(),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(),
    })),
  });
}

async function freshStore() {
  vi.resetModules();
  const { useThemeStore } = await import("@/stores/themeStore");
  return useThemeStore;
}

describe("themeStore", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    localStorage.clear();
    // Start with a dark OS preference by default.
    mockMatchMedia(false);
    document.documentElement.classList.remove("light", "dark");
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("defaults to 'dark' when nothing is in localStorage", async () => {
    // Dark is the default for the warm-future-library aesthetic.
    // Users opt INTO system-follow or light via Settings.
    const store = await freshStore();
    expect(store.getState().theme).toBe("dark");
  });

  it("reads stored value from localStorage on init", async () => {
    localStorage.setItem("lmchat:theme", "light");
    const store = await freshStore();
    expect(store.getState().theme).toBe("light");
  });

  it("applies 'light' class to documentElement when theme is light", async () => {
    localStorage.setItem("lmchat:theme", "light");
    await freshStore();
    expect(document.documentElement.classList.contains("light")).toBe(true);
  });

  it("removes 'light' class when theme is dark", async () => {
    document.documentElement.classList.add("light");
    localStorage.setItem("lmchat:theme", "dark");
    await freshStore();
    expect(document.documentElement.classList.contains("light")).toBe(false);
  });

  it("setTheme to 'dark' writes to localStorage and removes .light class", async () => {
    const store = await freshStore();
    store.getState().setTheme("dark");
    expect(localStorage.getItem("lmchat:theme")).toBe("dark");
    expect(document.documentElement.classList.contains("light")).toBe(false);
    expect(store.getState().theme).toBe("dark");
    expect(store.getState().effective).toBe("dark");
  });

  it("setTheme to 'light' writes to localStorage and adds .light class", async () => {
    const store = await freshStore();
    store.getState().setTheme("light");
    expect(localStorage.getItem("lmchat:theme")).toBe("light");
    expect(document.documentElement.classList.contains("light")).toBe(true);
    expect(store.getState().theme).toBe("light");
    expect(store.getState().effective).toBe("light");
  });

  it("setTheme to 'system' with dark OS → effective is dark", async () => {
    mockMatchMedia(false); // OS prefers dark.
    const store = await freshStore();
    store.getState().setTheme("system");
    expect(store.getState().theme).toBe("system");
    expect(store.getState().effective).toBe("dark");
    expect(document.documentElement.classList.contains("light")).toBe(false);
  });

  it("setTheme to 'system' with light OS → effective is light", async () => {
    mockMatchMedia(true); // OS prefers light.
    const store = await freshStore();
    store.getState().setTheme("system");
    expect(store.getState().theme).toBe("system");
    expect(store.getState().effective).toBe("light");
    expect(document.documentElement.classList.contains("light")).toBe(true);
  });

  it("setTheme to same value is a no-op (no localStorage write)", async () => {
    localStorage.setItem("lmchat:theme", "dark");
    const store = await freshStore();
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem");
    store.getState().setTheme("dark"); // same value
    // setItem should not have been called a second time for the same value.
    expect(setItemSpy).not.toHaveBeenCalled();
  });
});
