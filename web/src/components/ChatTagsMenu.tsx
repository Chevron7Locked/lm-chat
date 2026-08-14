/* SPDX-License-Identifier: Apache-2.0 */
/**
 * ChatTagsMenu — kebab-style trigger + dropdown for viewing/adding/removing
 * free-form tags on a chat.
 *
 * Mirrors MoveToFolderMenu.tsx's popover pattern (kebab trigger,
 * outside-click + Escape to close). Mutation-agnostic — the caller passes
 * an `onChange` callback that receives the FULL new tag list; the caller
 * fires the actual PATCH mutation (chats.tags is replaced wholesale server
 * side, so add/remove both go through the same round-trip).
 */
import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type MouseEvent,
  type SubmitEvent,
} from "react";
import { Tag } from "lucide-react";

interface ChatTagsMenuProps {
  /** Current tags on the chat. */
  tags: string[];
  /** Called with the full new tag list after an add or remove. */
  onChange: (tags: string[]) => void;
  /** Optional test-id prefix so the same component can appear multiple
   *  times in one page without colliding. */
  testIdPrefix?: string;
  /** Optional label override for the trigger button. */
  ariaLabel?: string;
}

export function ChatTagsMenu({
  tags,
  onChange,
  testIdPrefix = "chat-tags",
  ariaLabel = "Edit tags",
}: ChatTagsMenuProps) {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState("");
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

  function handleAdd(e: SubmitEvent<HTMLFormElement>): void {
    e.preventDefault();
    const trimmed = draft.trim();
    setDraft("");
    if (trimmed === "" || tags.includes(trimmed)) return;
    onChange([...tags, trimmed]);
  }

  function handleRemove(tag: string): void {
    onChange(tags.filter((t) => t !== tag));
  }

  return (
    <div ref={rootRef} style={rootStyle} className="lmchat-chat-tags-menu">
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
        <Tag size={14} aria-hidden />
        {tags.length > 0 && (
          <span style={countBadgeStyle} data-testid={`${testIdPrefix}-count`}>
            {tags.length}
          </span>
        )}
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
          {tags.length === 0 && <div style={emptyStyle}>No tags yet.</div>}
          {tags.length > 0 && (
            <div style={chipsRowStyle}>
              {tags.map((tag) => (
                <span
                  key={tag}
                  style={chipStyle}
                  data-testid={`${testIdPrefix}-chip-${tag}`}
                >
                  {tag}
                  <button
                    type="button"
                    aria-label={`Remove tag ${tag}`}
                    onClick={() => {
                      handleRemove(tag);
                    }}
                    style={chipRemoveStyle}
                    data-testid={`${testIdPrefix}-remove-${tag}`}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}
          <form onSubmit={handleAdd} style={addFormStyle}>
            <input
              type="text"
              value={draft}
              onChange={(e) => {
                setDraft(e.target.value);
              }}
              placeholder="Add tag…"
              aria-label="New tag name"
              style={addInputStyle}
              data-testid={`${testIdPrefix}-input`}
              maxLength={32}
            />
            <button
              type="submit"
              style={addSubmitStyle}
              data-testid={`${testIdPrefix}-submit`}
            >
              Add
            </button>
          </form>
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
  padding: "var(--space-glue)",
  borderRadius: "var(--radius-sm)",
  display: "inline-flex",
  alignItems: "center",
  gap: 2,
  justifyContent: "center",
  // WCAG 2.2 SC 2.5.8 — minimum 24×24px interactive target.
  minHeight: "24px",
  minWidth: "24px",
};

const countBadgeStyle: CSSProperties = {
  fontSize: 9,
  lineHeight: 1,
  fontWeight: 700,
};

const menuStyle: CSSProperties = {
  position: "absolute",
  right: 0,
  top: "100%",
  marginTop: "var(--space-glue)",
  minWidth: 200,
  maxHeight: 260,
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

const emptyStyle: CSSProperties = {
  padding: "6px 10px",
  fontSize: 12,
  color: "var(--color-text-muted)",
  fontStyle: "italic",
};

const chipsRowStyle: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  gap: 4,
  padding: "2px 4px",
};

const chipStyle: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: 4,
  padding: "2px 6px",
  borderRadius: "var(--radius-sm)",
  background: "var(--color-surface)",
  border: "1px solid var(--color-border)",
  color: "var(--color-text)",
  fontSize: 11,
};

const chipRemoveStyle: CSSProperties = {
  background: "transparent",
  border: "none",
  cursor: "pointer",
  color: "var(--color-text-muted)",
  fontSize: 12,
  lineHeight: 1,
  padding: 0,
};

const addFormStyle: CSSProperties = {
  display: "flex",
  gap: 4,
  padding: "2px 2px 0 2px",
};

const addInputStyle: CSSProperties = {
  flex: 1,
  minWidth: 0,
  padding: "4px 8px",
  fontSize: 12,
  borderRadius: "var(--radius-sm)",
  border: "1px solid var(--color-border)",
  background: "var(--color-surface)",
  color: "var(--color-text)",
};

const addSubmitStyle: CSSProperties = {
  padding: "4px 10px",
  fontSize: 12,
  borderRadius: "var(--radius-sm)",
  border: "1px solid var(--color-border-default)",
  background: "var(--color-surface-elevated)",
  color: "var(--color-text)",
  cursor: "pointer",
};
