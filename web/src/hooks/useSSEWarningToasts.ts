/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useSSEWarningToasts — surface non-terminal `warning` SSE frames as toasts.
 *
 * Extracted from pages/Chat.tsx — a self-contained, side-effect-only
 * cluster with zero cross-references elsewhere in the component.
 */
import { useEffect, useRef } from "react";
import type { StreamState } from "@/hooks/useSSE";
import type { PushOptions } from "@/stores/toastStore";

/**
 * Runs the warning-toast side-effect in response to `warnings` growing.
 *
 * No return value — pure side-effect hook.
 */
export function useSSEWarningToasts(
  warnings: StreamState["warnings"],
  push: (opts: PushOptions) => string,
): void {
  // Surface non-terminal `warning` SSE frames (e.g. the budget gate
  // trimming integrations for context) as toasts. `warnings` resets to []
  // on every start(); a ref tracks how
  // many entries have already been surfaced so re-renders and unrelated
  // effect re-runs never double-toast the same warning.
  const surfacedWarningsRef = useRef(0);
  useEffect(() => {
    if (warnings.length < surfacedWarningsRef.current) {
      // New stream started — the array was reset.
      surfacedWarningsRef.current = 0;
    }
    for (let i = surfacedWarningsRef.current; i < warnings.length; i++) {
      const w = warnings[i];
      if (w !== undefined) {
        push({ variant: "warning", message: w.message });
      }
    }
    surfacedWarningsRef.current = warnings.length;
  }, [warnings, push]);
}
