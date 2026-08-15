/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useChatPreset — per-chat active preset state.
 *
 * CURRENT semantics (verified against the actual call sites — the v0.5.x
 * "plain message clears the preset" rule this docstring used to describe
 * does NOT exist in this codebase; that stale description cost a real
 * planning cycle on 2026-08-14 when it was read as a live behavior and
 * briefed as a design conflict that wasn't real):
 *
 *   1. ``active_preset`` is PERSISTENT — once set, it stays active on every
 *      subsequent plain (non-slash) message until the user changes it via
 *      the rail picker (``ChatSettingsRail``) or ``clearPreset()``. There is
 *      no auto-clear-on-plain-message anywhere in this codebase; see
 *      ``Composer.tsx``'s "active_preset is persistent... No auto-clear
 *      here" comment at its submit call site.
 *   2. The rail picker is the SOLE writer of a USER-driven ``active_preset``
 *      change (via ``setPreset``/``clearPreset``). Slash commands
 *      (``/code``, ``/research`` …) do NOT touch ``active_preset`` — they
 *      launch a transient, clean-context sub-agent sub-session instead (see
 *      ``web/src/lib/presets.ts``'s module docstring and
 *      ``Composer.tsx``'s ``dispatchSlashCommand``). Whatever preset is
 *      already active continues to ship with the next plain message
 *      regardless of any slash command run in between.
 *   3. C3 (model-decided role adoption, 2026-08-14) adds a SECOND writer:
 *      ``adoptModelPreset`` applies a preset out-of-band after a completed
 *      assistant turn (the ``mode_adopt`` SSE frame — see
 *      streaming_service._infer_mode_oob). To keep rule 1's guarantee exact
 *      for a USER's own pick, adoption is tracked via a separate
 *      ``sources`` layer (below) and is a no-op whenever the chat's current
 *      preset was set by the user — it only ever changes a preset that was
 *      unset or itself previously model-adopted.
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
   * ``RAW_PRESET_ID`` (\"none\") = explicit raw-model escape hatch (no system_prompt).
   */
  overrides: Record<number, string>;
  /**
   * Per-chat adoption source for the CURRENT ``overrides[chatId]`` value.
   * Added for C3 (model-decided role adoption, next turn):
   *
   *   - ``"user"``  — the value was set by an explicit user action: the
   *     rail picker's ``setPreset``/``clearPreset``, or hydration from a
   *     server-persisted ``settings.active_preset`` (the server doesn't
   *     currently distinguish who wrote it last, so a hydrated value is
   *     conservatively treated as user-sourced — see ``hydrateFromChats``
   *     below).
   *   - ``"model"`` — the value was applied out-of-band by
   *     ``adoptModel`` after a completed assistant turn (the
   *     ``mode_adopt`` SSE frame; see streaming_service._infer_mode_oob).
   *   - absent — no override has ever been set for this chat (implicit
   *     ``DEFAULT_PRESET_ID``); the model is free to adopt.
   *
   * This is the "distinguishable layer" that lets model adoption persist
   * across plain messages (matching a user-chosen preset's existing
   * semantics — see Composer.tsx's ``active_preset is persistent`` note)
   * WITHOUT ever silently overriding a preset the user picked themselves:
   * ``adoptModel`` is a no-op whenever the current source is ``"user"``.
   * Deliberately NOT persisted to the server (no schema change) — after a
   * reload a model-adopted mode re-hydrates as ``"user"``-sourced, same as
   * any other persisted preset. That's an accepted trade-off, not an
   * oversight: the persona itself still survives the reload (this is
   * unchanged from before C3 existed), only the "was this auto-picked"
   * badge resets.
   */
  sources: Record<number, "user" | "model">;
  /** Set the in-memory preset for a chat. Use ``RAW_PRESET_ID`` to clear to raw mode. */
  setLocal: (chatId: number, presetId: string) => void;
  /**
   * Read the in-memory preset id for a chat.
   * Returns ``DEFAULT_PRESET_ID`` when no override has been set (absence → General).
   */
  getLocal: (chatId: number) => string;
  /** Seed from server data on first ``useChatsDirect`` resolution. */
  hydrateFromChats: (chats: ChatPresetHydrationEntry[]) => void;
  /**
   * Apply a model-adopted preset id (C3). GUARDED: a no-op whenever the
   * chat's current preset was set by the USER (``sources[chatId] ===
   * "user"``) — a manual rail-picker choice keeps its documented
   * semantics exactly and is never silently overridden by an out-of-band
   * inference. Returns ``true`` when the preset was actually applied, so
   * the caller (``useChatPreset``'s ``adoptModelPreset``) knows whether to
   * also PATCH the server.
   */
  adoptModel: (chatId: number, presetId: string) => boolean;
}

export const useChatPresetStore = create<ChatPresetState>((set, get) => ({
  overrides: {},
  sources: {},

  setLocal: (chatId: number, presetId: string) => {
    set((s) => ({
      overrides: { ...s.overrides, [chatId]: presetId },
      // A direct setLocal call is always a user action (the rail picker's
      // setPreset, or clearPreset) — never called by the model-adoption
      // path, which goes through adoptModel below instead.
      sources: { ...s.sources, [chatId]: "user" },
    }));
  },

  getLocal: (chatId: number): string => {
    // Absence defaults to General (the implicit default), not raw/empty.
    return get().overrides[chatId] ?? DEFAULT_PRESET_ID;
  },

  hydrateFromChats: (chats: ChatPresetHydrationEntry[]) => {
    const current = get().overrides;
    const additions: Record<number, string> = {};
    const sourceAdditions: Record<number, "user"> = {};
    for (const chat of chats) {
      const persisted = chat.settings?.active_preset;
      if (
        persisted != null &&
        // Legacy empty string is treated as unset → absence defaults to
        // DEFAULT_PRESET_ID (General).  An explicit ``RAW_PRESET_ID``
        // (\"none\") is a real value and hydrates normally.
        persisted !== "" &&
        // Don't overwrite an in-session override the user already set.
        !(chat.id in current)
      ) {
        additions[chat.id] = persisted;
        // Server-persisted values are conservatively treated as
        // user-sourced (see ChatPresetState.sources doc) — the server
        // doesn't track who wrote the value last, and defaulting to
        // "user" is the safe choice: it never lets model adoption
        // silently override a preset the operator set in a prior session.
        sourceAdditions[chat.id] = "user";
      }
    }
    if (Object.keys(additions).length > 0) {
      set((s) => ({
        overrides: { ...additions, ...s.overrides },
        sources: { ...sourceAdditions, ...s.sources },
      }));
    }
  },

  adoptModel: (chatId: number, presetId: string): boolean => {
    if (get().sources[chatId] === "user") {
      // A user-chosen preset is never silently overridden by an
      // out-of-band inference — see the ChatPresetState.sources doc.
      return false;
    }
    set((s) => ({
      overrides: { ...s.overrides, [chatId]: presetId },
      sources: { ...s.sources, [chatId]: "model" },
    }));
    return true;
  },
}));

// ─── Hook ───────────────────────────────────────────────────────────────────

export interface UseChatPresetReturn {
  /**
   * Active preset id.
   * Defaults to ``DEFAULT_PRESET_ID`` (\"general\") when no override has been
   * set — absence means General, not raw/empty.
   * ``RAW_PRESET_ID`` (\"none\") means the user explicitly chose no system prompt.
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
   * Sets ``RAW_PRESET_ID`` — does NOT write bare ``\"\"`` anymore.
   */
  clearPreset: () => void;
  /**
   * C3 — apply a model-adopted preset id (the ``mode_adopt`` SSE frame;
   * see streaming_service._infer_mode_oob). A no-op whenever
   * ``activePreset`` was set by the user (see ``adoptedByModel`` /
   * ``ChatPresetState.sources``) — a manual rail-picker choice is never
   * silently overridden. Persists to the server on success, same as
   * ``setPreset``, so the adopted persona survives a reload.
   */
  adoptModelPreset: (presetId: string) => void;
  /**
   * True when ``activePreset`` was applied by ``adoptModelPreset`` (C3)
   * rather than chosen by the user. Drives the \"adopted automatically\"
   * hint on the rail's preset selector and the persona-label chip's
   * adopted styling — both REUSE existing surfaces rather than a new
   * indicator.
   */
  adoptedByModel: boolean;
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
  const source = useChatPresetStore((s) =>
    chatId === null ? undefined : s.sources[chatId],
  );
  const setLocal = useChatPresetStore((s) => s.setLocal);
  const adoptModel = useChatPresetStore((s) => s.adoptModel);
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

  const adoptModelPreset = useCallback(
    (presetId: string): void => {
      if (chatId === null) return;
      // adoptModel itself is the guard (no-op when the user owns the
      // current value) — only persist to the server when it actually
      // changed something locally.
      if (!adoptModel(chatId, presetId)) return;
      updateChat.mutate({ active_preset: presetId });
    },
    [chatId, adoptModel, updateChat],
  );

  return {
    activePreset: localId,
    preset: getPreset(localId),
    setPreset,
    clearPreset,
    adoptModelPreset,
    adoptedByModel: source === "model",
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
