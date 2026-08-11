/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useReasoningOverridePersistence — hydrate per-chat reasoning-effort
 * overrides from the backend on first load, then persist them back
 * whenever the in-session override changes.
 *
 * Extracted from pages/Chat.tsx — a self-contained
 * cluster with zero cross-references elsewhere in the component. Reads
 * `useChatSettingsStore` internally (the store destructure travels with the
 * cluster since `hydrateFromChats` / `chatOverrides` are used only here).
 */
import { useEffect, useRef } from "react";
import type { ChatSummary, useUpdateChat } from "@/hooks/useChats";
import { useChatSettingsStore } from "@/stores/chatSettingsStore";
import type { ReasoningLevel } from "@/stores/chatSettingsStore";

/** Mirrors useUpdateChat's return type exactly — avoids re-declaring the
 * (unexported) UpdateChatBody shape here. */
export type ReasoningOverridePersistMutation = ReturnType<typeof useUpdateChat>;

// ─── Shared reasoning-level derivation ────────────────────────────────────
//
// Both ChatSettingsRail (the reasoning <select>) and ReasoningToggle (the
// cycle button) need to agree on "what is this chat's effective reasoning
// override". Before this fix they derived it independently: the Rail read
// `chat.settings.reasoning_effort` directly, while the Toggle read ONLY
// `chatSettingsStore.chatOverrides` (a session-local map seeded once at
// hydrate time) and never looked at `chat.settings` at all. Any chat
// visited before hydration ran, or created after it, showed a stale or
// simply wrong value on the Toggle even though the Rail was correct.
//
// REASONING_LEVELS / isReasoningLevel / deriveChatReasoningOverride are now
// the ONE place this settings-parsing happens: ChatSettingsRail imports
// them instead of keeping its own duplicate copy, and passes its resolved
// value down to ReasoningToggle via the `reasoningOverride` prop so the
// mounted pair can never disagree.

/** Ordered levels — the single canonical list. Both ChatSettingsRail's
 *  <select> options and ReasoningToggle's cycle/dropdown render off this
 *  (each previously kept its own duplicate array). */
export const REASONING_LEVELS: readonly ReasoningLevel[] = [
  "off",
  "low",
  "medium",
  "high",
];

function isReasoningLevel(v: unknown): v is ReasoningLevel {
  return (
    typeof v === "string" && (REASONING_LEVELS as readonly string[]).includes(v)
  );
}

/** Minimal shape needed to read a chat's persisted reasoning override. */
export interface ReasoningSettingsShape {
  reasoning_effort?: unknown;
  reasoning?: unknown;
}

/**
 * deriveChatReasoningOverride — the per-chat reasoning-effort override
 * baked into `chat.settings` (server truth, mirrored in the chatKeys
 * cache). Checks the canonical `reasoning_effort` key first, then the
 * legacy `reasoning` alias for backward-compat with chats saved before the
 * canonical-key migration. Returns "" when
 * neither key carries a recognized level — callers resolve "" against
 * their own global default.
 */
export function deriveChatReasoningOverride(
  settings: ReasoningSettingsShape | null | undefined,
): ReasoningLevel | "" {
  if (settings == null) return "";
  if (isReasoningLevel(settings.reasoning_effort)) return settings.reasoning_effort;
  if (isReasoningLevel(settings.reasoning)) return settings.reasoning;
  return "";
}

export interface UseReasoningOverridePersistenceArgs {
  chatId: number | null;
  chatsData: ChatSummary[] | undefined;
  updateChat: ReasoningOverridePersistMutation;
}

/**
 * Runs the reasoning-override hydrate + persist side-effects. No return
 * value — pure side-effect hook.
 */
export function useReasoningOverridePersistence({
  chatId,
  chatsData,
  updateChat,
}: UseReasoningOverridePersistenceArgs): void {
  const { hydrateFromChats, chatOverrides } = useChatSettingsStore();

  // Hydrate per-chat reasoning overrides from backend on first load.
  // Runs once when chatsData becomes available. hydrateFromChats only seeds
  // chats that don't already have an in-session override.
  const hydratedRef = useRef(false);
  useEffect(() => {
    if (chatsData && !hydratedRef.current) {
      hydratedRef.current = true;
      hydrateFromChats(chatsData);
    }
  }, [chatsData, hydrateFromChats]);

  // Persist reasoning_effort to backend whenever the per-chat override
  // changes for the current chat. Fires the PATCH mutation so the value
  // survives a page reload.
  const prevReasoningRef = useRef<string | undefined>(undefined);
  useEffect(() => {
    if (chatId === null) return;
    const override = chatOverrides[chatId];
    // undefined means no override has ever been set in this session — skip.
    if (override === undefined) return;
    // Only fire when the value actually changes.
    if (override === prevReasoningRef.current) return;
    prevReasoningRef.current = override;
    // Persist: "" clears the override; any other level sets it.
    updateChat.mutate({ reasoning_effort: override });
  }, [chatId, chatOverrides, updateChat]);
}
