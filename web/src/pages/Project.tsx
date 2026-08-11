/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Project page.
 *
 * Hosts three tabs under ``/project/:id``:
 *   - **Chats** — rolling auto-summary card + list of
 *     project chats + "New chat" form
 *   - **Documents** — list of project documents + upload + attach existing
 *   - **Settings** — name, description, custom instructions (with preset
 *     dropdown that seeds the instructions field) + delete-project
 *
 * The active tab tracks the URL hash (``#chats`` / ``#documents`` /
 * ``#settings``) so a refresh or shared link preserves the tab state.
 */
import {
  useMemo,
  useState,
  type ChangeEvent,
  type CSSProperties,
  type SubmitEvent,
} from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import {
  AlertTriangle,
  Archive,
  ArchiveRestore,
  Download,
  Folder,
  MessageSquarePlus,
  RefreshCw,
  Trash2,
  Upload,
} from "lucide-react";
import { AppShell } from "@/components/AppShell";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { useEmbeddingStatus } from "@/hooks/useEmbeddingStatus";
import { formatRelativeTime } from "@/lib/relativeTime";
import { useToast } from "@/stores/toastStore";
import {
  useProject,
  useUpdateProject,
  useDeleteProject,
  useArchiveProject,
  useUnarchiveProject,
  useExportProject,
  useCreateChatInProject,
  useMoveDocumentToProject,
  useReembedProject,
  useProjectKnowledgeStats,
  useRegenerateProjectSummary,
} from "@/hooks/useProjects";
import { useChats, type ChatScope } from "@/hooks/useChats";
import {
  useDocuments,
  useUploadDocument,
  useDeleteDocument,
  type Document,
} from "@/hooks/useDocuments";
import { usePrompts, type Prompt } from "@/hooks/usePrompts";
import { useChatModelOptions } from "@/hooks/useChatModelOptions";
import {
  ModelSelectControl,
  type ModelOption,
  type ModelOptionGroup,
} from "@/components/ModelSelectControl";

type TabKey = "chats" | "documents" | "settings";

const TABS: { key: TabKey; label: string }[] = [
  { key: "chats", label: "Chats" },
  { key: "documents", label: "Documents" },
  { key: "settings", label: "Settings" },
];

function activeTabFromHash(hash: string): TabKey {
  const stripped = hash.replace(/^#/, "");
  return stripped === "documents" || stripped === "settings"
    ? stripped
    : "chats";
}

export default function Project() {
  const { id } = useParams<{ id: string }>();
  const projectId = id !== undefined ? Number(id) : null;
  const navigate = useNavigate();
  const location = useLocation();
  const { push } = useToast();
  const { data: project, isLoading, isError } = useProject(projectId);
  const updateProject = useUpdateProject(projectId ?? 0);
  const deleteProject = useDeleteProject();
  const archiveProject = useArchiveProject();
  const unarchiveProject = useUnarchiveProject();
  const exportProject = useExportProject();
  const createChatInProject = useCreateChatInProject(projectId ?? 0);

  const activeTab = activeTabFromHash(location.hash);
  function setActiveTab(next: TabKey): void {
    void navigate(`${location.pathname}#${next}`, { replace: false });
  }

  useDocumentTitle(project ? `Project · ${project.name}` : "Project");

  const [editingName, setEditingName] = useState(false);
  const [nameDraft, setNameDraft] = useState("");
  // Persists across Chats↔Documents↔Settings tab switches because Project
  // (this component) stays mounted while the conditional tabpanel below
  // does not. Previously this lived in a module-scope binding that the
  // child subscribed to, but the parent never re-rendered on
  // binding-changes — so the prop `newChatTitle` stayed stale, the input
  // always showed "", and submit reported "Chat title is required." even
  // after typing.
  const [newChatTitle, setNewChatTitle] = useState("");
  const [deleteConfirming, setDeleteConfirming] = useState(false);

  if (projectId === null || Number.isNaN(projectId)) {
    return (
      <AppShell>
        <div style={{ padding: 32 }}>
          <p>Project id missing.</p>
          <Link to="/">← Back to chats</Link>
        </div>
      </AppShell>
    );
  }

  if (isLoading) {
    return (
      <AppShell>
        <div style={{ padding: 32 }}>Loading…</div>
      </AppShell>
    );
  }

  if (isError || project === undefined) {
    return (
      <AppShell>
        <div style={{ padding: 32 }}>
          <p>Project not found.</p>
          <Link to="/">← Back to chats</Link>
        </div>
      </AppShell>
    );
  }

  async function handleSaveName(): Promise<void> {
    if (project === undefined || projectId === null || projectId === 0) return;
    try {
      await updateProject.mutateAsync({ name: nameDraft.trim() });
      setEditingName(false);
    } catch (err) {
      push({
        variant: "error",
        message:
          err instanceof Error
            ? err.message
            : "Couldn't update the project name — try again.",
      });
    }
  }

  async function handleDelete(): Promise<void> {
    if (projectId === null) return;
    try {
      await deleteProject.mutateAsync({ projectId });
      void navigate("/");
    } catch (err) {
      push({
        variant: "error",
        message:
          err instanceof Error
            ? err.message
            : "Couldn't delete this project — try again.",
      });
    }
  }

  async function handleArchive(): Promise<void> {
    if (projectId === null) return;
    try {
      await archiveProject.mutateAsync({ projectId });
    } catch (err) {
      push({
        variant: "error",
        message:
          err instanceof Error
            ? err.message
            : "Couldn't archive this project — try again.",
      });
    }
  }

  async function handleUnarchive(): Promise<void> {
    if (projectId === null) return;
    try {
      await unarchiveProject.mutateAsync({ projectId });
    } catch (err) {
      push({
        variant: "error",
        message:
          err instanceof Error
            ? err.message
            : "Couldn't unarchive this project — try again.",
      });
    }
  }

  async function handleExport(): Promise<void> {
    if (projectId === null || project === undefined) return;
    try {
      await exportProject.mutateAsync({ projectId, name: project.name });
    } catch (err) {
      push({
        variant: "error",
        message:
          err instanceof Error
            ? err.message
            : "Couldn't export this project — try again.",
      });
    }
  }

  return (
    <AppShell>
      <div style={{ padding: 32, maxWidth: 900, margin: "0 auto" }}>
        {/* Header — name (edit-in-place) */}
        <header style={{ marginBottom: 16 }}>
          {editingName ? (
            <div style={{ display: "flex", gap: 8 }}>
              <input
                type="text"
                value={nameDraft}
                onChange={(e: ChangeEvent<HTMLInputElement>) => {
                  setNameDraft(e.target.value);
                }}
                autoFocus
                maxLength={256}
                style={inputStyle}
              />
              <button
                type="button"
                onClick={() => {
                  void handleSaveName();
                }}
                style={primaryBtn}
              >
                Save
              </button>
              <button
                type="button"
                onClick={() => {
                  setEditingName(false);
                }}
                style={secondaryBtn}
              >
                Cancel
              </button>
            </div>
          ) : (
            <h1 style={{ margin: 0, fontSize: 28 }} data-testid="project-name">
              <button
                type="button"
                onClick={() => {
                  setNameDraft(project.name);
                  setEditingName(true);
                }}
                title="Click to rename"
                style={editInPlaceBtnStyle}
                aria-label={`Rename ${project.name}`}
              >
                {project.name}
              </button>
            </h1>
          )}
          {project.archived_at != null && (
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: 4,
                marginTop: 4,
                padding: "2px 8px",
                borderRadius: 4,
                fontSize: 12,
                color: "var(--color-text-muted)",
                background: "var(--color-surface-elevated)",
                border: "1px solid var(--color-border)",
              }}
              data-testid="project-archived-badge"
            >
              <Archive size={12} aria-hidden /> Archived
            </span>
          )}
          {project.description !== "" && (
            <p
              style={{
                margin: "8px 0 0 0",
                color: "var(--color-text-muted)",
              }}
              data-testid="project-description"
            >
              {project.description}
            </p>
          )}
        </header>

        {/* Tab strip */}
        <div
          role="tablist"
          aria-label="Project sections"
          style={tabStripStyle}
          data-testid="project-tablist"
        >
          {TABS.map((t) => {
            const isActive = activeTab === t.key;
            return (
              <button
                key={t.key}
                role="tab"
                type="button"
                aria-selected={isActive}
                aria-controls={`project-tab-panel-${t.key}`}
                id={`project-tab-${t.key}`}
                onClick={() => {
                  setActiveTab(t.key);
                }}
                style={{
                  ...tabBtnStyle,
                  borderBottomColor: isActive
                    ? "var(--color-accent)"
                    : "transparent",
                  color: isActive
                    ? "var(--color-accent-text)"
                    : "var(--color-text-muted)",
                  fontWeight: isActive ? 600 : 500,
                  background: isActive
                    ? "color-mix(in oklch, var(--color-accent) 8%, transparent)"
                    : "transparent",
                }}
                data-testid={`project-tab-${t.key}`}
              >
                {t.label}
              </button>
            );
          })}
        </div>

        {/* Tab panels */}
        {activeTab === "chats" && (
          <div
            role="tabpanel"
            id="project-tab-panel-chats"
            aria-labelledby="project-tab-chats"
          >
            <ChatsTab
              projectId={projectId}
              newChatTitle={newChatTitle}
              setNewChatTitle={setNewChatTitle}
              onCreate={async (title) => {
                if (projectId === 0) return;
                try {
                  const created = await createChatInProject.mutateAsync({
                    title,
                  });
                  void navigate(`/chats/${String(created.id)}`);
                } catch (err) {
                  push({
                    variant: "error",
                    message:
                      err instanceof Error
                        ? err.message
                        : "Couldn't create that chat — try again.",
                  });
                }
              }}
              creating={createChatInProject.isPending}
            />
          </div>
        )}

        {activeTab === "documents" && (
          <div
            role="tabpanel"
            id="project-tab-panel-documents"
            aria-labelledby="project-tab-documents"
          >
            <DocumentsTab projectId={projectId} />
          </div>
        )}

        {activeTab === "settings" && (
          <div
            role="tabpanel"
            id="project-tab-panel-settings"
            aria-labelledby="project-tab-settings"
          >
            <SettingsTab
              project={project}
              onUpdate={async (body) => {
                await updateProject.mutateAsync(body);
              }}
            />
            <section aria-label="Export" style={{ marginTop: 32 }}>
              <h2 style={sectionHeading}>Export project</h2>
              <p
                style={{
                  color: "var(--color-text-muted)",
                  fontSize: 13,
                }}
              >
                Downloads a portable JSON backup of this project —
                instructions, documents (with extracted text), and every
                chat's messages. A local backup/handoff artifact, not a
                sharing link.
              </p>
              <button
                type="button"
                onClick={() => {
                  void handleExport();
                }}
                disabled={exportProject.isPending}
                style={secondaryBtn}
                data-testid="project-export-trigger"
              >
                <Download
                  size={16}
                  aria-hidden
                  style={{ verticalAlign: "middle" }}
                />{" "}
                {exportProject.isPending ? "Exporting…" : "Export project"}
              </button>
            </section>

            <section aria-label="Archiving" style={{ marginTop: 32 }}>
              <h2 style={sectionHeading}>
                {project.archived_at != null
                  ? "Unarchive project"
                  : "Archive project"}
              </h2>
              <p
                style={{
                  color: "var(--color-text-muted)",
                  fontSize: 13,
                }}
              >
                {project.archived_at != null
                  ? "Restores this project to the default sidebar and project list."
                  : "Drops this project out of the default sidebar and project list. Chats and documents are untouched — you can unarchive any time."}
              </p>
              {project.archived_at != null ? (
                <button
                  type="button"
                  onClick={() => {
                    void handleUnarchive();
                  }}
                  disabled={unarchiveProject.isPending}
                  style={secondaryBtn}
                  data-testid="project-unarchive-trigger"
                >
                  <ArchiveRestore
                    size={16}
                    aria-hidden
                    style={{ verticalAlign: "middle" }}
                  />{" "}
                  Unarchive project
                </button>
              ) : (
                <button
                  type="button"
                  onClick={() => {
                    void handleArchive();
                  }}
                  disabled={archiveProject.isPending}
                  style={secondaryBtn}
                  data-testid="project-archive-trigger"
                >
                  <Archive
                    size={16}
                    aria-hidden
                    style={{ verticalAlign: "middle" }}
                  />{" "}
                  Archive project
                </button>
              )}
            </section>

            <section aria-label="Danger zone" style={{ marginTop: 32 }}>
              <h2 style={sectionHeading}>Delete project</h2>
              <p
                style={{
                  color: "var(--color-text-muted)",
                  fontSize: 13,
                }}
              >
                Deletes the project. Its chats and documents survive (they
                become un-projected; the FK cascade flips them to NULL).
              </p>
              {deleteConfirming ? (
                <div style={{ display: "flex", gap: 8 }}>
                  <button
                    type="button"
                    onClick={() => {
                      void handleDelete();
                    }}
                    style={dangerBtn}
                    data-testid="project-delete-confirm"
                  >
                    Yes, delete it
                  </button>
                  <button
                    type="button"
                    onClick={() => {
                      setDeleteConfirming(false);
                    }}
                    style={secondaryBtn}
                  >
                    Cancel
                  </button>
                </div>
              ) : (
                <button
                  type="button"
                  onClick={() => {
                    setDeleteConfirming(true);
                  }}
                  style={dangerBtn}
                  data-testid="project-delete-trigger"
                >
                  <Trash2
                    size={16}
                    aria-hidden
                    style={{ verticalAlign: "middle" }}
                  />{" "}
                  Delete project
                </button>
              )}
            </section>
          </div>
        )}
      </div>
    </AppShell>
  );
}

