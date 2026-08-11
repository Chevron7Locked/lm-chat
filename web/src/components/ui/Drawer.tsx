/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Drawer — accessible side-anchored overlay panel.
 *
 * Behaviour:
 *  - Slides in from `side` (left | right) over a semi-transparent backdrop.
 *  - Backdrop click, Escape, and the header close button all invoke
 *    `onClose`.
 *  - Focus moves to the panel on open and is trapped inside while open.
 *  - Last-focused element prior to open is restored on close.
 *  - role="dialog" + aria-modal="true" + aria-labelledby pointing at the
 *    title element for AT semantics.
 *
 * Animation: a 200ms ease-out slide; the panel transforms from translateX
 * full-width-offset to 0 the frame after mount.  Reduced-motion users get
 * an instantaneous open: `matchMedia("(prefers-reduced-motion: reduce)")`
 * is checked in JS (Polish R1, F12) — the entry state starts entered and
 * the transform transition is dropped, so no slide ever runs.
 */
import {
  useCallback,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
} from "react";
import type {
  CSSProperties,
  KeyboardEvent as ReactKeyboardEvent,
  ReactNode,
} from "react";
import { X } from "lucide-react";

export interface DrawerProps {
  isOpen: boolean;
  onClose: () => void;
  side: "left" | "right";
  title: string;
  children: ReactNode;
  /** Optional fixed width (default 360px). */
  width?: number | string;
  /** Render a "Done" footer button bottom-right that calls onClose. */
  showFooter?: boolean;
  /** Optional test-id for the outer dialog node. */
  "data-testid"?: string;
}

/** Polish R1 (F12): reduced-motion users skip the entry slide entirely. */
function prefersReducedMotion(): boolean {
  return (
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

const FOCUSABLE_SELECTOR = [
  "a[href]",
  "button:not([disabled])",
  "textarea:not([disabled])",
  "input:not([disabled])",
  "select:not([disabled])",
  "[tabindex]:not([tabindex='-1'])",
].join(",");

export function Drawer({
  isOpen,
  onClose,
  side,
  title,
  children,
  width = 360,
  showFooter = false,
  "data-testid": testId,
}: DrawerProps) {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement>(null);
  const previouslyFocusedRef = useRef<HTMLElement | null>(null);
  // `entered` flips to true the frame after mount so the CSS transition
  // fires. Reduced-motion users start entered — no slide, instant open.
  const [entered, setEntered] = useState(() => prefersReducedMotion());

  // Capture previously-focused element before the panel mounts.
  useLayoutEffect(() => {
    if (isOpen) {
      previouslyFocusedRef.current =
        document.activeElement instanceof HTMLElement
          ? document.activeElement
          : null;
    }
  }, [isOpen]);

  // Trigger slide-in on open; reset for next open on close.
  useEffect(() => {
    if (!isOpen) {
      setEntered(false);
      return;
    }
    // Reduced motion: enter immediately (no rAF delay, no slide).
    if (prefersReducedMotion()) {
      setEntered(true);
      return;
    }
    const raf = requestAnimationFrame(() => {
      setEntered(true);
    });
    return () => {
      cancelAnimationFrame(raf);
    };
  }, [isOpen]);

  // Move focus into the panel on open; restore on close.
  useEffect(() => {
    if (!isOpen) return;
    const panel = panelRef.current;
    if (panel === null) return;
    const focusables = panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR);
    const first = focusables[0] ?? panel;
    first.focus({ preventScroll: true });
    return () => {
      const prev = previouslyFocusedRef.current;
      // Guard against the previously-focused element being unmounted.
      if (prev !== null && document.body.contains(prev)) {
        prev.focus({ preventScroll: true });
      }
    };
  }, [isOpen]);

  // Escape closes; Tab/Shift+Tab traps focus within the panel.
  const handleKeyDown = useCallback(
    (e: ReactKeyboardEvent<HTMLDivElement>) => {
      if (e.key === "Escape") {
        e.preventDefault();
        e.stopPropagation();
        onClose();
        return;
      }
      if (e.key !== "Tab") return;
      const panel = panelRef.current;
      if (panel === null) return;
      const focusables = Array.from(
        panel.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR),
      ).filter(
        (el) => el.offsetParent !== null || el === document.activeElement,
      );
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      if (first === undefined || last === undefined) {
        e.preventDefault();
        panel.focus();
        return;
      }
      const active = document.activeElement;
      if (e.shiftKey && active === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && active === last) {
        e.preventDefault();
        first.focus();
      }
    },
    [onClose],
  );

  if (!isOpen) return null;

  const panelTransform = entered
    ? "translateX(0)"
    : side === "right"
      ? "translateX(100%)"
      : "translateX(-100%)";

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      data-testid={testId}
      onKeyDown={handleKeyDown}
      style={backdropStyle}
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={panelRef}
        tabIndex={-1}
        style={{
          ...panelBaseStyle,
          [side]: 0,
          width: typeof width === "number" ? `${String(width)}px` : width,
          transform: panelTransform,
          // Belt-and-braces: even if `entered` toggles, nothing animates.
          ...(prefersReducedMotion() ? { transition: "none" } : {}),
        }}
      >
        <header style={headerStyle}>
          <h2 id={titleId} style={titleStyle}>
            {title}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label={`Close ${title}`}
            style={closeBtnStyle}
            data-testid="drawer-close"
          >
            <X size={14} aria-hidden />
          </button>
        </header>
        <div style={bodyStyle}>{children}</div>
        {showFooter && (
          <footer style={footerStyle}>
            <button
              type="button"
              onClick={onClose}
              style={doneBtnStyle}
              data-testid="drawer-done"
            >
              Done
            </button>
          </footer>
        )}
      </div>
    </div>
  );
}

