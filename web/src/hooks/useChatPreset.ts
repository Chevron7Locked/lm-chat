/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useChatPreset — per-chat active preset state + sticky-mode lock-in.
 *
 * Mirrors the previous app's ``meta._activePreset`` semantics:
 *
 *   1. Plain message (no leading ``/``)  → clears the active preset on send.
 *      ("sticky until a plain message is sent".)
 *   2. Preset slash command (``/code`` …) → sets ``active_preset`` on the
 *      chat. The next user message ships with the preset's system_prompt on
 *      the CanonicalChatRequest.
 *
 * The hook composes two layers:
 *
 *  - Local Zustand store: per-chat optimistic preset id keyed by chatId.
 *    Reads/writes are instant; the server-state PATCH happens in the
 *    background so the badge appears the moment the slash command fires
 *    (no PATCH-roundtrip flicker).
 *
 *  - Server persistence via useUpdateChat: writes ``active_preset`` to the
 *    chat's settings JSON. Backend route ``PATCH /api/chats/:id`` already
 *    accepts the field (``src/lmchat/routes/chats.py``).
 *
 * Hydration from server: when ``useChatsDirect`` resolves, ``hydrate()``
 * seeds local overrides from each chat's ``settings.active_preset`` so the
 * badge survives a page reload.
 *
 * The Composer is the primary write surface; ``ChatSettingsRail`` reads &
 * writes the same hook so changes from either UI sync.
 */
import { useCallback, useEffect } from "react";
import { create } from "zustand";
import { useUpdateChat } from "@/hooks/useChats";
import { getPreset, type Preset, DEFAULT_PRESET_ID, RAW_PRESET_ID } from "@/lib/presets";

// ─── Minimal hydration shape ────────────────────────────────────────────────

/** Minimal chat shape consumed by ``hydrateFromChats``. */
export interface ChatPresetHydrationEntry {
  id: number;
  settings?: { active_preset?: string | null } | undefined;
}

// ─── Zustand store ──────────────────────────────────────────────────────────

interface ChatPresetState {
  /**
   * Per-chat active preset id (e.g. ``"coder"``).
   * Missing key / ``DEFAULT_PRESET_ID`` = General (the implicit default).
   * ``RAW_PRESET_ID`` ("none") = explicit raw-model escape hatch (no system_prompt).
   */
  overrides: Record<number, string>;
  /** Set the in-memory preset for a chat. Use ``RAW_PRESET_ID`` to clear to raw mode. */
  setLocal: (chatId: number, presetId: string) => void;
  /**
   * Read the in-memory preset id for a chat.
   * Returns ``DEFAULT_PRESET_ID`` when no override has been set (absence → General).
   */
  getLocal: (chatId: number) => string;
  /** Seed from server data on first ``useChatsDirect`` resolution. */
  hydrateFromChats: (chats: ChatPresetHydrationEntry[]) => void;
}

export const useChatPresetStore = create<ChatPresetState>((set, get) => ({
  overrides: {},

  setLocal: (chatId: number, presetId: string) => {
    set((s) => ({
      overrides: { ...s.overrides, [chatId]: presetId },
    }));
  },

  getLocal: (chatId: number): string => {
    // Absence defaults to General (the implicit default), not raw/empty.
    return get().overrides[chatId] ?? DEFAULT_PRESET_ID;
  },

  hydrateFromChats: (chats: ChatPresetHydrationEntry[]) => {
    const current = get().overrides;
    const additions: Record<number, string> = {};
    for (const chat of chats) {
      const persisted = chat.settings?.active_preset;
      if (
        persisted != null &&
        // Legacy empty string is treated as unset → absence defaults to
        // DEFAULT_PRESET_ID (General).  An explicit ``RAW_PRESET_ID``
        // ("none") is a real value and hydrates normally.
        persisted !== "" &&
        // Don't overwrite an in-session override the user already set.
        !(chat.id in current)
      ) {
        additions[chat.id] = persisted;
      }
    }
    if (Object.keys(additions).length > 0) {
      set((s) => ({ overrides: { ...additions, ...s.overrides } }));
    }
  },
}));

