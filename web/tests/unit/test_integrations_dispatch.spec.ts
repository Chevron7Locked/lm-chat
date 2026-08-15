/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Integration-toggle dispatch tests (bug fix: per-chat tool toggles ignored).
 *
 * Covers three scenarios from the confirmed bug report:
 *
 * (a) A chat with a stored selection that excludes mcp/firecrawl — the
 *     dispatched `integrations` field must exclude firecrawl.
 *
 * (b) A selection toggled fully off — the payload must send
 *     `integrations: []`, NOT omit the field (absent field → BE applies admin
 *     defaults, which was the original bug).
 *
 * (c) The sub-session stream call includes the per-chat integrations that
 *     `resolveChatIntegrationsField` would return, including an explicit [].
 *
 * Test strategy:
 *   - (a)+(b): Test `resolveChatIntegrationsField` directly against a seeded
 *     localStorage, then confirm the useSubSessionSSE hook forwards the field
 *     in the FormData it POSTs (via a fetch mock that captures the body).
 *   - (c): Confirm that the hook sends the `integrations` form field even
 *     when the resolved value is an empty array.
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import { resolveChatIntegrationsField } from "@/components/Composer";

// ─── Helpers ──────────────────────────────────────────────────────────────────

const STORAGE_KEY = (chatId: number) =>
  `lmchat:composer:integrations:${String(chatId)}`;

function seedStorage(chatId: number, integrations: string[]): void {
  localStorage.setItem(STORAGE_KEY(chatId), JSON.stringify(integrations));
}

function buildSseComplete(content = "done"): string {
  return `event: sub.complete\ndata: ${JSON.stringify({ final_content: content })}\n\n`;
}

function sseResponse(body: string): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      controller.enqueue(encoder.encode(body));
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

const BASE_PARAMS = {
  chatId: 55,
  modelId: "test-model",
  systemPrompt: "you are a helper",
  messages: [{ role: "user" as const, content: "hello" }],
};

// ─── Setup / teardown ─────────────────────────────────────────────────────────

beforeEach(() => {
  localStorage.clear();
  vi.clearAllMocks();
});

afterEach(() => {
  localStorage.clear();
  vi.restoreAllMocks();
});

// ─── (a) Stored selection excludes mcp/firecrawl ─────────────────────────────

describe("resolveChatIntegrationsField", () => {
  it("(a) returns stored list that excludes mcp/firecrawl", () => {
    const chatId = 10;
    seedStorage(chatId, ["mcp/context7", "mcp/deepwiki"]);
    const field = resolveChatIntegrationsField(chatId);
    expect(field).toBeDefined();
    expect(field).not.toContain("mcp/firecrawl");
    expect(field).toEqual(["mcp/context7", "mcp/deepwiki"]);
  });

  it("(b) returns explicit [] when selection is all-off", () => {
    const chatId = 11;
    seedStorage(chatId, []);
    const field = resolveChatIntegrationsField(chatId);
    // Must return [] (not undefined) so callers can send integrations:[] to BE.
    expect(field).toBeDefined();
    expect(Array.isArray(field)).toBe(true);
    expect((field as string[]).length).toBe(0);
  });

  it("returns undefined when no entry exists (never configured)", () => {
    const field = resolveChatIntegrationsField(99);
    expect(field).toBeUndefined();
  });
});

// ─── useSubSessionSSE FormData forwarding ─────────────────────────────────────

