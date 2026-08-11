/* SPDX-License-Identifier: Apache-2.0 */
/**
 * ProjectsSection — sidebar block listing the admin's projects.
 *
 * Sits ABOVE the existing folder + chat tree — projects sit above folders.
 *
 * The block shows:
 *  - A "Projects" label + "+ New" button (opens an anchored floating
 *    create-project popover — does NOT reflow the list below it)
 *  - One row per project: name + chat-count chip
 *  - Click a row → navigate to ``/project/:id``
 *
 * Empty state is the bare label + "+ New" button. The component is
 * self-contained — no Sidebar.tsx props beyond what it reads from
 * react-router + the auth store via the hook.
 */
import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type SubmitEvent,
} from "react";
import { Link, useNavigate } from "react-router-dom";
import { FolderKanban, Plus } from "lucide-react";
import {
  useProjects,
  useCreateProject,
  type ProjectResponse,
} from "@/hooks/useProjects";
import { useToast } from "@/stores/toastStore";

interface ProjectsSectionProps {
  /** When the parent Sidebar is collapsed, hide labels and shrink rows. */
  collapsed: boolean;
  /**
   * Optional callback fired when the user clicks the "Projects" label.
   * When set, the label becomes a button that lets the Sidebar swap to a
   * full-pane projects view (the inline tree was too packed; needed a
   * replacement-menu pattern).
   */
  onOpenAllProjects?: () => void;
}

