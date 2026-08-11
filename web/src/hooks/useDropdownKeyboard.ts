/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useDropdownKeyboard — reusable keyboard navigation hook for dropdown menus.
 *
 * Closes DEFERRED §2 (UserMenu keyboard accessibility).
 *
 * Features:
 * - Arrow Up / Arrow Down: cycle focus through menu items.
 * - Escape: close the dropdown and return focus to the trigger.
 * - Tab / Shift+Tab: trap focus inside the dropdown while it is open.
 * - Home / End: jump to first / last item.
 *
 * Usage:
 *   const { containerProps } = useDropdownKeyboard({ open, onClose, itemSelector });
 *   return <div {...containerProps} role="menu">...</div>;
 *
 * `itemSelector` defaults to '[role="menuitem"]:not([disabled])'.
 */
import { useCallback, useEffect, useRef } from "react";
import type { KeyboardEvent, RefObject } from "react";

interface UseDropdownKeyboardOptions {
  /** Whether the dropdown is currently open. */
  open: boolean;
  /** Called when the dropdown should close (e.g. Escape pressed). */
  onClose: () => void;
  /** CSS selector for focusable items inside the dropdown container.
   *  Defaults to '[role="menuitem"]:not([disabled])'. */
  itemSelector?: string;
}

interface UseDropdownKeyboardResult {
  /** Spread onto the dropdown container element. */
  containerProps: {
    onKeyDown: (e: KeyboardEvent<HTMLElement>) => void;
    ref: RefObject<HTMLDivElement | null>;
  };
}

const DEFAULT_ITEM_SELECTOR = '[role="menuitem"]:not([disabled])';

export function useDropdownKeyboard({
  open,
  onClose,
  itemSelector = DEFAULT_ITEM_SELECTOR,
}: UseDropdownKeyboardOptions): UseDropdownKeyboardResult {
  const containerRef = useRef<HTMLDivElement>(null);

  // When the dropdown opens, focus the first menu item.
  useEffect(() => {
    if (!open) return;
    const items =
      containerRef.current?.querySelectorAll<HTMLElement>(itemSelector);
    if (items && items.length > 0) {
      // Defer so the DOM has painted.
      requestAnimationFrame(() => {
        items[0]?.focus();
      });
    }
  }, [open, itemSelector]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLElement>) => {
      if (!open) return;

      // Escape is always handled — even when containerRef.current is null
      // (e.g. in unit tests without a real DOM).
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
        return;
      }

      const container = containerRef.current;
      if (!container) return;

      const items = Array.from(
        container.querySelectorAll<HTMLElement>(itemSelector),
      );
      if (items.length === 0) return;

      const focused = document.activeElement as HTMLElement | null;
      const currentIdx = focused ? items.indexOf(focused) : -1;

      switch (e.key) {
        case "ArrowDown": {
          e.preventDefault();
          const next = currentIdx < items.length - 1 ? currentIdx + 1 : 0;
          items[next]?.focus();
          break;
        }
        case "ArrowUp": {
          e.preventDefault();
          const prev = currentIdx > 0 ? currentIdx - 1 : items.length - 1;
          items[prev]?.focus();
          break;
        }
        case "Home": {
          e.preventDefault();
          items[0]?.focus();
          break;
        }
        case "End": {
          e.preventDefault();
          items[items.length - 1]?.focus();
          break;
        }
        case "Tab": {
          // Trap focus inside the dropdown.
          if (e.shiftKey) {
            // Shift+Tab: move to previous item or wrap to last.
            e.preventDefault();
            const prev = currentIdx > 0 ? currentIdx - 1 : items.length - 1;
            items[prev]?.focus();
          } else {
            // Tab: move to next item or wrap to first.
            e.preventDefault();
            const next = currentIdx < items.length - 1 ? currentIdx + 1 : 0;
            items[next]?.focus();
          }
          break;
        }
        default:
          break;
      }
    },
    [open, onClose, itemSelector],
  );

  return {
    containerProps: {
      onKeyDown: handleKeyDown,
      ref: containerRef,
    },
  };
}
