/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Projects — all-projects landing page.
 *
 * A full-pane replacement for the sidebar's compact Projects list: every
 * project the admin owns, with chat/doc counts, a "New project" action,
 * and archived projects tucked into a collapsed section
 * rather than mixed into the primary list.
 *
 * Chat/doc counts are derived client-side from the existing unscoped
 * `useChats()` / `useDocuments()` lists (same `project_id` filter
 * DocumentsTab already applies per-project) rather than a new backend
 * aggregate — cheap because both lists are already fetched elsewhere in
 * the app and TanStack Query dedupes the cache hit.
 */
import { useMemo, useState, type CSSProperties, type SubmitEvent } from "react";
import { Link, useNavigate } from "react-router-dom";
import { FileText, FolderKanban, MessageSquare, Plus } from "lucide-react";
import { AppShell } from "@/components/AppShell";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { useToast } from "@/stores/toastStore";
import {
  useProjects,
  useCreateProject,
  type ProjectResponse,
} from "@/hooks/useProjects";
import { useChats } from "@/hooks/useChats";
import { useDocuments } from "@/hooks/useDocuments";

export default function Projects() {
  useDocumentTitle("Projects");
  const navigate = useNavigate();
  const { push } = useToast();

  // include_archived=true — split into active/archived below so the
  // page never issues two separate list requests for one dataset.
  const {
    data: allProjects,
    isLoading,
    isError,
    refetch,
  } = useProjects(true);
  const { data: chatsData } = useChats();
  const { data: allDocs } = useDocuments();
  const createProject = useCreateProject();

  const [createOpen, setCreateOpen] = useState(false);
  const [createName, setCreateName] = useState("");

  const activeProjects = useMemo(
    () => (allProjects ?? []).filter((p) => p.archived_at == null),
    [allProjects],
  );
  const archivedProjects = useMemo(
    () => (allProjects ?? []).filter((p) => p.archived_at != null),
    [allProjects],
  );

  function countsFor(projectId: number): { chats: number; docs: number } {
    const chats = (chatsData?.chats ?? []).filter(
      (c) => c.project_id === projectId,
    ).length;
    const docs = (allDocs ?? []).filter(
      (d) => (d.project_id ?? null) === projectId,
    ).length;
    return { chats, docs };
  }

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
      push({
        variant: "error",
        message:
          err instanceof Error
            ? err.message
            : "Couldn't create that project — try again.",
      });
    }
  }

  return (
    <AppShell>
      <div style={{ padding: 32, maxWidth: 900, margin: "0 auto" }}>
        <header style={headerRowStyle}>
          <h1 style={{ margin: 0, fontSize: 28 }}>Projects</h1>
          <button
            type="button"
            onClick={() => {
              setCreateOpen((v) => !v);
            }}
            style={primaryBtn}
            data-testid="projects-new-trigger"
          >
            <Plus size={16} aria-hidden style={{ verticalAlign: "middle" }} />{" "}
            New project
          </button>
        </header>

        {createOpen && (
          <form
            onSubmit={(e) => {
              void handleCreate(e);
            }}
            style={{ display: "flex", gap: 8, marginBottom: 24 }}
            data-testid="projects-new-form"
          >
            <input
              type="text"
              value={createName}
              onChange={(e) => {
                setCreateName(e.target.value);
              }}
              placeholder="Project name…"
              autoFocus
              maxLength={256}
              style={inputStyle}
              data-testid="projects-new-input"
            />
            <button
              type="submit"
              disabled={createProject.isPending}
              style={primaryBtn}
              data-testid="projects-new-submit"
            >
              {createProject.isPending ? "…" : "Create"}
            </button>
            <button
              type="button"
              onClick={() => {
                setCreateOpen(false);
                setCreateName("");
              }}
              style={secondaryBtn}
            >
              Cancel
            </button>
          </form>
        )}

        {isLoading && <p data-testid="projects-loading">Loading…</p>}
        {isError && (
          <p role="status" data-testid="projects-error">
            Couldn't load projects.{" "}
            <button
              type="button"
              onClick={() => {
                void refetch();
              }}
              style={secondaryBtn}
              data-testid="projects-retry"
            >
              Retry
            </button>
          </p>
        )}

        {!isLoading &&
          !isError &&
          activeProjects.length === 0 &&
          archivedProjects.length === 0 && (
            <p style={emptyStyle} data-testid="projects-empty">
              No projects yet — create one above to get started.
            </p>
          )}

        {!isLoading && !isError && activeProjects.length > 0 && (
          <ul style={listStyle} data-testid="projects-list">
            {activeProjects.map((p) => (
              <ProjectCard key={p.id} project={p} counts={countsFor(p.id)} />
            ))}
          </ul>
        )}

        {!isLoading && !isError && archivedProjects.length > 0 && (
          <details style={archivedDetailsStyle} data-testid="projects-archived-section">
            <summary style={archivedSummaryStyle}>
              Archived ({archivedProjects.length})
            </summary>
            <ul style={listStyle} data-testid="projects-archived-list">
              {archivedProjects.map((p) => (
                <ProjectCard key={p.id} project={p} counts={countsFor(p.id)} />
              ))}
            </ul>
          </details>
        )}
      </div>
    </AppShell>
  );
}

