/* SPDX-License-Identifier: Apache-2.0 */
/**
 * LM Studio connection state machine — Zustand.
 *
 * Single source of truth for the *connection status* derived from probes
 * against the backend's LM Studio bridge.  The TanStack Query that
 * actually fetches `/api/models` lives in `useModels`; this store wraps
 * that query's state into a UI-facing tri-/quad-state that components
 * can render unambiguously:
 *
 *   - "idle":       no probe has completed yet (mount-time hydration)
 *   - "probing":    a probe is in flight
 *   - "connected":  last probe succeeded AND ≥1 model is loaded
 *   - "no_models":  last probe succeeded but ZERO models are loaded
 *                   (LM Studio is reachable but cannot stream — UX must
 *                   nudge the user to load a model)
 *   - "error":      last probe failed
 *
 * Why a separate store and not just useQuery state?
 *   The Settings → LM Studio tab also runs a user-initiated "Test
 *   connection" probe via POST /api/settings/lmstudio/test that takes
 *   a freshly-typed base_url + api_key (not yet saved).  That probe
 *   needs to feed the same status display the rest of the app reads,
 *   without the user having to save first.  The store lets the Settings
 *   page push results in alongside the background TanStack probe.
 */
import { create } from "zustand";

export type LmStudioConnectionStatus =
  | "idle"
  | "probing"
  | "connected"
  | "no_models"
  | "error";

interface ResolveOpts {
  ok: boolean;
  /** Total reachable models (loaded + unloaded). */
  modelCount?: number;
  /** Loaded models — needed to distinguish "connected" from "no_models". */
  loadedCount?: number;
  /** Human-readable error if ok=false. */
  error?: string;
}

interface LmStudioState {
  status: LmStudioConnectionStatus;
  modelCount: number;
  loadedCount: number;
  lastError: string | null;
  /** Epoch ms; null when no probe has completed. */
  lastProbeAt: number | null;

  beginProbe: () => void;
  resolveProbe: (opts: ResolveOpts) => void;
  reset: () => void;
}

export const useLmStudioStore = create<LmStudioState>((set) => ({
  status: "idle",
  modelCount: 0,
  loadedCount: 0,
  lastError: null,
  lastProbeAt: null,

  beginProbe: (): void => {
    set((s) => ({
      // Don't reset prior result fields — surface the previous count
      // while a refresh is in flight so the UI doesn't flicker to 0.
      status: s.status === "idle" ? "probing" : s.status,
    }));
  },

  resolveProbe: (opts: ResolveOpts): void => {
    const modelCount = opts.modelCount ?? 0;
    const loadedCount = opts.loadedCount ?? 0;
    let status: LmStudioConnectionStatus;
    if (!opts.ok) {
      status = "error";
    } else if (loadedCount === 0) {
      status = "no_models";
    } else {
      status = "connected";
    }
    set({
      status,
      modelCount,
      loadedCount,
      lastError: opts.ok ? null : (opts.error ?? "Probe failed."),
      lastProbeAt: Date.now(),
    });
  },

  reset: (): void => {
    set({
      status: "idle",
      modelCount: 0,
      loadedCount: 0,
      lastError: null,
      lastProbeAt: null,
    });
  },
}));
