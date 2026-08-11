/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useChatScopedState — per-chat UI state at an explicit persistence tier.
 *
 * Per-chat UI state used to leak across chats because it lived in
 * un-keyed `useState` (e.g. `selectedModel` in Chat.tsx) or in ~50
 * lines of bespoke localStorage plumbing (e.g. `selectedIntegrations`
 * in Composer.tsx). This hook is the single shape for both:
 *
 *   - `"memory"` tier — module-level Map keyed `<chatId|"null">:<key>`.
 *     Survives route changes within the tab, scoped per chat by
 *     construction, gone on reload.
 *   - `"local"` tier — localStorage (JSON-encoded), SSR-safe lazy init,
 *     corrupt-entry removal on parse failure, persist effect pinned to the
 *     resolved key. `options.localStorageKeyOverride` lets an adopting site
 *     keep a pre-existing key shape (no user-state invalidation).
 *
 * When `chatId` changes the hook re-hydrates: the caller always sees chat
 * B's value (or `defaultValue`) after navigating to chat B — never chat A's.
 *
 * Notes for adopters:
 *   - `key` and `tier` must be stable for the lifetime of the call site.
 *   - The hook does NOT write storage on hydration — only after the setter
 *     runs. Callers can therefore distinguish "no entry yet" (e.g. for
 *     seed-once-from-defaults logic) by probing storage themselves.
 *   - `"local"` tier values must survive `JSON.stringify`/`JSON.parse`
 *     round-trips (no `undefined`, functions, Dates, …).
 *   - Function-typed `T` is unsupported (the setter treats a function
 *     argument as a functional updater).
 */
import { useCallback, useEffect, useRef, useState } from "react";

type Tier = "memory" | "local";

export interface ChatScopedStateOptions {
  /**
   * `"local"` tier only: produce the full localStorage key for a chat id,
   * overriding the synthesized `lmchat:chat-scoped:<key>:<chatId>` shape.
   * Used to preserve legacy key shapes already in users' storage.
   */
  localStorageKeyOverride?: (chatId: number | null) => string;
}

/** Module-level memory tier — survives route changes, gone on reload. */
const memoryStore = new Map<string, unknown>();

/** Test-only: wipe the memory tier between tests. */
export function __resetChatScopedMemoryForTests(): void {
  memoryStore.clear();
}

function scrubLocal(storageKey: string): void {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(storageKey);
  } catch {
    // Private browsing / quota — nothing we can do.
  }
}

function hydrate<T>(
  tier: Tier,
  storageKey: string,
  defaultValue: T,
  validate?: (v: unknown) => v is T,
): T {
  if (tier === "memory") {
    if (!memoryStore.has(storageKey)) return defaultValue;
    const value = memoryStore.get(storageKey);
    if (validate !== undefined && !validate(value)) {
      // Bad entry (shape drifted between writes) — scrub and fall back.
      memoryStore.delete(storageKey);
      return defaultValue;
    }
    return value as T;
  }
  // "local" tier — SSR-safe: no window means no storage to read.
  if (typeof window === "undefined") return defaultValue;
  let raw: string | null;
  try {
    raw = window.localStorage.getItem(storageKey);
  } catch {
    return defaultValue;
  }
  if (raw === null) return defaultValue;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (validate !== undefined && !validate(parsed)) {
      scrubLocal(storageKey);
      return defaultValue;
    }
    return parsed as T;
  } catch {
    // Corrupt entry — remove it so future hydrations start clean.
    scrubLocal(storageKey);
    return defaultValue;
  }
}

function persist(tier: Tier, storageKey: string, value: unknown): void {
  if (tier === "memory") {
    memoryStore.set(storageKey, value);
    return;
  }
  if (typeof window === "undefined") return;
  try {
    window.localStorage.setItem(storageKey, JSON.stringify(value));
  } catch {
    // Quota / private browsing — state still works in-tab.
  }
}

/**
 * Internal cell: `key` pins which chat the value was hydrated for, `dirty`
 * marks values the user actually wrote (hydrated values are never
 * re-persisted, so "no entry yet" stays observable to callers).
 */
interface Cell<T> {
  key: string;
  value: T;
  dirty: boolean;
}

export function useChatScopedState<T>(
  chatId: number | null,
  key: string,
  tier: Tier,
  defaultValue: T,
  validate?: (v: unknown) => v is T,
  options?: ChatScopedStateOptions,
): [T, (next: T | ((prev: T) => T)) => void] {
  const storageKey =
    tier === "memory"
      ? `${String(chatId)}:${key}`
      : (options?.localStorageKeyOverride?.(chatId) ??
        `lmchat:chat-scoped:${key}:${String(chatId)}`);

  // defaultValue / validate are often fresh literals per render; route the
  // setter's rare stale-closure rehydration through a ref so the setter
  // identity only changes with the resolved key.
  const latestRef = useRef({ defaultValue, validate });
  useEffect(() => {
    latestRef.current = { defaultValue, validate };
  });

  const [cell, setCell] = useState<Cell<T>>(() => ({
    key: storageKey,
    value: hydrate(tier, storageKey, defaultValue, validate),
    dirty: false,
  }));

  // Chat switch (or any key change): re-hydrate for the new key. Render-time
  // state adjustment per React's "adjust state when props change" pattern —
  // React re-renders immediately, so consumers never commit chat A's value
  // under chat B's id.
  if (cell.key !== storageKey) {
    setCell({
      key: storageKey,
      value: hydrate(tier, storageKey, defaultValue, validate),
      dirty: false,
    });
  }

  // Persist effect — pinned to the resolved key: only user-written (dirty)
  // values are stored, and only under the key they were written for. A
  // mid-switch render can never flush chat A's value into chat B's slot.
  useEffect(() => {
    if (!cell.dirty || cell.key !== storageKey) return;
    persist(tier, cell.key, cell.value);
  }, [cell, storageKey, tier]);

  const setValue = useCallback(
    (next: T | ((prev: T) => T)): void => {
      setCell((prev) => {
        const base =
          prev.key === storageKey
            ? prev.value
            : hydrate(
                tier,
                storageKey,
                latestRef.current.defaultValue,
                latestRef.current.validate,
              );
        const value =
          typeof next === "function" ? (next as (prev: T) => T)(base) : next;
        return { key: storageKey, value, dirty: true };
      });
    },
    [storageKey, tier],
  );

  return [cell.value, setValue];
}