// ─── Hook ───────────────────────────────────────────────────────────────────

export interface UseChatPresetReturn {
  /**
   * Active preset id.
   * Defaults to ``DEFAULT_PRESET_ID`` ("general") when no override has been
   * set — absence means General, not raw/empty.
   * ``RAW_PRESET_ID`` ("none") means the user explicitly chose no system prompt.
   */
  activePreset: string;
  /** Resolved Preset object, or null when preset is ``RAW_PRESET_ID`` / unknown. */
  preset: Preset | null;
  /**
   * Apply a preset (writes both local state + server-side ``active_preset``).
   * Pass ``RAW_PRESET_ID`` to switch to raw (no system prompt) mode.
   */
  setPreset: (presetId: string) => void;
  /**
   * Switches the chat to raw-model mode (no system prompt).
   * Sets ``RAW_PRESET_ID`` — does NOT write bare ``""`` anymore.
   */
  clearPreset: () => void;
}

/**
 * Per-chat preset state + mutator.
 *
 * The local Zustand override is written synchronously so the Composer badge
 * appears the instant a slash command fires; the PATCH to persist happens
 * in the background. If the PATCH fails the local optimistic value stays —
 * the next time ``hydrateFromChats`` runs it'll resync.
 */
export function useChatPreset(chatId: number | null): UseChatPresetReturn {
  // Subscribe to this chat's override so the badge re-renders on changes.
  // Absence defaults to DEFAULT_PRESET_ID (General) — not raw/empty.
  const localId = useChatPresetStore((s) =>
    chatId === null ? DEFAULT_PRESET_ID : (s.overrides[chatId] ?? DEFAULT_PRESET_ID),
  );
  const setLocal = useChatPresetStore((s) => s.setLocal);
  // useUpdateChat needs a chatId — we still call it unconditionally to keep
  // the hook order stable; we just no-op the mutation when chatId is null.
  const updateChat = useUpdateChat(chatId ?? 0);

  const setPreset = useCallback(
    (presetId: string): void => {
      if (chatId === null) return;
      setLocal(chatId, presetId);
      // Persist to backend (active_preset on chat settings JSON).
      // Pass RAW_PRESET_ID ("none") to mean "no system prompt" (raw mode).
      // The route accepts any string; "" is treated as null server-side.
      updateChat.mutate({ active_preset: presetId });
    },
    [chatId, setLocal, updateChat],
  );

  const clearPreset = useCallback((): void => {
    // Sets RAW_PRESET_ID so no bare "" is ever written to the store or BE.
    setPreset(RAW_PRESET_ID);
  }, [setPreset]);

  return {
    activePreset: localId,
    preset: getPreset(localId),
    setPreset,
    clearPreset,
  };
}

// ─── Hydration helper ───────────────────────────────────────────────────────

/**
 * Seed the per-chat preset store from server data.
 *
 * Mount this once near the top of the chat tree (e.g. Chat.tsx) so the badge
 * survives a page reload. Mirrors the ``chatSettingsStore.hydrateFromChats``
 * pattern.
 */
export function useHydrateChatPresets(
  chats: ChatPresetHydrationEntry[] | undefined,
): void {
  const hydrate = useChatPresetStore((s) => s.hydrateFromChats);
  // Stringify the persisted-preset projection so the deps array stays
  // primitive — re-hydrate only when the projection actually changes.
  const projection = (chats ?? [])
    .map((c) => `${String(c.id)}:${c.settings?.active_preset ?? ""}`)
    .join("|");
  useEffect(() => {
    if (chats === undefined) return;
    hydrate(chats);
    // `chats` is intentionally accessed via the primitive `projection`
    // so the effect re-fires only when persisted preset ids actually change.
  }, [projection, chats, hydrate]);
}
