/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Toast store — Zustand 5.
 *
 * Stateful toast queue. Toasts stack in the bottom-right so they don't
 * overlap the chat-input footer. Auto-dismiss after the
 * configured duration (default 5 000ms). Each toast has a unique id generated
 * client-side.
 *
 * Usage:
 *   const { push } = useToastStore.getState();
 *   push({ variant: "success", message: "Saved!" });
 */
import { create } from "zustand";

export type ToastVariant = "info" | "success" | "warning" | "error";

interface ToastAction {
  label: string;
  onClick: () => void;
}

export interface Toast {
  /** Unique identifier for this toast. */
  id: string;
  variant: ToastVariant;
  /** Optional short title rendered above the message (use for high-stakes errors). */
  title?: string | undefined;
  message: string;
  /** Optional inline action button (e.g. Retry, Undo). */
  action?: ToastAction | undefined;
  /** Duration in milliseconds before auto-dismiss. 0 = never auto-dismiss. */
  duration: number;
  /**
   * How many times this same toast has fired. Repeated identical pushes
   * (same variant + title + message, no action) coalesce onto the existing
   * toast and increment this count instead of stacking N copies. 1 = shown
   * once; the renderer shows a `×N` badge only when count > 1.
   */
  count: number;
}

export interface PushOptions {
  variant: ToastVariant;
  /** Optional short title rendered above the message (use for high-stakes errors). */
  title?: string | undefined;
  message: string;
  /** Optional inline action button (e.g. Retry, Undo). */
  action?: ToastAction | undefined;
  /** Override the auto-dismiss duration (default 5 000ms). */
  duration?: number | undefined;
}

interface ToastState {
  toasts: Toast[];
  push: (opts: PushOptions) => string;
  dismiss: (id: string) => void;
  clear: () => void;
}

let _idCounter = 0;

function genId(): string {
  _idCounter += 1;
  return `toast-${String(_idCounter)}`;
}

/**
 * Maximum number of toasts rendered on the stack at once.
 *
 * Toasts are transient by nature — a tall, unbounded stack buries page
 * content (acutely so on mobile, where the stack spans the column) and
 * reads as noise rather than feedback. When a `push()` would exceed this
 * cap, the OLDEST toast is evicted so the stack never grows past it. The
 * newest toast is always shown.
 */
export const MAX_TOASTS = 3;

export const useToastStore = create<ToastState>((set) => ({
  toasts: [],

  push: (opts: PushOptions): string => {
    // Coalesce a repeated identical toast onto the most recent matching one
    // rather than stacking a fresh copy. Only actionless toasts coalesce —
    // a toast carrying an action button is treated as distinct so its action
    // is never silently dropped. Returns the existing id on a coalesce.
    const existing = useToastStore
      .getState()
      .toasts.find(
        (t) =>
          t.action === undefined &&
          opts.action === undefined &&
          t.variant === opts.variant &&
          t.title === opts.title &&
          t.message === opts.message,
      );
    if (existing !== undefined) {
      set((s) => ({
        toasts: s.toasts.map((t) =>
          t.id === existing.id ? { ...t, count: t.count + 1 } : t,
        ),
      }));
      return existing.id;
    }

    const id = genId();
    // Error toasts are sticky by default (dismiss-only).
    // Any other variant defaults to 5 000 ms. Callers can still override via
    // the explicit `duration` field.
    const defaultDuration = opts.variant === "error" ? 0 : 5_000;
    const toast: Toast = {
      id,
      variant: opts.variant,
      title: opts.title,
      message: opts.message,
      action: opts.action,
      duration: opts.duration ?? defaultDuration,
      count: 1,
    };
    // Cap the stack: when appending would exceed MAX_TOASTS, evict from the
    // FRONT (oldest) so the newest toast is always shown and the stack never
    // grows past the cap.
    set((s) => ({
      toasts: [...s.toasts, toast].slice(-MAX_TOASTS),
    }));

    if (toast.duration > 0) {
      setTimeout(() => {
        set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }));
      }, toast.duration);
    }

    return id;
  },

  dismiss: (id: string) => {
    set((s) => ({ toasts: s.toasts.filter((t) => t.id !== id) }));
  },

  clear: () => {
    set({ toasts: [] });
  },
}));

/** Convenience hook for components. */
export function useToast(): {
  push: (opts: PushOptions) => string;
  dismiss: (id: string) => void;
} {
  const push = useToastStore((s) => s.push);
  const dismiss = useToastStore((s) => s.dismiss);
  return { push, dismiss };
}
