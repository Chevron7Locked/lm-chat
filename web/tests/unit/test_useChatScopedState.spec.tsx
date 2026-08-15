/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useChatScopedState — per-chat UI state at an explicit persistence tier
 * (Item 6, 2026-06-12 chat-flow remediation).
 *
 * Pins the hook's load-bearing invariants:
 *  - "memory" tier: chat-switch isolation — chat B never sees chat A's value.
 *  - "local" tier: hydrates from localStorage, persists writes, survives
 *    remount, recovers from corrupt JSON (and scrubs the bad entry), and is
 *    SSR-safe (no window → defaultValue, no throw).
 *  - validate predicate: hydration failures fall back to defaultValue AND
 *    scrub the offending entry.
 *  - localStorageKeyOverride: writes land at the custom key, not the
 *    synthesized one (legacy-key preservation contract).
 */
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { createElement } from "react";
import { renderToString } from "react-dom/server";
import {
  useChatScopedState,
  __resetChatScopedMemoryForTests,
} from "@/hooks/useChatScopedState";

beforeEach(() => {
  __resetChatScopedMemoryForTests();
  localStorage.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("useChatScopedState — memory tier", () => {
  it("isolates state per chat: set in A, default in B, back to A's value", () => {
    const { result, rerender } = renderHook(
      ({ chatId }: { chatId: number | null }) =>
        useChatScopedState<string | undefined>(
          chatId,
          "selectedModel",
          "memory",
          undefined,
        ),
      { initialProps: { chatId: 1 } },
    );

    expect(result.current[0]).toBeUndefined();
    act(() => {
      result.current[1]("model-a");
    });
    expect(result.current[0]).toBe("model-a");

    // Switch to chat B → hook presents B's value (the default), not A's.
    rerender({ chatId: 2 });
    expect(result.current[0]).toBeUndefined();

    // Switch back to A → A's value re-hydrates from the memory tier.
    rerender({ chatId: 1 });
    expect(result.current[0]).toBe("model-a");
  });

  it("keys null chatId separately from numeric ids", () => {
    const { result, rerender } = renderHook(
      ({ chatId }: { chatId: number | null }) =>
        useChatScopedState<string>(chatId, "k", "memory", "dflt"),
      { initialProps: { chatId: null as number | null } },
    );
    act(() => {
      result.current[1]("null-chat-value");
    });
    rerender({ chatId: 7 });
    expect(result.current[0]).toBe("dflt");
    rerender({ chatId: null });
    expect(result.current[0]).toBe("null-chat-value");
  });

  it("supports functional updates", () => {
    const { result } = renderHook(() =>
      useChatScopedState<number>(3, "count", "memory", 0),
    );
    act(() => {
      result.current[1]((prev) => prev + 5);
    });
    expect(result.current[0]).toBe(5);
  });

  it("validate failure on hydrate falls back to default and scrubs the entry", () => {
    const isString = (v: unknown): v is string => typeof v === "string";
    // Seed a bad (non-string) value into the memory tier via a looser hook.
    const seeded = renderHook(() =>
      useChatScopedState<unknown>(9, "badkey", "memory", null),
    );
    act(() => {
      seeded.result.current[1](12345);
    });
    seeded.unmount();

    // Validating consumer of the same slot: falls back + scrubs.
    const { result, rerender } = renderHook(
      ({ chatId }: { chatId: number | null }) =>
        useChatScopedState<string>(chatId, "badkey", "memory", "safe", isString),
      { initialProps: { chatId: 9 } },
    );
    expect(result.current[0]).toBe("safe");

    // Scrubbed: even a non-validating reader now sees nothing stored.
    rerender({ chatId: 9 });
    const reader = renderHook(() =>
      useChatScopedState<unknown>(9, "badkey", "memory", "empty"),
    );
    expect(reader.result.current[0]).toBe("empty");
  });
});

describe("useChatScopedState — local tier", () => {
  const isStringArray = (v: unknown): v is string[] =>
    Array.isArray(v) && v.every((x) => typeof x === "string");

  it("hydrates from localStorage", () => {
    localStorage.setItem(
      "lmchat:chat-scoped:tools:5",
      JSON.stringify(["a", "b"]),
    );
    const { result } = renderHook(() =>
      useChatScopedState<string[]>(5, "tools", "local", [], isStringArray),
    );
    expect(result.current[0]).toEqual(["a", "b"]);
  });

  it("persists writes under the synthesized key", () => {
    const { result } = renderHook(() =>
      useChatScopedState<string[]>(5, "tools", "local", [], isStringArray),
    );
    act(() => {
      result.current[1](["x"]);
    });
    expect(localStorage.getItem("lmchat:chat-scoped:tools:5")).toBe(
      JSON.stringify(["x"]),
    );
  });

  it("does NOT write storage on hydration alone (no-entry stays observable)", () => {
    renderHook(() =>
      useChatScopedState<string[]>(5, "tools", "local", [], isStringArray),
    );
    // Callers (e.g. Composer's defaults seed) rely on distinguishing
    // "no entry" from "persisted empty selection".
    expect(localStorage.getItem("lmchat:chat-scoped:tools:5")).toBeNull();
  });

  it("survives unmount/remount", () => {
    const first = renderHook(() =>
      useChatScopedState<string[]>(6, "tools", "local", [], isStringArray),
    );
    act(() => {
      first.result.current[1](["kept"]);
    });
    first.unmount();

    const second = renderHook(() =>
      useChatScopedState<string[]>(6, "tools", "local", [], isStringArray),
    );
    expect(second.result.current[0]).toEqual(["kept"]);
  });

  it("re-hydrates on chat switch — chat B never sees chat A's value", () => {
    localStorage.setItem("lmchat:chat-scoped:tools:2", JSON.stringify(["b"]));
    const { result, rerender } = renderHook(
      ({ chatId }: { chatId: number | null }) =>
        useChatScopedState<string[]>(chatId, "tools", "local", [], isStringArray),
      { initialProps: { chatId: 1 } },
    );
    act(() => {
      result.current[1](["a"]);
    });
    rerender({ chatId: 2 });
    expect(result.current[0]).toEqual(["b"]);
    // A's entry was not clobbered by the switch.
    expect(localStorage.getItem("lmchat:chat-scoped:tools:1")).toBe(
      JSON.stringify(["a"]),
    );
  });

  it("falls back to default on corrupt JSON and scrubs the entry", () => {
    localStorage.setItem("lmchat:chat-scoped:tools:5", "{not-json!");
    const { result } = renderHook(() =>
      useChatScopedState<string[]>(5, "tools", "local", [], isStringArray),
    );
    expect(result.current[0]).toEqual([]);
    expect(localStorage.getItem("lmchat:chat-scoped:tools:5")).toBeNull();
  });

  it("falls back to default and scrubs when the validate predicate rejects", () => {
    // Valid JSON, wrong shape (numbers, not strings).
    localStorage.setItem(
      "lmchat:chat-scoped:tools:5",
      JSON.stringify([1, 2, 3]),
    );
    const { result } = renderHook(() =>
      useChatScopedState<string[]>(5, "tools", "local", [], isStringArray),
    );
    expect(result.current[0]).toEqual([]);
    expect(localStorage.getItem("lmchat:chat-scoped:tools:5")).toBeNull();
  });

  it("SSR guard: renders defaultValue without throwing when window is undefined", () => {
    function Probe(): ReturnType<typeof createElement> {
      const [value] = useChatScopedState<string>(
        null,
        "ssr-key",
        "local",
        "ssr-fallback",
      );
      return createElement("span", null, value);
    }
    vi.stubGlobal("window", undefined);
    let html = "";
    expect(() => {
      html = renderToString(createElement(Probe));
    }).not.toThrow();
    expect(html).toContain("ssr-fallback");
  });

  it("honors localStorageKeyOverride — writes at the custom key, not the synthesized one", () => {
    const { result } = renderHook(() =>
      useChatScopedState<string[]>(
        42,
        "integrations",
        "local",
        [],
        isStringArray,
        {
          localStorageKeyOverride: (id) =>
            `lmchat:composer:integrations:${String(id)}`,
        },
      ),
    );
    act(() => {
      result.current[1](["mcp/searxng"]);
    });
    expect(localStorage.getItem("lmchat:composer:integrations:42")).toBe(
      JSON.stringify(["mcp/searxng"]),
    );
    expect(
      localStorage.getItem("lmchat:chat-scoped:integrations:42"),
    ).toBeNull();
  });

  it("hydrates from the override key (legacy entries keep working)", () => {
    localStorage.setItem(
      "lmchat:composer:integrations:42",
      JSON.stringify(["mcp/firecrawl"]),
    );
    const { result } = renderHook(() =>
      useChatScopedState<string[]>(
        42,
        "integrations",
        "local",
        [],
        isStringArray,
        {
          localStorageKeyOverride: (id) =>
            `lmchat:composer:integrations:${String(id)}`,
        },
      ),
    );
    expect(result.current[0]).toEqual(["mcp/firecrawl"]);
  });
});