// ─── Project card ───────────────────────────────────────────────────────────

interface ProjectCardProps {
  project: ProjectResponse;
  counts: { chats: number; docs: number };
}

function ProjectCard({ project, counts }: ProjectCardProps) {
  return (
    <li style={cardStyle} data-testid={`projects-card-${String(project.id)}`}>
      <Link
        to={`/project/${String(project.id)}`}
        style={cardLinkStyle}
        data-testid={`projects-card-link-${String(project.id)}`}
      >
        <FolderKanban size={20} aria-hidden style={{ flexShrink: 0 }} />
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={cardNameRowStyle}>
            <span style={cardNameStyle}>{project.name}</span>
            {project.archived_at != null && (
              <span style={archivedBadgeStyle}>Archived</span>
            )}
          </div>
          {project.description !== "" && (
            <p style={cardDescriptionStyle}>{project.description}</p>
          )}
        </div>
        <div style={cardCountsStyle}>
          <span title={`${String(counts.chats)} chats`}>
            <MessageSquare size={13} aria-hidden style={{ verticalAlign: "middle" }} />{" "}
            {counts.chats}
          </span>
          <span title={`${String(counts.docs)} documents`}>
            <FileText size={13} aria-hidden style={{ verticalAlign: "middle" }} />{" "}
            {counts.docs}
          </span>
        </div>
      </Link>
    </li>
  );
}

// ─── Local styles ────────────────────────────────────────────────────────────
//
// Inline styles intentionally, mirroring Project.tsx / ProjectsSection.tsx —
// keeps the global stylesheet untouched to minimize blast radius.

const headerRowStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  justifyContent: "space-between",
  marginBottom: 24,
};

const primaryBtn: CSSProperties = {
  padding: "6px 14px",
  fontSize: 13,
  borderRadius: 4,
  border: "1px solid var(--color-border-default)",
  background: "var(--color-surface-elevated)",
  color: "var(--color-text)",
  cursor: "pointer",
};

const secondaryBtn: CSSProperties = {
  padding: "6px 14px",
  fontSize: 13,
  borderRadius: 4,
  border: "1px solid var(--color-border)",
  background: "transparent",
  color: "var(--color-text)",
  cursor: "pointer",
};

const inputStyle: CSSProperties = {
  flex: 1,
  padding: "6px 10px",
  fontSize: 13,
  borderRadius: 4,
  border: "1px solid var(--color-border)",
  background: "var(--color-surface)",
  color: "var(--color-text)",
};

const emptyStyle: CSSProperties = {
  color: "var(--color-text-muted)",
  fontStyle: "italic",
};

const listStyle: CSSProperties = {
  listStyle: "none",
  margin: 0,
  padding: 0,
  display: "flex",
  flexDirection: "column",
  gap: 8,
};

const cardStyle: CSSProperties = {
  border: "1px solid var(--color-border)",
  borderRadius: 6,
  background: "var(--color-surface-elevated)",
};

const cardLinkStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 12,
  padding: "12px 16px",
  color: "var(--color-text)",
  textDecoration: "none",
};

const cardNameRowStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
};

const cardNameStyle: CSSProperties = {
  fontWeight: 600,
  fontSize: 15,
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const cardDescriptionStyle: CSSProperties = {
  margin: "2px 0 0 0",
  fontSize: 13,
  color: "var(--color-text-muted)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

const cardCountsStyle: CSSProperties = {
  display: "flex",
  gap: 12,
  fontSize: 12,
  color: "var(--color-text-muted)",
  flexShrink: 0,
};

const archivedBadgeStyle: CSSProperties = {
  fontSize: 11,
  padding: "1px 6px",
  borderRadius: 4,
  color: "var(--color-text-muted)",
  background: "var(--color-surface)",
  border: "1px solid var(--color-border)",
};

const archivedDetailsStyle: CSSProperties = {
  marginTop: 32,
};

const archivedSummaryStyle: CSSProperties = {
  cursor: "pointer",
  fontSize: 13,
  color: "var(--color-text-muted)",
  marginBottom: 8,
};
