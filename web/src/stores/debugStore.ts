/* SPDX-License-Identifier: Apache-2.0 */
/**
 * debugStore — frontend-only verbose-logging toggle.
 *
 * Backend has no `/api/admin/debug` endpoint; per the cluster brief we
 * fall back to a frontend-only toggle that controls `console.log`
 * verbosity inside the SPA.  Surfaces:
 *
 *   - `enabled`: current setting (persisted to localStorage)
 *   - `setEnabled(next)`: setter; also reflects via `window.__lmchatDebug`
 *
 * The store seeds from localStorage on first load and stays in sync.
 *
 * Follow-up: when the backend exposes an admin/debug endpoint, switch
 * `setEnabled` to POST and propagate the change to backend logging level.
 */
import { create } from "zustand";

const LS_KEY = "lmchat:debug-logging";

interface DebugState {
  enabled: boolean;
  setEnabled: (next: boolean) => void;
}

function readStored(): boolean {
  try {
    return localStorage.getItem(LS_KEY) === "true";
  } catch {
    return false;
  }
}

function persist(v: boolean): void {
  try {
    localStorage.setItem(LS_KEY, v ? "true" : "false");
  } catch {
    // ignore
  }
}

function reflectGlobal(v: boolean): void {
  try {
    // Public flag so non-React modules (api.ts, useSSE.ts) can branch on it.
    (window as unknown as { __lmchatDebug?: boolean }).__lmchatDebug = v;
  } catch {
    // SSR or sandboxed env — ignore.
  }
}

const initial = readStored();
reflectGlobal(initial);

export const useDebugStore = create<DebugState>((set) => ({
  enabled: initial,
  setEnabled: (next) => {
    persist(next);
    reflectGlobal(next);
    set({ enabled: next });
  },
}));