export function ProjectsSection({
  collapsed,
  onOpenAllProjects,
}: ProjectsSectionProps) {
  const navigate = useNavigate();
  const {
    data: projects,
    isLoading,
    isError,
    refetch: refetchProjects,
  } = useProjects();
  const createProject = useCreateProject();
  const { push } = useToast();
  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState("");
  const newBtnRef = useRef<HTMLButtonElement>(null);
  const createPopoverWrapRef = useRef<HTMLDivElement>(null);

  // Outside-click closes the popover (mirrors MoveToProjectMenu /
  // OverflowMenu — an anchored floating panel, not part of layout flow).
  useEffect(() => {
    if (!createOpen) return;
    function onDocClick(e: MouseEvent): void {
      if (
        createPopoverWrapRef.current &&
        !createPopoverWrapRef.current.contains(e.target as Node)
      ) {
        setCreateOpen(false);
        setCreateName("");
        newBtnRef.current?.focus();
      }
    }
    document.addEventListener("mousedown", onDocClick);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
    };
  }, [createOpen]);

  // Escape closes the popover and returns focus to the trigger.
  useEffect(() => {
    if (!createOpen) return;
    function onEsc(e: KeyboardEvent): void {
      if (e.key === "Escape") {
        setCreateOpen(false);
        setCreateName("");
        newBtnRef.current?.focus();
      }
    }
    document.addEventListener("keydown", onEsc);
    return () => {
      document.removeEventListener("keydown", onEsc);
    };
  }, [createOpen]);

  async function handleCreate(e: SubmitEvent<HTMLFormElement>): Promise<void> {
    e.preventDefault();
    const name = createName.trim();
    if (name === "") {
      push({ variant: "error", message: "Project name is required." });
      return;
    }
    try {
      const created = await createProject.mutateAsync({ name });
      setCreateName("");
      setCreateOpen(false);
      void navigate(`/project/${String(created.id)}`);
    } catch (err) {
      const detail =
        err instanceof Error
          ? err.message
          : "Couldn't create that project — try again.";
      push({ variant: "error", message: detail });
    }
  }

  return (
    <section
      className="lmchat-projects-section"
      aria-label="Projects"
      data-testid="sidebar-projects-section"
    >
      <header className="lmchat-projects-header" style={headerStyle}>
        {!collapsed &&
          (onOpenAllProjects !== undefined ? (
            <button
              type="button"
              onClick={onOpenAllProjects}
              className="lmchat-projects-label lmchat-projects-label--button"
              style={labelButtonStyle}
              data-testid="sidebar-projects-label-button"
              aria-label="Open Projects view"
              title="Open Projects view"
            >
              <span>Projects</span>
              <span className="lmchat-chevron" aria-hidden>
                ›
              </span>
            </button>
          ) : (
            <span className="lmchat-projects-label" style={labelStyle}>
              Projects
            </span>
          ))}
        <div
          ref={createPopoverWrapRef}
          style={triggerWrapStyle(collapsed)}
          className="lmchat-projects-new-wrap"
        >
          <button
            ref={newBtnRef}
            type="button"
            aria-label="New project"
            aria-haspopup="dialog"
            aria-expanded={createOpen}
            title="New project"
            onClick={() => {
              setCreateOpen((v) => !v);
            }}
            className="lmchat-projects-new-btn"
            style={newBtnStyle}
            data-testid="sidebar-projects-new-btn"
          >
            <Plus size={16} aria-hidden />
          </button>
          {createOpen && !collapsed && (
            <div
              role="dialog"
              aria-label="Create project"
              style={createPopoverStyle}
              className="lmchat-projects-create-popover"
              data-testid="sidebar-projects-create-popover"
            >
              <form
                onSubmit={(e) => {
                  void handleCreate(e);
                }}
                className="lmchat-projects-create-form"
                style={createFormStyle}
              >
                <input
                  type="text"
                  value={createName}
                  onChange={(e) => {
                    setCreateName(e.target.value);
                  }}
                  placeholder="Project name"
                  autoFocus
                  maxLength={256}
                  className="lmchat-projects-create-input"
                  style={createInputStyle}
                  data-testid="sidebar-projects-create-input"
                />
                <button
                  type="submit"
                  disabled={createProject.isPending}
                  className="lmchat-projects-create-submit"
                  style={createSubmitStyle}
                  data-testid="sidebar-projects-create-submit"
                >
                  {createProject.isPending ? "…" : "Create"}
                </button>
              </form>
            </div>
          )}
        </div>
      </header>

      <ul
        className="lmchat-projects-list"
        style={listStyle}
        data-testid="sidebar-projects-list"
      >
        {isLoading && (
          <li className="lmchat-projects-empty" style={emptyStyle}>
            Loading…
          </li>
        )}
        {isError && (
          <li
            className="lmchat-projects-empty"
            style={emptyStyle}
            role="status"
            data-testid="sidebar-projects-error"
          >
            <span>Couldn't load projects.</span>{" "}
            <button
              type="button"
              onClick={() => {
                void refetchProjects();
              }}
              className="lmchat-text-link"
              style={retryBtnStyle}
              data-testid="sidebar-projects-retry"
            >
              Retry
            </button>
          </li>
        )}
        {projects?.length === 0 && !collapsed && (
          <li className="lmchat-projects-empty" style={emptyStyle}>
            No projects yet.
          </li>
        )}
        {!collapsed &&
          projects?.map((p) => (
            <ProjectRow key={p.id} project={p} collapsed={collapsed} />
          ))}
      </ul>
    </section>
  );
}

interface ProjectRowProps {
  project: ProjectResponse;
  collapsed: boolean;
}

function ProjectRow({ project, collapsed }: ProjectRowProps) {
  return (
    <li
      className="lmchat-projects-row"
      data-testid={`sidebar-project-row-${String(project.id)}`}
    >
      <Link
        to={`/project/${String(project.id)}`}
        className="lmchat-projects-row-link"
        style={rowLinkStyle(collapsed)}
        title={project.description || project.name}
      >
        <FolderKanban size={16} aria-hidden />
        {!collapsed && (
          <span className="lmchat-projects-row-name" style={rowNameStyle}>
            {project.name}
          </span>
        )}
      </Link>
    </li>
  );
}

// ─── Local styles ────────────────────────────────────────────────────────────
//
// Inline styles intentionally — this block does NOT touch the global
// stylesheet to minimize blast radius. A follow-up styling pass can promote
// these to `lmchat-projects-*` classes in `web/src/styles/sidebar.css`.

const headerStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  padding: "8px 12px 4px 12px",
};

const labelStyle: CSSProperties = {
  textTransform: "uppercase",
  fontSize: "11px",
  letterSpacing: "0.04em",
  color: "var(--color-text-muted)",
};

const labelButtonStyle: CSSProperties = {
  ...labelStyle,
  display: "inline-flex",
  alignItems: "center",
  gap: "4px",
  background: "transparent",
  border: "none",
  padding: 0,
  cursor: "pointer",
  font: "inherit",
  textTransform: "uppercase",
  fontSize: "11px",
  letterSpacing: "0.04em",
  color: "var(--color-text-muted)",
};

const newBtnStyle: CSSProperties = {
  background: "transparent",
  border: "none",
  color: "var(--color-text-muted)",
  cursor: "pointer",
  padding: "2px",
  borderRadius: "4px",
  display: "inline-flex",
  alignItems: "center",
  justifyContent: "center",
};

// Wraps the trigger button + its popover. `position: relative` anchors the
// absolutely-positioned popover to this element so it floats above the
// sidebar without pushing the project list (or anything else) down —
// mirrors MoveToProjectMenu's `rootStyle` / OverflowMenu's `wrapStyle`.
function triggerWrapStyle(collapsed: boolean): CSSProperties {
  return {
    position: "relative",
    display: "inline-flex",
    marginInlineStart: collapsed ? 0 : "auto",
  };
}

// Anchored floating popover — same recipe as MoveToProjectMenu's
// `menuStyle` / OverflowMenu's `panelStyle`: absolute + anchored to the
// trigger's right edge, elevated surface + border + radius + shadow tokens.
const createPopoverStyle: CSSProperties = {
  position: "absolute",
  right: 0,
  top: "100%",
  marginTop: "var(--space-glue)",
  minWidth: 220,
  background: "var(--color-surface-elevated)",
  border: "1px solid var(--color-border-default)",
  borderRadius: "var(--radius-md)",
  boxShadow: "var(--shadow-md)",
  zIndex: 1000,
  padding: "var(--space-sibling-relaxed)",
};

const createFormStyle: CSSProperties = {
  display: "flex",
  gap: "var(--space-glue)",
};

const createInputStyle: CSSProperties = {
  flex: 1,
  padding: "4px 8px",
  fontSize: "12px",
  borderRadius: "4px",
  border: "1px solid var(--color-border)",
  background: "var(--color-surface)",
  color: "var(--color-text)",
};

const createSubmitStyle: CSSProperties = {
  padding: "4px 10px",
  fontSize: "12px",
  borderRadius: "4px",
  border: "1px solid var(--color-border)",
  background: "var(--color-surface-elevated)",
  color: "var(--color-text)",
  cursor: "pointer",
};

const listStyle: CSSProperties = {
  listStyle: "none",
  margin: 0,
  padding: "0 4px 4px 4px",
};

const emptyStyle: CSSProperties = {
  fontSize: "12px",
  color: "var(--color-text-muted)",
  padding: "4px 12px",
  fontStyle: "italic",
};

function rowLinkStyle(collapsed: boolean): CSSProperties {
  return {
    display: "flex",
    alignItems: "center",
    gap: "8px",
    padding: collapsed ? "6px 0" : "6px 8px",
    justifyContent: collapsed ? "center" : "flex-start",
    borderRadius: "4px",
    color: "var(--color-text)",
    textDecoration: "none",
    fontSize: "13px",
  };
}

const rowNameStyle: CSSProperties = {
  flex: 1,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const retryBtnStyle: CSSProperties = {
  background: "transparent",
  border: "1px solid var(--color-border)",
  borderRadius: 4,
  color: "var(--color-text)",
  fontSize: 11,
  padding: "1px 6px",
  cursor: "pointer",
  marginInlineStart: 6,
};
