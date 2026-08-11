/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useFocusTrap — traps keyboard focus inside a container while it is open.
 *
 * Covers WCAG 2.1.2 (No Keyboard Trap) and 4.1.2 for modal dialogs.
 *
 * Behaviour:
 *   - On open: captures the currently-focused element, then moves focus to
 *     initialFocusRef.current (if provided) or the first focusable descendant.
 *   - While open: Tab cycles forward through focusables; Shift+Tab cycles
 *     backward. Wraps at both ends. The focusable list is re-queried on each
 *     tab keypress so it stays accurate if the DOM changes mid-render.
 *   - On close (isOpen → false): restores focus to the previously-focused
 *     element (unless restoreFocus is explicitly false).
 *
 * ESC is NOT handled here — components handle their own close-on-Esc so they
 * have full control over side-effects (e.g. calling onCancel vs onClose).
 */
import { useEffect, useRef } from "react";
import type { RefObject } from "react";

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "area[href]",
  "button:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "textarea:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
  "details > summary",
].join(", ");

function getFocusables(container: HTMLElement): HTMLElement[] {
  return Array.from(
    container.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
  ).filter((el) => !el.closest("[aria-hidden='true']"));
}

export interface FocusTrapOptions {
  /** If provided, focus this element on open instead of the first focusable. */
  initialFocusRef?: RefObject<HTMLElement | null>;
  /** Whether to restore focus to the trigger element on close. Default: true. */
  restoreFocus?: boolean;
}

export function useFocusTrap(
  containerRef: RefObject<HTMLElement | null>,
  isOpen: boolean,
  options: FocusTrapOptions = {},
): void {
  const { initialFocusRef, restoreFocus = true } = options;
  // Capture the element that was focused when the overlay opened.
  const previousFocusRef = useRef<HTMLElement | null>(null);

  useEffect(() => {
    if (!isOpen) return undefined;
    const container = containerRef.current;
    if (!container) return undefined;

    // Store the trigger so we can restore on close.
    previousFocusRef.current = document.activeElement as HTMLElement | null;

    // Move focus into the trap.
    const target = initialFocusRef?.current ?? getFocusables(container)[0];
    let raf: number | null = null;
    if (target) {
      // Defer one frame so the element is fully painted before focusing.
      raf = requestAnimationFrame(() => {
        target.focus();
      });
    }

    return (): void => {
      if (raf !== null) cancelAnimationFrame(raf);
      // Restore focus on both isOpen→false transitions AND unmount-while-open.
      // Callers that hardcode isOpen=true (e.g. ConfirmDialog) only ever unmount;
      // they never see a false transition, so we must restore here, not in a
      // separate effect.
      if (restoreFocus) {
        const prev = previousFocusRef.current;
        if (
          prev &&
          typeof prev.focus === "function" &&
          document.contains(prev)
        ) {
          prev.focus();
        }
      }
    };
  }, [isOpen, restoreFocus]);

  // Tab-key cycling inside the container.
  useEffect(() => {
    if (!isOpen) return;
    const container = containerRef.current;
    if (!container) return;

    function handleKeyDown(e: KeyboardEvent): void {
      if (e.key !== "Tab") return;
      if (!container) return;

      const focusables = getFocusables(container);
      if (focusables.length === 0) {
        e.preventDefault();
        return;
      }

      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (!first || !last) return;

      if (e.shiftKey) {
        // Shift+Tab: if at the first element, wrap to last.
        if (document.activeElement === first) {
          e.preventDefault();
          last.focus();
        }
      } else {
        // Tab: if at the last element, wrap to first.
        if (document.activeElement === last) {
          e.preventDefault();
          first.focus();
        }
      }
    }

    container.addEventListener("keydown", handleKeyDown);
    return () => {
      container.removeEventListener("keydown", handleKeyDown);
    };
  }, [isOpen]);
}