describe("useSubSessionSSE — integrations FormData forwarding", () => {
  it("(a) forwards the per-chat selection (excludes firecrawl) in the form body", async () => {
    const { useSubSessionSSE } = await import("@/hooks/useSubSessionSSE");

    let capturedFormData: FormData | undefined;
    global.fetch = vi.fn().mockImplementation((_url: unknown, init: RequestInit) => {
      capturedFormData = init.body as FormData;
      return Promise.resolve(sseResponse(buildSseComplete()));
    }) as typeof global.fetch;

    const { result } = renderHook(() => useSubSessionSSE());

    act(() => {
      result.current.stream({
        ...BASE_PARAMS,
        integrations: ["mcp/context7", "mcp/deepwiki"],
      });
    });

    await waitFor(() => {
      expect(result.current.state.status).toBe("complete");
    });

    expect(capturedFormData).toBeDefined();
    if (!capturedFormData) throw new Error("expected the FormData body to have been captured");
    const rawField = capturedFormData.get("integrations");
    expect(rawField).not.toBeNull();
    const parsed = JSON.parse(rawField as string) as string[];
    expect(parsed).toContain("mcp/context7");
    expect(parsed).not.toContain("mcp/firecrawl");
  });

  it("(b) sends integrations:[] (not absent) when all tools are toggled off", async () => {
    const { useSubSessionSSE } = await import("@/hooks/useSubSessionSSE");

    let capturedFormData: FormData | undefined;
    global.fetch = vi.fn().mockImplementation((_url: unknown, init: RequestInit) => {
      capturedFormData = init.body as FormData;
      return Promise.resolve(sseResponse(buildSseComplete()));
    }) as typeof global.fetch;

    const { result } = renderHook(() => useSubSessionSSE());

    act(() => {
      result.current.stream({
        ...BASE_PARAMS,
        integrations: [], // explicit all-off
      });
    });

    await waitFor(() => {
      expect(result.current.state.status).toBe("complete");
    });

    expect(capturedFormData).toBeDefined();
    if (!capturedFormData) throw new Error("expected the FormData body to have been captured");
    // The field MUST be present — absence would let the BE apply admin defaults.
    const rawField = capturedFormData.get("integrations");
    expect(rawField).not.toBeNull();
    const parsed = JSON.parse(rawField as string) as string[];
    expect(Array.isArray(parsed)).toBe(true);
    expect(parsed.length).toBe(0);
  });

  it("(c) sub-session stream: omits integrations field when no per-chat selection exists", async () => {
    // No localStorage entry for chatId 55 → resolveChatIntegrationsField returns
    // undefined → hook should NOT append the field (letting BE seed defaults).
    const { useSubSessionSSE } = await import("@/hooks/useSubSessionSSE");

    let capturedFormData: FormData | undefined;
    global.fetch = vi.fn().mockImplementation((_url: unknown, init: RequestInit) => {
      capturedFormData = init.body as FormData;
      return Promise.resolve(sseResponse(buildSseComplete()));
    }) as typeof global.fetch;

    const { result } = renderHook(() => useSubSessionSSE());

    act(() => {
      // integrations: undefined → no per-chat selection
      result.current.stream({ ...BASE_PARAMS });
    });

    await waitFor(() => {
      expect(result.current.state.status).toBe("complete");
    });

    expect(capturedFormData).toBeDefined();
    if (!capturedFormData) throw new Error("expected the FormData body to have been captured");
    // Field must be absent so the BE applies admin defaults.
    expect(capturedFormData.get("integrations")).toBeNull();
  });

  it("(c) sub-session stream: sends per-chat selection (from resolveChatIntegrationsField) including explicit []", async () => {
    // Simulate what Chat.tsx does after the fix: it calls
    // resolveChatIntegrationsField(cid) and spreads the result.
    // We test that an explicit empty array stored for the chat reaches the hook
    // and is forwarded in the form body.
    const chatId = 55;
    seedStorage(chatId, []); // user toggled all tools off

    const resolved = resolveChatIntegrationsField(chatId);
    // Verify the resolver returns [] (not undefined).
    expect(resolved).toBeDefined();
    expect(Array.isArray(resolved)).toBe(true);

    const { useSubSessionSSE } = await import("@/hooks/useSubSessionSSE");

    let capturedFormData: FormData | undefined;
    global.fetch = vi.fn().mockImplementation((_url: unknown, init: RequestInit) => {
      capturedFormData = init.body as FormData;
      return Promise.resolve(sseResponse(buildSseComplete()));
    }) as typeof global.fetch;

    const { result } = renderHook(() => useSubSessionSSE());

    // Mimic what the fixed Chat.tsx does: spread resolved only when defined.
    act(() => {
      result.current.stream({
        ...BASE_PARAMS,
        ...(resolved !== undefined && { integrations: resolved }),
      });
    });

    await waitFor(() => {
      expect(result.current.state.status).toBe("complete");
    });

    expect(capturedFormData).toBeDefined();
    if (!capturedFormData) throw new Error("expected the FormData body to have been captured");
    const rawField = capturedFormData.get("integrations");
    // Must be present and parse to [].
    expect(rawField).not.toBeNull();
    const parsed = JSON.parse(rawField as string) as string[];
    expect(Array.isArray(parsed)).toBe(true);
    expect(parsed.length).toBe(0);
  });
});