// ─── Project summary ────────────────────────────────────────────────────────

function ProjectSummaryCard({ projectId }: { projectId: number }) {
  const { push } = useToast();
  const { data: project } = useProject(projectId);
  const regenerate = useRegenerateProjectSummary(projectId);

  async function handleRegenerate(): Promise<void> {
    try {
      await regenerate.mutateAsync();
    } catch (err) {
      push({
        variant: "error",
        message:
          err instanceof Error
            ? err.message
            : "Couldn't regenerate the project summary — try again.",
      });
    }
  }

  const summary = project?.summary ?? "";
  const updatedAt = project?.summary_updated_at ?? null;

  return (
    <section
      aria-label="Project summary"
      style={{ marginBottom: 24 }}
      data-testid="project-summary-card"
    >
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          gap: 8,
        }}
      >
        <h2 style={{ ...sectionHeading, margin: 0 }}>Project summary</h2>
        <button
          type="button"
          onClick={() => {
            void handleRegenerate();
          }}
          disabled={regenerate.isPending}
          style={secondaryBtn}
          data-testid="project-summary-regenerate"
        >
          <RefreshCw
            size={16}
            aria-hidden
            style={{ verticalAlign: "middle" }}
          />{" "}
          {regenerate.isPending ? "Regenerating…" : "Regenerate"}
        </button>
      </div>
      {summary !== "" ? (
        <>
          <p
            style={{ margin: "8px 0 4px 0", whiteSpace: "pre-wrap" }}
            data-testid="project-summary-text"
          >
            {summary}
          </p>
          {updatedAt !== null && (
            <p
              style={{
                margin: 0,
                fontSize: 12,
                color: "var(--color-text-muted)",
              }}
              data-testid="project-summary-updated-at"
            >
              Updated {formatRelativeTime(updatedAt)}
            </p>
          )}
        </>
      ) : (
        <p
          style={{
            margin: "8px 0 0 0",
            color: "var(--color-text-muted)",
            fontSize: 13,
            fontStyle: "italic",
          }}
          data-testid="project-summary-empty"
        >
          No summary yet — it builds as you chat, or regenerate now.
        </p>
      )}
    </section>
  );
}

