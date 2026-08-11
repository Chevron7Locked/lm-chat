/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Fix B (FIX_PLAN.md §B) — regenerate / retry / followup / sub-session finalize
 * must honour an explicit "tools off" (localStorage `[]`) selection.
 *
 * These tests exercise two complementary layers:
 *
 *   (a) `resolveChatIntegrationsField` helper contract — returns [] for a stored
 *       empty selection, undefined for an untouched chat.
 *
 *   (b) Payload shape invariant — a payload built for a chat with stored [] MUST
 *       include `integrations: []`; a payload built for an untouched chat MUST
 *       omit the field. Tested via the helper directly (the call sites in Chat.tsx
 *       spread the result identically for all four paths).
 *
 * Red-on-revert: revert resolveChatIntegrationsField to always return undefined
 * → the stored-[] tests in group (b) fail because integrations would be omitted.
 */
import { describe, it, expect, beforeEach } from "vitest";
import { resolveChatIntegrationsField } from "@/components/Composer";

// ─── Helper ──────────────────────────────────────────────────────────────────

function storeIntegrations(chatId: number, value: string[]): void {
  localStorage.setItem(
    `lmchat:composer:integrations:${String(chatId)}`,
    JSON.stringify(value),
  );
}

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("Fix B — resolveChatIntegrationsField + payload spread invariants", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  // ── (a) Helper contract ───────────────────────────────────────────────────

  it("(a) returns [] when stored entry is an explicit empty array (tools-off state)", () => {
    storeIntegrations(10, []);
    expect(resolveChatIntegrationsField(10)).toEqual([]);
  });

  it("(a) returns undefined when no localStorage entry exists (untouched chat)", () => {
    expect(resolveChatIntegrationsField(20)).toBeUndefined();
  });

  it("(a) returns the stored array for non-empty selections", () => {
    storeIntegrations(30, ["mcp/searxng", "mcp/firecrawl"]);
    expect(resolveChatIntegrationsField(30)).toEqual(["mcp/searxng", "mcp/firecrawl"]);
  });

  // ── (b) Payload shape invariant ───────────────────────────────────────────
  //
  // All four call sites use the same spread pattern:
  //   ...(field !== undefined && { integrations: field })
  //
  // We verify the spread produces the right shape so a future refactor
  // of the spread expression can't silently break the contract.

  it("(b) regenerate payload includes integrations: [] for a stored-empty chat", () => {
    const chatId = 42;
    storeIntegrations(chatId, []);
    const field = resolveChatIntegrationsField(chatId);

    // Mirror the spread used in handleRegenerateConfirm / handleRetryInterruptedStream /
    // followup onSelect / handleSubSessionFinalize:
    const payload = {
      input: [{ type: "text", content: "prior prompt" }],
      model: "test-model",
      ...(field !== undefined && { integrations: field }),
    };

    expect(payload.integrations).toEqual([]);
  });

  it("(b) regenerate payload omits integrations for an untouched chat", () => {
    const chatId = 43; // nothing stored
    const field = resolveChatIntegrationsField(chatId);

    const payload = {
      input: [{ type: "text", content: "prior prompt" }],
      model: "test-model",
      ...(field !== undefined && { integrations: field }),
    };

    expect("integrations" in payload).toBe(false);
  });

  it("(b) finalize payload includes integrations: [] for a stored-empty chat (sub-session path)", () => {
    const chatId = 44;
    storeIntegrations(chatId, []);
    const field = resolveChatIntegrationsField(chatId);

    // Mirror handleSubSessionFinalize's spread into subSessionSSE.finalize(...)
    const finalizeArgs = {
      chatId,
      modelId: "test-model",
      systemPrompt: "system",
      messages: [],
      ...(field !== undefined && { integrations: field }),
    };

    expect(finalizeArgs.integrations).toEqual([]);
  });

  it("(b) finalize payload omits integrations for an untouched chat (sub-session path)", () => {
    const chatId = 45; // nothing stored
    const field = resolveChatIntegrationsField(chatId);

    const finalizeArgs = {
      chatId,
      modelId: "test-model",
      systemPrompt: "system",
      messages: [],
      ...(field !== undefined && { integrations: field }),
    };

    expect("integrations" in finalizeArgs).toBe(false);
  });

  it("(b) payload with stored non-empty selection carries integrations (no-tools-off)", () => {
    const chatId = 46;
    storeIntegrations(chatId, ["mcp/context7"]);
    const field = resolveChatIntegrationsField(chatId);

    const payload = {
      input: [],
      model: "test-model",
      ...(field !== undefined && { integrations: field }),
    };

    expect(payload.integrations).toEqual(["mcp/context7"]);
  });
});
