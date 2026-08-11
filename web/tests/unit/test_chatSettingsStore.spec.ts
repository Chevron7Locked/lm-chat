/**
 * Unit tests for chatSettingsStore (P8a item 2).
 *
 * Covers:
 *  - globalReasoning default and cycling
 *  - per-chat override and empty-string sentinel
 *  - effectiveReasoning correctly resolves the sentinel
 *  - localStorage persistence
 */
import { describe, it, expect, beforeEach, vi } from "vitest";

async function freshStore() {
  const { useChatSettingsStore } = await import("@/stores/chatSettingsStore");
  return useChatSettingsStore;
}

describe("chatSettingsStore", () => {
  beforeEach(() => {
    vi.resetModules();
    vi.resetAllMocks();
    // Clear localStorage between tests.
    try {
      localStorage.clear();
    } catch {
      // Ignore.
    }
  });

  it("globalReasoning defaults to 'off'", async () => {
    const store = await freshStore();
    expect(store.getState().globalReasoning).toBe("off");
  });

  it("cycleGlobalReasoning cycles off→low→medium→high→off", async () => {
    const store = await freshStore();
    const { cycleGlobalReasoning } = store.getState();

    expect(store.getState().globalReasoning).toBe("off");
    cycleGlobalReasoning();
    expect(store.getState().globalReasoning).toBe("low");
    cycleGlobalReasoning();
    expect(store.getState().globalReasoning).toBe("medium");
    cycleGlobalReasoning();
    expect(store.getState().globalReasoning).toBe("high");
    cycleGlobalReasoning();
    expect(store.getState().globalReasoning).toBe("off");
  });

  it("setGlobalReasoning sets the level and persists to localStorage", async () => {
    const store = await freshStore();
    store.getState().setGlobalReasoning("high");
    expect(store.getState().globalReasoning).toBe("high");
    expect(localStorage.getItem("lmchat:settings:reasoning")).toBe("high");
  });

  it("effectiveReasoning returns globalReasoning when no per-chat override", async () => {
    const store = await freshStore();
    store.getState().setGlobalReasoning("medium");
    expect(store.getState().effectiveReasoning(42)).toBe("medium");
  });

  it("effectiveReasoning returns the per-chat override when set", async () => {
    const store = await freshStore();
    store.getState().setGlobalReasoning("medium");
    store.getState().setChatReasoning(1, "high");
    expect(store.getState().effectiveReasoning(1)).toBe("high");
  });

  it("effectiveReasoning treats empty-string override as fall-through to global", async () => {
    const store = await freshStore();
    store.getState().setGlobalReasoning("medium");
    store.getState().setChatReasoning(5, "");
    // Empty string must NOT override; must return globalReasoning.
    expect(store.getState().effectiveReasoning(5)).toBe("medium");
  });

  it("effectiveReasoning returns global when override is cleared via empty string", async () => {
    const store = await freshStore();
    store.getState().setGlobalReasoning("low");
    store.getState().setChatReasoning(3, "high");
    expect(store.getState().effectiveReasoning(3)).toBe("high");
    // Clear override.
    store.getState().setChatReasoning(3, "");
    expect(store.getState().effectiveReasoning(3)).toBe("low");
  });

  // K-001: hydrateFromChats tests
  it("hydrateFromChats seeds overrides from backend settings", async () => {
    const store = await freshStore();
    const chats = [
      { id: 10, settings: { reasoning_effort: "high" } },
      { id: 20, settings: { reasoning_effort: "low" } },
      { id: 30, settings: {} },  // no reasoning_effort
    ];
    store.getState().hydrateFromChats(chats);
    expect(store.getState().chatOverrides[10]).toBe("high");
    expect(store.getState().chatOverrides[20]).toBe("low");
    expect(store.getState().chatOverrides[30]).toBeUndefined();
  });

  it("hydrateFromChats ignores invalid reasoning_effort values", async () => {
    const store = await freshStore();
    const chats = [
      { id: 10, settings: { reasoning_effort: "extreme" } },  // invalid level
    ];
    store.getState().hydrateFromChats(chats);
    expect(store.getState().chatOverrides[10]).toBeUndefined();
  });

  it("hydrateFromChats does not overwrite in-session overrides", async () => {
    const store = await freshStore();
    // User set chat 10 to "medium" in this session.
    store.getState().setChatReasoning(10, "medium");
    // Backend says chat 10 was "high".
    const chats = [{ id: 10, settings: { reasoning_effort: "high" } }];
    store.getState().hydrateFromChats(chats);
    // In-session override must win.
    expect(store.getState().chatOverrides[10]).toBe("medium");
  });

  it("hydrateFromChats handles chats with null reasoning_effort", async () => {
    const store = await freshStore();
    const chats = [
      { id: 10, settings: { reasoning_effort: null } },
    ];
    store.getState().hydrateFromChats(chats);
    expect(store.getState().chatOverrides[10]).toBeUndefined();
  });

  it("hydrateFromChats handles chats with no settings field", async () => {
    const store = await freshStore();
    const chats = [{ id: 10 }];
    store.getState().hydrateFromChats(chats);
    expect(store.getState().chatOverrides[10]).toBeUndefined();
  });

  it("hydrateFromChats effectiveReasoning reflects hydrated values", async () => {
    const store = await freshStore();
    store.getState().setGlobalReasoning("off");
    const chats = [{ id: 99, settings: { reasoning_effort: "medium" } }];
    store.getState().hydrateFromChats(chats);
    expect(store.getState().effectiveReasoning(99)).toBe("medium");
  });
});