// ─── Chats tab ──────────────────────────────────────────────────────────────

interface ChatsTabProps {
  projectId: number;
  newChatTitle: string;
  setNewChatTitle: (s: string) => void;
  onCreate: (title: string) => Promise<void>;
  creating: boolean;
}

function ChatsTab({
  projectId,
  newChatTitle,
  setNewChatTitle,
  onCreate,
  creating,
}: ChatsTabProps) {
  const scope: ChatScope = { projectId };
  const { data: chatsData } = useChats(scope);

  async function handleSubmit(e: SubmitEvent<HTMLFormElement>): Promise<void> {
    e.preventDefault();
    const title = newChatTitle.trim();
    await onCreate(title);
    setNewChatTitle("");
  }

  return (
    <section aria-label="Project chats">
      <ProjectSummaryCard projectId={projectId} />
      <form
        onSubmit={(e) => {
          void handleSubmit(e);
        }}
        style={{ display: "flex", gap: 8, marginBottom: 12 }}
      >
        <input
          type="text"
          value={newChatTitle}
          onChange={(e) => {
            setNewChatTitle(e.target.value);
          }}
          placeholder="New chat title…"
          maxLength={256}
          style={inputStyle}
          data-testid="project-new-chat-input"
        />
        <button
          type="submit"
          disabled={creating}
          style={primaryBtn}
          data-testid="project-new-chat-submit"
        >
          <MessageSquarePlus
            size={16}
            aria-hidden
            style={{ verticalAlign: "middle" }}
          />{" "}
          New chat
        </button>
      </form>
      <ul style={listStyle} data-testid="project-chats-list">
        {(chatsData?.chats ?? []).map((c) => (
          <li key={c.id} style={chatRowStyle}>
            <Link
              to={`/c/${String(c.id)}`}
              style={{
                color: "var(--color-text)",
                textDecoration: "none",
              }}
            >
              {c.title}
            </Link>
            {c.folder !== null && (
              <span style={folderChip}>
                <Folder
                  size={12}
                  aria-hidden
                  style={{ verticalAlign: "middle" }}
                />{" "}
                {c.folder}
              </span>
            )}
          </li>
        ))}
        {(chatsData?.chats ?? []).length === 0 && (
          <li
            style={{
              color: "var(--color-text-muted)",
              fontStyle: "italic",
            }}
          >
            No chats yet — create one above to get started.
          </li>
        )}
      </ul>
    </section>
  );
}