const backdropStyle: CSSProperties = {
  position: "fixed",
  inset: 0,
  background: "var(--color-scrim-xl)",
  zIndex: 100,
  display: "flex",
};

const panelBaseStyle: CSSProperties = {
  position: "absolute",
  top: 0,
  bottom: 0,
  display: "flex",
  flexDirection: "column",
  background: "var(--color-surface)",
  borderLeft: "1px solid var(--color-border-default)",
  borderRight: "1px solid var(--color-border-default)",
  boxShadow: "var(--shadow-lg)",
  transition: "transform 200ms ease-out",
  outline: "none",
  maxWidth: "100vw",
};

const headerStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  padding: "var(--space-sibling-relaxed) var(--space-group)",
  borderBottom: "1px solid var(--color-border)",
  flexShrink: 0,
};

const titleStyle: CSSProperties = {
  margin: 0,
  fontSize: "1rem",
  fontWeight: 600,
  fontFamily: "var(--font-display)",
  color: "var(--color-text)",
};

const closeBtnStyle: CSSProperties = {
  // 32px — matches the app-wide .rr-icon-btn action-button box (was 28px,
  // the smallest interactive target in the rail).
  width: "32px",
  height: "32px",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  background: "none",
  border: "1px solid transparent",
  borderRadius: "var(--radius-sm)",
  fontSize: "0.875rem",
  color: "var(--color-text-muted)",
  cursor: "pointer",
};

const bodyStyle: CSSProperties = {
  flex: 1,
  minHeight: 0,
  overflowY: "auto",
};

const footerStyle: CSSProperties = {
  display: "flex",
  justifyContent: "flex-end",
  padding: "var(--space-sibling-relaxed) var(--space-group)",
  borderTop: "1px solid var(--color-border)",
  flexShrink: 0,
};

const doneBtnStyle: CSSProperties = {
  padding: "var(--space-glue) var(--space-group)",
  background: "var(--color-accent)",
  border: "none",
  borderRadius: "var(--radius-sm)",
  fontSize: "0.875rem",
  fontWeight: 600,
  color: "var(--color-text-on-accent)",
  cursor: "pointer",
};
