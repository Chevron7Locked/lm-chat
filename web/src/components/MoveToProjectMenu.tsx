/* SPDX-License-Identifier: Apache-2.0 */
/**
 * MoveToProjectMenu — kebab button + dropdown for reassigning a chat
 * or document to a project (or detaching from one).
 *
 * Phase-6 punch-list item C: ``useMoveDocumentToProject`` already ships
 * in the hooks layer; this component is the UI chrome that consumes it.
 *
 * The component is mutation-agnostic — callers pass an ``onMove``
 * callback that gets the new ``projectId`` (or ``null`` to detach).
 * That keeps the component reusable across chats and documents
 * without coupling it to either mutation hook directly.
 */
import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent,
} from "react";
import { FolderKanban, Check } from "lucide-react";
import { useProjects, type ProjectResponse } from "@/hooks/useProjects";

interface MoveToProjectMenuProps {
  /** Current project id of the entity (null = un-projected). Used to
   *  decide whether the "Detach from project" item is shown and to
   *  visually mark the active project in the list. */
  currentProjectId: number | null;
  /** Called with the new project id (or null to detach). The caller
   *  fires the actual mutation. */
  onMove: (newProjectId: number | null) => void;
  /** Optional test-id prefix so the same component can appear multiple
   *  times in one page without colliding. */
  testIdPrefix?: string;
  /** Optional label override for the trigger button. Defaults to
   *  "Move to project". */
  ariaLabel?: string;
}

export function MoveToProjectMenu({
  currentProjectId,
  onMove,
  testIdPrefix = "move-to-project",
  ariaLabel = "Move to project",
}: MoveToProjectMenuProps) {
  const { data: projects } = useProjects();
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

  function handlePick(newProjectId: number | null): void {
    setOpen(false);
    onMove(newProjectId);
  }

  // Hide the affordance entirely when there's literally nothing to do —
  // user has zero projects AND the chat isn't currently in one. Without
  // this guard, every chat row carried a folder-kanban icon that opened
  // a menu saying "No projects yet." (The new-chat sidebar row was
  // showing the folder icon and opening an empty "No projects" menu —
  // meaningless if there are no projects.)
  // While projects is still loading (undefined), keep rendering so the
  // row layout doesn't shift when the list arrives.
  // IMPORTANT: this early return must sit BELOW all hook calls (above
  // useEffects → Rules of Hooks violation: rendered fewer hooks than
  // expected). All hooks complete before any conditional return.
  const noProjectsToMoveTo = projects?.length === 0;
  if (noProjectsToMoveTo && currentProjectId === null) {
    return null;
  }

  return (
    <div ref={rootRef} style={rootStyle} className="lmchat-move-to-project">
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
        <FolderKanban size={14} aria-hidden />
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
          {currentProjectId !== null && (
            <button
              role="menuitem"
              type="button"
              onClick={() => {
                handlePick(null);
              }}
              style={itemStyle}
              data-testid={`${testIdPrefix}-detach`}
            >
              Detach from project
            </button>
          )}
          {projects === undefined && <div style={emptyStyle}>Loading…</div>}
          {projects?.length === 0 && (
            <div style={emptyStyle}>No projects yet.</div>
          )}
          {projects?.map((p: ProjectResponse) => (
            <button
              key={p.id}
              role="menuitem"
              type="button"
              onClick={() => {
                handlePick(p.id);
              }}
              style={{
                ...itemStyle,
                fontWeight: p.id === currentProjectId ? 600 : 400,
              }}
              data-testid={`${testIdPrefix}-pick-${String(p.id)}`}
            >
              {p.id === currentProjectId && (
                <Check
                  size={12}
                  aria-hidden
                  style={{ marginRight: "4px", flexShrink: 0 }}
                />
              )}
              {p.name}
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
  // Fix 4: WCAG 2.2 SC 2.5.8 — minimum 24×24px interactive target.
  minHeight: "24px",
  minWidth: "24px",
};

const menuStyle: CSSProperties = {
  position: "absolute",
  right: 0,
  top: "100%",
  // Spacing grammar tokens — glue rhythm.
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
};

const emptyStyle: CSSProperties = {
  padding: "6px 10px",
  fontSize: 12,
  color: "var(--color-text-muted)",
  fontStyle: "italic",
};