// ─── Documents tab ──────────────────────────────────────────────────────────

interface DocumentsTabProps {
  projectId: number;
}

function DocumentsTab({ projectId }: DocumentsTabProps) {
  const { push } = useToast();
  const { data: allDocs } = useDocuments();
  const { data: project } = useProject(projectId);
  const { data: embeddingStatus } = useEmbeddingStatus();
  const uploadDoc = useUploadDocument(projectId);
  const deleteDoc = useDeleteDocument();
  const moveDoc = useMoveDocumentToProject();
  const reembed = useReembedProject(projectId);

  const pinnedEmbeddingModelId = project?.embedding_model_id ?? null;
  const activeEmbeddingModelId = embeddingStatus?.active_model_id ?? null;
  const showEmbeddingMismatch =
    pinnedEmbeddingModelId !== null &&
    pinnedEmbeddingModelId !== "" &&
    activeEmbeddingModelId !== null &&
    pinnedEmbeddingModelId !== activeEmbeddingModelId;

  async function handleReembed(): Promise<void> {
    try {
      const result = await reembed.mutateAsync();
      push({
        variant: "success",
        message:
          `Re-embedded ${String(result.documents_re_embedded)} ` +
          `document${result.documents_re_embedded === 1 ? "" : "s"} ` +
          `(${String(result.chunks_re_embedded)} chunks) under ` +
          `${result.active_embedding_model_id}.`,
      });
    } catch (err) {
      push({
        variant: "error",
        message:
          err instanceof Error
            ? err.message
            : "Couldn't re-embed project documents — try again.",
      });
    }
  }

  const projectDocs = useMemo(
    () => (allDocs ?? []).filter((d) => (d.project_id ?? null) === projectId),
    [allDocs, projectId],
  );
  const unprojectedDocs = useMemo(
    () => (allDocs ?? []).filter((d) => (d.project_id ?? null) === null),
    [allDocs],
  );

  const [attachPickerOpen, setAttachPickerOpen] = useState(false);

  async function handleUpload(e: ChangeEvent<HTMLInputElement>): Promise<void> {
    const file = e.target.files?.[0];
    e.target.value = "";
    if (!file) return;
    // Single round-trip — ``uploadDoc`` is bound to this project's id, so
    // it hits ``POST /api/projects/{id}/documents`` directly. The doc
    // never exists un-projected.
    try {
      const result = await uploadDoc.mutateAsync(file);
      push({
        variant: "success",
        message: `Uploaded "${result.filename}" into project.`,
      });
    } catch {
      push({
        variant: "error",
        message: `Couldn't upload "${file.name}" — try again.`,
      });
    }
  }

  function handleAttach(documentId: number): void {
    moveDoc.mutate({
      documentId,
      oldProjectId: null,
      newProjectId: projectId,
    });
    setAttachPickerOpen(false);
  }

  function handleRemoveFromProject(documentId: number): void {
    moveDoc.mutate({
      documentId,
      oldProjectId: projectId,
      newProjectId: null,
    });
  }

  async function handleDelete(documentId: number): Promise<void> {
    try {
      await deleteDoc.mutateAsync(documentId);
    } catch (err) {
      push({
        variant: "error",
        message:
          err instanceof Error
            ? err.message
            : "Couldn't delete that document — try again.",
      });
    }
  }

  return (
    <section aria-label="Project documents">
      <KnowledgeMeter projectId={projectId} hasDocs={projectDocs.length > 0} />
      {/* When the project's pinned embedding model
          differs from the user's currently active one, surface a
          warning naming both ids + an inline "Re-embed all" button
          that rewrites every chunk under the new active model and
          updates the project pin (POST /api/projects/{id}/re-embed). */}
      {showEmbeddingMismatch && (
        <div
          role="status"
          data-testid="embedding-mismatch-warning"
          style={mismatchStyle}
        >
          <AlertTriangle size={16} aria-hidden style={{ flexShrink: 0 }} />
          <span style={{ flex: 1 }}>
            This project's documents were embedded under{" "}
            <code style={mismatchCodeStyle}>{pinnedEmbeddingModelId}</code> but
            your active embedding model is{" "}
            <code style={mismatchCodeStyle}>{activeEmbeddingModelId}</code>.
            Retrieval against the existing chunks will mis-cosine until the
            project is re-embedded under the new model.
          </span>
          <button
            type="button"
            onClick={() => {
              void handleReembed();
            }}
            disabled={reembed.isPending}
            data-testid="reembed-project-button"
            style={reembedBtnStyle}
            aria-label={
              reembed.isPending
                ? "Re-embedding…"
                : `Re-embed all documents under ${activeEmbeddingModelId}`
            }
          >
            {reembed.isPending ? "Re-embedding…" : "Re-embed all"}
          </button>
        </div>
      )}
      <div
        style={{
          display: "flex",
          gap: 8,
          marginBottom: 12,
          alignItems: "center",
        }}
      >
        <label
          htmlFor="project-doc-upload-input"
          style={primaryBtn}
          data-testid="project-doc-upload-label"
        >
          <Upload size={16} aria-hidden style={{ verticalAlign: "middle" }} />{" "}
          Upload
        </label>
        <input
          id="project-doc-upload-input"
          type="file"
          accept=".txt,.md,.html,.pdf,.epub,.docx"
          onChange={(e) => {
            void handleUpload(e);
          }}
          style={{ display: "none" }}
          data-testid="project-doc-upload-input"
        />
        <button
          type="button"
          onClick={() => {
            setAttachPickerOpen((v) => !v);
          }}
          style={secondaryBtn}
          data-testid="project-doc-attach-toggle"
        >
          {attachPickerOpen ? "Cancel attach" : "Attach existing"}
        </button>
      </div>

      {attachPickerOpen && (
        <div
          style={{
            border: "1px solid var(--color-border-default)",
            background: "var(--color-surface-elevated)",
            borderRadius: 6,
            padding: 8,
            marginBottom: 12,
          }}
          data-testid="project-doc-attach-picker"
        >
          {unprojectedDocs.length === 0 ? (
            <p
              style={{
                margin: 0,
                color: "var(--color-text-muted)",
                fontSize: 12,
                fontStyle: "italic",
              }}
            >
              No un-projected documents to attach.
            </p>
          ) : (
            <ul style={listStyle}>
              {unprojectedDocs.map((d: Document) => (
                <li
                  key={d.id}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 8,
                    padding: "4px 0",
                  }}
                >
                  <span style={{ flex: 1 }}>{d.title}</span>
                  <button
                    type="button"
                    onClick={() => {
                      handleAttach(d.id);
                    }}
                    style={secondaryBtn}
                    data-testid={`project-doc-attach-${String(d.id)}`}
                  >
                    Attach
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      <ul style={listStyle} data-testid="project-docs-list">
        {projectDocs.length === 0 && (
          <li
            style={{
              color: "var(--color-text-muted)",
              fontStyle: "italic",
            }}
          >
            No documents in this project yet.
          </li>
        )}
        {projectDocs.map((d) => (
          <li
            key={d.id}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "6px 0",
              borderBottom: "1px solid var(--color-border)",
            }}
            data-testid={`project-doc-row-${String(d.id)}`}
          >
            <span style={{ flex: 1 }}>{d.title}</span>
            <button
              type="button"
              onClick={() => {
                handleRemoveFromProject(d.id);
              }}
              style={secondaryBtn}
              data-testid={`project-doc-remove-${String(d.id)}`}
            >
              Remove from project
            </button>
            <button
              type="button"
              onClick={() => {
                void handleDelete(d.id);
              }}
              style={dangerBtn}
              data-testid={`project-doc-delete-${String(d.id)}`}
            >
              Delete
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

// ─── KB capacity meter ──────────────────────────────────────────────────────

interface KnowledgeMeterProps {
  projectId: number;
  hasDocs: boolean;
}

function KnowledgeMeter({ projectId, hasDocs }: KnowledgeMeterProps) {
  const { data: stats } = useProjectKnowledgeStats(projectId);

  if (!hasDocs) {
    return (
      <p
        style={{
          margin: "0 0 12px 0",
          color: "var(--color-text-muted)",
          fontSize: 13,
          fontStyle: "italic",
        }}
        data-testid="project-kb-meter-empty"
      >
        No documents yet — the knowledge meter fills in once you add some.
      </p>
    );
  }

  // Loading — skip rendering rather than flash a 0%-full bar.
  if (stats === undefined) return null;

  const pct =
    stats.threshold > 0
      ? Math.min(100, (stats.corpus_tokens / stats.threshold) * 100)
      : 0;
  const tone = pct >= 100 ? "danger" : pct >= 80 ? "warning" : "ok";
  const color =
    tone === "danger"
      ? "var(--color-danger)"
      : tone === "warning"
        ? "var(--color-warning)"
        : "var(--color-accent)";

  return (
    <div style={{ marginBottom: 16 }} data-testid="project-kb-meter">
      <p
        style={{
          margin: "0 0 4px 0",
          fontSize: 13,
          color: "var(--color-text-muted)",
        }}
      >
        ~{stats.corpus_tokens.toLocaleString()} tokens of knowledge ·{" "}
        {Math.round(pct)}% of the inline threshold
      </p>
      <div
        style={kbMeterTrackStyle}
        role="progressbar"
        aria-valuenow={Math.round(pct)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`Knowledge base: ${String(Math.round(pct))}% of the inline threshold`}
      >
        <div
          style={{ ...kbMeterFillStyle, width: `${String(pct)}%`, background: color }}
        />
      </div>
    </div>
  );
}

// ─── Settings tab ───────────────────────────────────────────────────────────

interface SettingsTabProps {
  project: {
    name: string;
    description: string;
    system_prompt: string;
    default_model_id?: string | null;
    rag_threshold?: number | null;
  };
  onUpdate: (body: {
    name?: string;
    description?: string;
    system_prompt?: string;
    default_model_id?: string;
    rag_threshold?: number;
    clear?: string;
  }) => Promise<void>;
}

// Sentinel — a real, always-selectable
// "Use global default" option prepended to the model list. Distinct from
// ModelSelectControl's own `placeholder` prop, which renders a *disabled*
// option: once a real model was picked, a disabled placeholder can never
// be re-selected, so it can't carry the "clear back to NULL" affordance
// this picker needs. `loaded: true` keeps it visible in the "Loaded"
// optgroup when the real options split loaded/unloaded (the component
// filters strictly on `loaded === true` / `=== false`; anything else,
// like an unset `loaded`, silently disappears from both groups).
const USE_GLOBAL_DEFAULT_OPTION: ModelOption = {
  id: "",
  label: "Use global default",
  loaded: true,
};

function SettingsTab({ project, onUpdate }: SettingsTabProps) {
  const { push } = useToast();
  const { data: prompts } = usePrompts();
  const {
    options: chatModelOptions,
    groups: chatModelGroups,
    isLoading: modelsLoading,
  } = useChatModelOptions();
  const [name, setName] = useState(project.name);
  const [description, setDescription] = useState(project.description);
  const [systemPrompt, setSystemPrompt] = useState(project.system_prompt);
  const [selectedPresetId, setSelectedPresetId] = useState<string>("");
  const [defaultModelId, setDefaultModelId] = useState<string>(
    project.default_model_id ?? "",
  );
  const [ragThreshold, setRagThreshold] = useState<string>(
    project.rag_threshold !== null && project.rag_threshold !== undefined
      ? String(project.rag_threshold)
      : "",
  );

  // Bug fix: every other model picker in the app — the chat
  // header `<select>` (pages/Chat.tsx TopBar) and PresetModelsSection —
  // renders/selects composite "<provider>::<model_id>" option VALUES, built
  // by mapping the SAME `useChatModelOptions()` output through
  // `${o.provider}::${o.id}`. This picker used to render bare `o.id`
  // values instead, so a project whose `default_model_id` matched a real
  // (bare) model never matched any *rendered* option — it showed "Use
  // global default" and Save silently cleared the pin. `default_model_id`
  // itself stays a bare id in storage (there's no `default_model_provider`
  // column, and `create_chat_in_project` seeds it verbatim into the new
  // chat's bare `chats.model_id` — composite would break that seed). The
  // composite transform below is therefore purely a rendering-layer
  // convenience, exactly mirroring Chat.tsx: build composite ids for the
  // `<select>`, decode back to bare on selection.
  const modelOptions = useMemo<ModelOption[]>(
    () => [
      USE_GLOBAL_DEFAULT_OPTION,
      ...chatModelOptions.map((o) => ({ ...o, id: `${o.provider}::${o.id}` })),
    ],
    [chatModelOptions],
  );
  // Same sentinel, but nested in its own optgroup ahead of the real
  // provider groups — the grouped render path ignores the flat
  // `options` prop entirely, so the sentinel has to live in `groups`
  // too or it never renders when multiple providers are configured.
  const modelGroups = useMemo<ModelOptionGroup[]>(
    () =>
      chatModelGroups.length > 1
        ? [
            {
              provider: "",
              label: "Default",
              options: [USE_GLOBAL_DEFAULT_OPTION],
            },
            ...chatModelGroups.map((g) => ({
              ...g,
              options: g.options.map((o) => ({
                ...o,
                id: `${o.provider}::${o.id}`,
              })),
            })),
          ]
        : [],
    [chatModelGroups],
  );
  // The `<select>` VALUE, derived from the stored bare `defaultModelId` —
  // look up its real provider in the live catalog (covers a future
  // multi-provider default); fall back to "lmstudio" when the model isn't
  // currently known, same default Chat.tsx uses when a chat has no
  // explicit `settings.provider` yet.
  const defaultModelComposite = useMemo(() => {
    if (defaultModelId === "") return "";
    const known = chatModelOptions.find((o) => o.id === defaultModelId);
    const provider = known?.provider ?? "lmstudio";
    return `${provider}::${defaultModelId}`;
  }, [defaultModelId, chatModelOptions]);
  function handleDefaultModelChange(compositeOrEmpty: string): void {
    if (compositeOrEmpty === "") {
      setDefaultModelId("");
      return;
    }
    // Decode composite "<provider>::<model_id>" — split on FIRST :: only.
    // Mirrors Chat.tsx's onModelChange exactly; only the bare model_id is
    // persisted (see comment above modelOptions).
    const sepIdx = compositeOrEmpty.indexOf("::");
    const modelId =
      sepIdx >= 0 ? compositeOrEmpty.slice(sepIdx + 2) : compositeOrEmpty;
    setDefaultModelId(modelId);
  }

  function handlePresetChange(e: ChangeEvent<HTMLSelectElement>): void {
    const id = e.target.value;
    setSelectedPresetId(id);
    if (id === "") return;
    const preset = (prompts ?? []).find((p: Prompt) => String(p.id) === id);
    if (preset === undefined) return;
    // Copies preset text into the instructions field (avoids
    // silent-invalidation when the preset is later edited).
    setSystemPrompt(preset.content);
    push({
      variant: "info",
      message: `Loaded preset "${preset.name}" — remember to Save.`,
    });
  }

  async function handleSave(e: SubmitEvent<HTMLFormElement>): Promise<void> {
    e.preventDefault();
    const trimmedThreshold = ragThreshold.trim();
    let parsedThreshold: number | null = null;
    if (trimmedThreshold !== "") {
      const n = Number(trimmedThreshold);
      if (!Number.isInteger(n) || n < 0) {
        push({
          variant: "error",
          message:
            "RAG threshold must be a non-negative whole number of tokens, or blank.",
        });
        return;
      }
      parsedThreshold = n;
    }

    // Both fields are nullable columns where NULL is itself meaningful
    // ("use the global default" / "use the formula") — clearing goes
    // through `clear=` rather than sending an empty value (mirrors the
    // PATCH route's own clear semantics for these two fields).
    const clearFields: string[] = [];
    if (defaultModelId === "") clearFields.push("default_model_id");
    if (parsedThreshold === null) clearFields.push("rag_threshold");

    try {
      await onUpdate({
        name: name.trim(),
        description,
        system_prompt: systemPrompt,
        ...(defaultModelId !== "" ? { default_model_id: defaultModelId } : {}),
        ...(parsedThreshold !== null ? { rag_threshold: parsedThreshold } : {}),
        ...(clearFields.length > 0 ? { clear: clearFields.join(",") } : {}),
      });
      push({ variant: "success", message: "Project settings saved." });
    } catch (err) {
      push({
        variant: "error",
        message:
          err instanceof Error
            ? err.message
            : "Couldn't save project settings — try again.",
      });
    }
  }

  return (
    <section
      aria-label="Project settings"
      style={{ display: "flex", flexDirection: "column", gap: 16 }}
    >
      <form
        onSubmit={(e) => {
          void handleSave(e);
        }}
        style={{ display: "flex", flexDirection: "column", gap: 12 }}
      >
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <label style={labelStyle} htmlFor="project-settings-name">
            Name
          </label>
          <input
            id="project-settings-name"
            type="text"
            value={name}
            onChange={(e) => {
              setName(e.target.value);
            }}
            maxLength={256}
            style={inputStyle}
            data-testid="project-settings-name"
          />
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <label style={labelStyle} htmlFor="project-settings-description">
            Description
          </label>
          <textarea
            id="project-settings-description"
            value={description}
            onChange={(e) => {
              setDescription(e.target.value);
            }}
            rows={2}
            maxLength={1024}
            style={textareaStyle}
            data-testid="project-settings-description"
          />
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <label style={labelStyle} htmlFor="project-settings-preset">
            Use a preset as starting point
          </label>
          <select
            id="project-settings-preset"
            value={selectedPresetId}
            onChange={handlePresetChange}
            style={inputStyle}
            data-testid="project-settings-preset"
          >
            <option value="">— Select a preset —</option>
            {(prompts ?? []).map((p: Prompt) => (
              <option key={p.id} value={String(p.id)}>
                {p.name}
              </option>
            ))}
          </select>
          <p
            style={{
              margin: 0,
              fontSize: 11,
              color: "var(--color-text-muted)",
            }}
          >
            Selecting a preset copies its text into the instructions field
            below. Future edits to the preset will not affect this project.
          </p>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <label style={labelStyle} htmlFor="project-settings-prompt">
            Custom instructions (system prompt)
          </label>
          <textarea
            id="project-settings-prompt"
            value={systemPrompt}
            onChange={(e) => {
              setSystemPrompt(e.target.value);
            }}
            rows={10}
            maxLength={16384}
            style={textareaStyle}
            placeholder="Optional. Applied automatically to every chat in this project."
            data-testid="project-settings-prompt"
          />
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <label style={labelStyle} htmlFor="project-settings-default-model">
            Default model
          </label>
          <ModelSelectControl
            id="project-settings-default-model"
            ariaLabel="Default model for new chats in this project"
            value={defaultModelComposite}
            onChange={handleDefaultModelChange}
            options={modelOptions}
            {...(modelGroups.length > 0 ? { groups: modelGroups } : {})}
            isLoading={modelsLoading}
            className="lmchat-select"
            testId="project-settings-default-model"
          />
          <p
            style={{
              margin: 0,
              fontSize: 11,
              color: "var(--color-text-muted)",
            }}
          >
            Seeds the model for every new chat created in this project.
            "Use global default" falls through to your app-wide default.
          </p>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          <label style={labelStyle} htmlFor="project-settings-rag-threshold">
            RAG inline/hybrid threshold (tokens)
          </label>
          <input
            id="project-settings-rag-threshold"
            type="number"
            min={0}
            step={1}
            value={ragThreshold}
            onChange={(e) => {
              setRagThreshold(e.target.value);
            }}
            style={inputStyle}
            placeholder="Automatic"
            data-testid="project-settings-rag-threshold"
          />
          <p
            style={{
              margin: 0,
              fontSize: 11,
              color: "var(--color-text-muted)",
            }}
          >
            Below this, the whole project's documents are inlined into every
            request; above it, retrieval falls back to hybrid search. Leave
            blank to size the threshold from the active model's context
            window automatically.
          </p>
        </div>
        <div>
          <button
            type="submit"
            style={primaryBtn}
            data-testid="project-settings-save"
          >
            Save settings
          </button>
        </div>
      </form>
    </section>
  );
}

// ─── Local styles ────────────────────────────────────────────────────────────

const inputStyle: CSSProperties = {
  padding: "6px 10px",
  fontSize: 14,
  borderRadius: 4,
  border: "1px solid var(--color-border)",
  background: "var(--color-surface)",
  color: "var(--color-text)",
  flex: 1,
};

const textareaStyle: CSSProperties = {
  padding: "6px 10px",
  fontSize: 13,
  borderRadius: 4,
  border: "1px solid var(--color-border)",
  background: "var(--color-surface)",
  color: "var(--color-text)",
  fontFamily: "inherit",
  resize: "vertical",
};

const labelStyle: CSSProperties = {
  fontSize: 12,
  textTransform: "uppercase",
  letterSpacing: "0.04em",
  color: "var(--color-text-muted)",
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

const dangerBtn: CSSProperties = {
  ...primaryBtn,
  background: "var(--color-danger-quiet)",
  borderColor: "var(--color-danger)",
  color: "var(--color-danger)",
};

const sectionHeading: CSSProperties = {
  margin: "16px 0 8px 0",
  fontSize: 16,
  textTransform: "uppercase",
  letterSpacing: "0.04em",
  color: "var(--color-text-muted)",
};

const listStyle: CSSProperties = {
  listStyle: "none",
  padding: 0,
  margin: 0,
};

const chatRowStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "6px 0",
  borderBottom: "1px solid var(--color-border)",
};

const editInPlaceBtnStyle: CSSProperties = {
  background: "transparent",
  border: "none",
  padding: 0,
  margin: 0,
  font: "inherit",
  color: "inherit",
  cursor: "pointer",
  textAlign: "left",
  width: "100%",
};

const folderChip: CSSProperties = {
  fontSize: 11,
  padding: "2px 6px",
  borderRadius: 8,
  background: "var(--color-surface-elevated)",
  color: "var(--color-text-muted)",
};

const tabStripStyle: CSSProperties = {
  display: "flex",
  gap: 4,
  borderBottom: "1px solid var(--color-border)",
  marginBottom: 16,
};

// Active-state visibility bumped — the previous 2px transparent
// underline relied on a subtle color shift that read as "no selection" in
// small viewports. Now the active tab gets a 3px accent underline + accent-
// quiet background tint, matching the rhythm of the settings nav active
// state without the brass-shimmer overkill.
const tabBtnStyle: CSSProperties = {
  background: "transparent",
  border: "none",
  borderBottom: "3px solid transparent",
  padding: "10px 16px",
  fontSize: "var(--fs-label)",
  fontFamily: "var(--font-sans)",
  cursor: "pointer",
  marginBottom: -1,
  borderRadius: "var(--radius-xs) var(--radius-xs) 0 0",
  transition:
    "color var(--duration-fast) var(--ease-out-quart), " +
    "background var(--duration-fast) var(--ease-out-quart), " +
    "border-bottom-color var(--duration-fast) var(--ease-out-quart)",
};

const mismatchStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: 8,
  padding: "10px 12px",
  background: "var(--color-warning-quiet)",
  color: "var(--color-warning)",
  border: "1px solid var(--color-warning)",
  borderRadius: 6,
  fontSize: 13,
  margin: "8px 0",
};

const mismatchCodeStyle: CSSProperties = {
  background: "var(--color-surface)",
  padding: "1px 6px",
  borderRadius: 4,
  fontFamily:
    "ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace",
  fontSize: 12,
};

const reembedBtnStyle: CSSProperties = {
  padding: "6px 12px",
  fontSize: 12,
  fontWeight: 500,
  background: "var(--color-warning)",
  color: "var(--color-surface)",
  border: "none",
  borderRadius: 4,
  cursor: "pointer",
  whiteSpace: "nowrap",
  flexShrink: 0,
};

// KB capacity meter — same track/fill recipe as
// QuotaSection's QuotaBar, inlined here since Project.tsx keeps its own
// inline styles rather than touching the global stylesheet (see
// ProjectsSection.tsx's "Local styles" note).
const kbMeterTrackStyle: CSSProperties = {
  height: 6,
  borderRadius: 3,
  background: "var(--color-border)",
  overflow: "hidden",
};

const kbMeterFillStyle: CSSProperties = {
  height: "100%",
  borderRadius: 3,
  transition: "width 0.2s ease",
};
