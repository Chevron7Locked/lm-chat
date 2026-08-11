/* SPDX-License-Identifier: Apache-2.0 */
/**
 * OverflowMenu — a "⋯" trigger that opens a small dropdown of actions.
 *
 * Used to collapse the chat-lifecycle actions (Fork / Pin / Pins /
 * Delete) out of the TopBar's flat 10-button row. Those are infrequent
 * actions that shouldn't sit first-class next to the model selector and
 * panel toggles (TopBar nav-explosion / Miller's law).
 */
import { useEffect, useRef, useState } from "react";
import type { CSSProperties, ReactNode } from "react";
import { MoreHorizontal, Check } from "lucide-react";

interface OverflowAction {
  label: string;
  onClick: () => void;
  /** Renders in the accent color + checkmark when true. */
  active?: boolean;
  /** Renders in the danger color. */
  danger?: boolean;
  icon?: ReactNode;
}

interface OverflowMenuProps {
  actions: OverflowAction[];
  ariaLabel?: string;
}

export function OverflowMenu({
  actions,
  ariaLabel = "More chat actions",
}: OverflowMenuProps) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDocClick(e: MouseEvent): void {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onEsc(e: KeyboardEvent): void {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onEsc);
    };
  }, [open]);

  return (
    <div ref={wrapRef} style={wrapStyle}>
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={ariaLabel}
        onClick={() => {
          setOpen((v) => !v);
        }}
        style={triggerStyle}
        data-testid="topbar-overflow-trigger"
      >
        <MoreHorizontal size={16} aria-hidden />
      </button>
      {open && (
        <div role="menu" style={panelStyle} data-testid="topbar-overflow-panel">
          {actions.map((action) => (
            <button
              key={action.label}
              type="button"
              role="menuitem"
              onClick={() => {
                action.onClick();
                setOpen(false);
              }}
              style={itemStyle(action.danger ?? false, action.active ?? false)}
            >
              {action.icon}
              <span>{action.label}</span>
              {action.active === true && (
                <span
                  aria-hidden
                  style={{ marginLeft: "auto", display: "inline-flex" }}
                >
                  <Check size={14} aria-hidden />
                </span>
              )}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

const wrapStyle: CSSProperties = {
  position: "relative",
  display: "inline-flex",
};

const triggerStyle: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  padding: "var(--padding-btn-y) var(--space-glue)",
  // Fix 3: align to 38px canonical topbar height (matches model-select +
  // lmchat-topbar-btn). WCAG 2.5.5 satisfied (38 > 24px minimum).
  minHeight: "38px",
  minWidth: "38px",
  background: "none",
  border: "1px solid var(--color-border)",
  borderRadius: "var(--radius-xs)",
  color: "var(--color-text-muted)",
  cursor: "pointer",
};

const panelStyle: CSSProperties = {
  position: "absolute",
  right: 0,
  top: "calc(100% + 4px)",
  minWidth: 160,
  background: "var(--color-surface)",
  border: "1px solid var(--color-border)",
  borderRadius: "var(--radius-sm)",
  boxShadow: "var(--shadow-md)",
  zIndex: 200,
  padding: "var(--spacing-2xs) 0",
  display: "flex",
  flexDirection: "column",
};

function itemStyle(danger: boolean, active: boolean): CSSProperties {
  return {
    display: "flex",
    alignItems: "center",
    gap: "var(--space-glue)",
    textAlign: "left",
    background: "none",
    border: "none",
    padding: "var(--space-glue) var(--space-sibling-relaxed)",
    fontSize: "var(--fs-label)",
    color: danger
      ? "var(--color-danger)"
      : active
        ? "var(--color-accent)"
        : "var(--color-text)",
    cursor: "pointer",
  };
}
