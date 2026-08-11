/* SPDX-License-Identifier: Apache-2.0 */
/**
 * MoveToFolderMenu — kebab-style trigger + dropdown for moving a chat into
 * a folder (or removing it from its current folder).
 *
 * BUG 4 fix: folders were creatable in the sidebar but drag-and-drop was
 * the ONLY way to move a chat into one, and that path had structural gaps
 * (per-folder DndContext boundaries meant cross-folder drags could never
 * resolve; an empty folder had zero sortable items to aim a drag at — see
 * Sidebar.tsx). This menu is the reliable, keyboard/mobile-accessible
 * fallback that always works regardless of any DnD edge case. Mirrors
 * MoveToProjectMenu.tsx's popover pattern (kebab trigger, outside-click +
 * Escape to close, hide-when-nothing-to-do).
 *
 * The component is mutation-agnostic — the caller passes an ``onPick``
 * callback that receives the chosen folder name, or ``null`` for "Remove
 * from folder". The caller fires the actual reorder mutation.
 */
import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent,
} from "react";
import { Folder, Check } from "lucide-react";
import { useFolders } from "@/hooks/useFolders";

interface MoveToFolderMenuProps {
  /** Current folder of the chat (null = unfoldered). Used to decide
   *  whether "Remove from folder" is shown and to mark the active
   *  folder in the list. */
  currentFolder: string | null;
  /** Called with the picked folder name, or null for "Remove from
   *  folder". The caller fires the actual reorder mutation. */
  onPick: (folder: string | null) => void;
  /** Optional test-id prefix so the same component can appear multiple
   *  times in one page without colliding. */
  testIdPrefix?: string;
  /** Optional label override for the trigger button. Defaults to
   *  "Move to folder". */
  ariaLabel?: string;
}

export function MoveToFolderMenu({
  currentFolder,
  onPick,
  testIdPrefix = "move-to-folder",
  ariaLabel = "Move to folder",
}: MoveToFolderMenuProps) {
  const { data: folders } = useFolders();
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  // Outside-click closes the menu.
  useEffect(() => {
    if (!open) return;
    function onDocClick(e: globalThis.MouseEvent): void {
      if (
        rootRef.current !== null &&
        e.target instanceof Node &&
        !rootRef.current.contains(e.target)
      ) {
        setOpen(false);
      }
    }
    document.addEventListener("mousedown", onDocClick);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
    };
  }, [open]);

  // Escape closes the menu.
  useEffect(() => {
    if (!open) return;
    function onKey(e: globalThis.KeyboardEvent): void {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  function handleTriggerClick(e: MouseEvent): void {
    e.preventDefault();
    e.stopPropagation();
    setOpen((v) => !v);
  }

  function handlePick(folder: string | null): void {
    setOpen(false);
    onPick(folder);
  }

  // Hide the affordance entirely when there's literally nothing to do —
  // no folders exist AND the chat isn't currently in one. Mirrors
  // MoveToProjectMenu's ``noProjectsToMoveTo`` guard.
  // IMPORTANT: this early return must sit BELOW all hook calls (above
  // useEffects → Rules of Hooks violation otherwise).
  const noFoldersToMoveTo = folders?.length === 0;
  if (noFoldersToMoveTo && currentFolder === null) {
    return null;
  }

  return (
    <div ref={rootRef} style={rootStyle} className="lmchat-move-to-folder">
      <button
        type="button"
        aria-label={ariaLabel}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={handleTriggerClick}
        style={triggerStyle}
        data-testid={`${testIdPrefix}-trigger`}
        title={ariaLabel}
      >
        <Folder size={14} aria-hidden />
      </button>
      {open && (
        <div
          role="menu"
          style={menuStyle}
          data-testid={`${testIdPrefix}-menu`}
          onClick={(e) => {
            e.stopPropagation();
          }}
        >
          {currentFolder !== null && (
            <button
              role="menuitem"
              type="button"
              onClick={() => {
                handlePick(null);
              }}
              style={itemStyle}
              data-testid={`${testIdPrefix}-remove`}
            >
              Remove from folder
            </button>
          )}
          {folders === undefined && <div style={emptyStyle}>Loading…</div>}
          {folders?.length === 0 && (
            <div style={emptyStyle}>No folders yet.</div>
          )}
          {folders?.map((name) => (
            <button
              key={name}
              role="menuitem"
              type="button"
              onClick={() => {
                handlePick(name);
              }}
              style={{
                ...itemStyle,
                fontWeight: name === currentFolder ? 600 : 400,
              }}
              data-testid={`${testIdPrefix}-pick-${name}`}
            >
              {name === currentFolder && (
                <Check
                  size={12}
                  aria-hidden
                  style={{ marginRight: "4px", flexShrink: 0 }}
                />
              )}
              {name}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

// ─── Local styles ────────────────────────────────────────────────────────────

const rootStyle: CSSProperties = {
  position: "relative",
  display: "inline-block",
};

const triggerStyle: CSSProperties = {
  background: "transparent",
  border: "none",
  color: "var(--color-text-muted)",
  cursor: "pointer",
  // Spacing grammar — glue token.
  padding: "var(--space-glue)",
  borderRadius: "var(--radius-sm)",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
  // WCAG 2.2 SC 2.5.8 — minimum 24×24px interactive target.
  minHeight: "24px",
  minWidth: "24px",
};

const menuStyle: CSSProperties = {
  position: "absolute",
  right: 0,
  top: "100%",
  marginTop: "var(--space-glue)",
  minWidth: 180,
  maxHeight: 240,
  overflowY: "auto",
  background: "var(--color-surface-elevated)",
  border: "1px solid var(--color-border-default)",
  borderRadius: "var(--radius-sm)",
  boxShadow: "var(--shadow-md)",
  zIndex: 1000,
  padding: "var(--space-glue)",
  display: "flex",
  flexDirection: "column",
  gap: "var(--space-glue)",
};

const itemStyle: CSSProperties = {
  background: "transparent",
  border: "none",
  textAlign: "left",
  padding: "6px 10px",
  borderRadius: 4,
  cursor: "pointer",
  color: "var(--color-text)",
  fontSize: 13,
  display: "flex",
  alignItems: "center",
};

const emptyStyle: CSSProperties = {
  padding: "6px 10px",
  fontSize: 12,
  color: "var(--color-text-muted)",
  fontStyle: "italic",
};
