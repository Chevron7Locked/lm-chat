/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Sidebar — collapsible chat list with pinned, folder-grouped chats.
 *
 * DnD-kit sortable per folder + pinned section, with a KeyboardSensor for
 * WCAG 2.5.1 keyboard DnD support.
 *
 * Cross-folder moves: each folder + the pinned section share a SINGLE
 *   DndContext (pinned + every folder, including empty ones, via
 *   ``FolderDroppable``/``useDroppable``). A per-container DndContext could
 *   never resolve a drop across containers, and empty folders would register
 *   no droppable target at all. A per-row "Move to folder" menu
 *   (``MoveToFolderMenu``) is an always-working, keyboard/mobile-accessible
 *   fallback.
 *
 * Data via TanStack Query (useChatsDirect). Includes:
 *  - New chat button
 *  - Filter text input
 *  - Pinned chats at top (sorted by display_order)
 *  - Remaining chats grouped by folder (ungrouped last; sorted by display_order)
 *  - DnD drag-and-drop, including across folders (single DndContext,
 *    closestCorners collision detection; see ``resolveDropTarget``)
 *  - Per-row "Move to folder" menu (``MoveToFolderMenu``) — reliable
 *    fallback for touch/keyboard users and any DnD edge case
 *  - Per-item: title + relative last-updated timestamp
 *  - Keyboard DnD via KeyboardSensor (Tab to handle, Space to grab,
 *    Arrow keys to move, Space/Enter to drop, Escape to cancel)
 *  - ARIA live region announces drag-start and drop events to screen readers
 *
 * All inline pixel-literal styles are replaced with sidebar.css classes
 * and semantic spacing tokens (--space-glue/sibling/group/chapter).
 * Material layers (vellum + leather) applied via CSS pseudo-elements.
 */
import { useState, useCallback, useRef, useMemo } from "react";
import type {
  CSSProperties,
  KeyboardEvent as ReactKeyboardEvent,
  MouseEvent as ReactMouseEvent,
  ReactNode,
} from "react";
import { Link, useParams, useNavigate, useLocation } from "react-router-dom";
import {
  DndContext,
  closestCorners,
  PointerSensor,
  KeyboardSensor,
  useSensor,
  useSensors,
  useDroppable,
} from "@dnd-kit/core";
import type { DragEndEvent, DragStartEvent } from "@dnd-kit/core";
import {
  SortableContext,
  verticalListSortingStrategy,
  useSortable,
  sortableKeyboardCoordinates,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
import {
  useChatsDirect,
  useCreateChat,
  useDeleteChat,
  useReorderChat,
  useArchivedChats,
  useArchiveChat,
  useUnarchiveChat,
  useUpdateChat,
} from "@/hooks/useChats";
import type { ChatSummary } from "@/hooks/useChats";
import { ChatTagsMenu } from "@/components/ChatTagsMenu";
import {
  useCreateChatInProject,
  useCreateProject,
} from "@/hooks/useProjects";
import { Trash2, Archive, ArchiveRestore } from "lucide-react";
import { useSubSessionStore } from "@/stores/subSessionStore";
import { useTitleGenerationStore } from "@/stores/titleGenerationStore";
import {
  useFolders,
  useAddFolder,
  useRenameFolder,
  useDeleteFolder,
} from "@/hooks/useFolders";
import { useToast, useToastStore } from "@/stores/toastStore";
import { UserMenu } from "@/components/UserMenu";
import { MoveToFolderMenu } from "@/components/MoveToFolderMenu";
import { api } from "@/lib/api";
import { useSidebarStats, formatTokens } from "@/hooks/useSidebarStats";
import {
  Lock,
  Unlock,
  Plus,
  FolderPlus,
  FolderKanban,
  Settings,
  Brain,
  FileText,
  BarChart2,
  BookOpen,
  HelpCircle,
  Library,
  ChevronLeft,
  ChevronRight,
  Tag as TagIcon,
} from "lucide-react";
import { usePlatform } from "@/hooks/usePlatform";
import { formatShortcut } from "@/lib/formatShortcut";
import { AnimatedSavedCounter } from "@/components/AnimatedSavedCounter";
import { BrandMark, BRAND_NAME } from "@/components/BrandMark";
import { ProjectsSection } from "@/components/ProjectsSection";
import "@/styles/sidebar.css";

// ─── Component ──────────────────────────────────────────────────────────────

interface SidebarProps {
  collapsed: boolean;
  onToggle: () => void;
  /**
   * Called when the user clicks the footer `?` button to
   * open the global KeyboardHelp modal. Optional so existing call sites
   * keep compiling; if omitted, the button is hidden.
   */
  onShowKeyboardHelp?: (() => void) | undefined;
  /**
   * When true, suppresses the desktop brand-row (BrandMark +
   * wordmark + collapse toggle) and the desktop-sidebar header block.
   * Used when Sidebar is mounted inside the mobile drawer shell which
   * already renders its own header (LM Chat wordmark + close button).
   * This prevents the double-header (drawer header + sidebar brand row)
   * that appeared as "nested sidebar inside another sidebar".
   */
  mobile?: boolean | undefined;
}

export function Sidebar({
  collapsed,
  onToggle,
  onShowKeyboardHelp,
  mobile = false,
}: SidebarProps) {
  const location = useLocation();
  const platform = usePlatform();
  const modEnter = formatShortcut(platform, { mod: true, key: "Enter" });
  const [filter, setFilter] = useState("");
  // Search mode — "keyword" is the local substring
  // filter; "semantic" hits the backend `/api/search?scope=chats` endpoint
  // (pg_trgm / FTS5 on the server). `null` means no active search yet so
  // the list shows all chats unfiltered.
  const [searchMode, setSearchMode] = useState<"keyword" | "semantic" | null>(
    null,
  );
  const [semanticHits, setSemanticHits] = useState<Set<number> | null>(null);
  const [searchPending, setSearchPending] = useState(false);
  const [announcement, setAnnouncement] = useState("");
  // The folders/recent-chats section is the UN-PROJECTED slice —
  // in-project chats appear under <ProjectsSection> above. Without
  // ``unscoped: true`` here, project chats render twice.
  const { data, isLoading, isError } = useChatsDirect({
    projectId: null,
    unscoped: true,
  });
  // Archived chats get their OWN query (GET /api/chats excludes archived
  // by default) — a dedicated "Archived" section below the main list,
  // mirroring the Projects page's active/archived split.
  const { data: archivedChatsData } = useArchivedChats();
  const archivedChats = archivedChatsData ?? [];
  const unarchiveChat = useUnarchiveChat();
  const { data: foldersData } = useFolders();
  const addFolder = useAddFolder();
  const renameFolder = useRenameFolder();
  const deleteFolder = useDeleteFolder();
  const createChat = useCreateChat();
  // Clicking sidebar "New Chat" while on /project/<n> created an
  // un-projected chat (the
  // chat showed up in the global list with project_id=NULL, so the
  // project's custom instructions never applied). Derive projectId from
  // the URL and route to the project-scoped endpoint when present.
  const projectMatch = /^\/project\/(\d+)/.exec(location.pathname);
  const activeProjectId =
    projectMatch !== null ? Number(projectMatch[1]) : null;
  // Hooks must be unconditional — instantiate even with 0 placeholder
  // so the call order stays stable. We only mutate when activeProjectId
  // is non-null below.
  const createChatInProject = useCreateChatInProject(activeProjectId ?? 0);
  const reorderChat = useReorderChat();
  const { push } = useToast();

  // Folder CRUD UI state.
  const [folderCreateOpen, setFolderCreateOpen] = useState(false);
  const [folderCreateName, setFolderCreateName] = useState("");
  const [folderMenuOpen, setFolderMenuOpen] = useState<string | null>(null);
  const [folderRenameOpen, setFolderRenameOpen] = useState<string | null>(null);
  const [folderRenameValue, setFolderRenameValue] = useState("");
  const [folderDeleteConfirm, setFolderDeleteConfirm] = useState<string | null>(
    null,
  );
  const params = useParams<{ chatId?: string }>();
  const navigate = useNavigate();
  const activeChatId =
    params.chatId !== undefined ? Number(params.chatId) : null;
  const chatsRef = useRef<ChatSummary[]>([]);
  const stats = useSidebarStats();

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(KeyboardSensor, {
      coordinateGetter: sortableKeyboardCoordinates,
    }),
  );

  const handleSignOut = useCallback(() => {
    void navigate("/login");
  }, [navigate]);

  // Toggle the next-chat incognito flag.
  // Persistent for the session; auto-reset after each create call so
  // it must be explicitly opted in each time.
  const [incognitoNext, setIncognitoNext] = useState(false);
  // Replacement-menu pattern for Projects. When viewMode is
  // "projects", the chat list + library nav swap out for a Projects-only
  // pane with a Back arrow. Mirrors the iOS/Files navigation idiom rather
  // than the inline-tree pattern that was too packed.
  const [viewMode, setViewMode] = useState<"chats" | "projects">("chats");
  // Project-create state lifted from ProjectsSection so the big CTA at
  // the top of the sidebar can drive it when viewMode is "projects".
  const createProject = useCreateProject();
  const [projectCreateOpen, setProjectCreateOpen] = useState(false);
  const [projectCreateName, setProjectCreateName] = useState("");

  async function handleCreateProject(): Promise<void> {
    const name = projectCreateName.trim();
    if (name === "") {
      push({ variant: "error", message: "Project name is required." });
      return;
    }
    try {
      const created = await createProject.mutateAsync({ name });
      setProjectCreateName("");
      setProjectCreateOpen(false);
      // Land on the new project page; viewMode goes back to chats so
      // returning via the sidebar reads as a clean back-to-list.
      setViewMode("chats");
      void navigate(`/project/${String(created.id)}`);
    } catch (err) {
      const detail =
        err instanceof Error
          ? err.message
          : "Couldn't create that project — try again.";
      push({ variant: "error", message: detail });
    }
  }

  async function handleNewChat(): Promise<void> {
    try {
      const created =
        activeProjectId !== null
          ? await createChatInProject.mutateAsync({
              title: incognitoNext ? "Incognito Chat" : "New Chat",
              incognito: incognitoNext,
            })
          : await createChat.mutateAsync({
              title: incognitoNext ? "Incognito Chat" : "New Chat",
              incognito: incognitoNext,
            });
      // Auto-reset the toggle so the next "+ New Chat" click defaults
      // to a normal chat unless explicitly opted in again.
      setIncognitoNext(false);
      // Navigate to the newly-created chat. Without this the user stays
      // on whatever page they were on (eg. /settings) and the new chat
      // just appears in the list — surprising and clearly wrong UX.
      void navigate(`/chats/${String(created.id)}`);
    } catch {
      push({
        variant: "error",
        message: "Couldn't create a new chat — try again.",
      });
    }
  }

  const chats = data ?? [];
  // Keep ref in sync so drag handlers can read current titles.
  chatsRef.current = chats;

  // Trigger a backend semantic search against chats + messages.
  // Returns the union of chat IDs whose title (scope=chats) or whose messages
  // (scope=messages) matched the query.  Caller surfaces the result by
  // populating `semanticHits` (a Set of chat IDs) which the filter logic
  // below uses to gate the visible list.
  const runSemanticSearch = useCallback(
    async (q: string) => {
      if (q.trim() === "") {
        setSearchMode(null);
        setSemanticHits(null);
        return;
      }
      setSearchPending(true);
      setSearchMode("semantic");
      try {
        const params = new URLSearchParams({ q, scope: "all", limit: "50" });
        // /api/search?scope=all returns { messages, chats, memory, _errors? }
        const resp = await api.request<{
          messages?: { chat_id?: number | null; id?: number }[];
          chats?: { id: number }[];
        }>(`/api/search?${params.toString()}`);
        const hits = new Set<number>();
        for (const c of resp.chats ?? []) hits.add(c.id);
        for (const m of resp.messages ?? []) {
          if (m.chat_id != null) hits.add(m.chat_id);
        }
        setSemanticHits(hits);
      } catch {
        push({
          variant: "error",
          message: "Semantic search failed — using keyword filter.",
        });
        setSearchMode("keyword");
        setSemanticHits(null);
      } finally {
        setSearchPending(false);
      }
    },
    [push],
  );

  // Handle Enter / Cmd+Enter / Shift+Enter on the search input.
  // Enter → semantic; Cmd+Enter or Shift+Enter → keyword.
  // Escape clears both modes and shows the unfiltered list.
  const handleSearchKey = useCallback(
    (e: ReactKeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter") {
        e.preventDefault();
        if (e.metaKey || e.ctrlKey || e.shiftKey) {
          setSearchMode("keyword");
          setSemanticHits(null);
        } else {
          void runSemanticSearch(filter);
        }
      } else if (e.key === "Escape") {
        e.preventDefault();
        setFilter("");
        setSearchMode(null);
        setSemanticHits(null);
      }
    },
    [filter, runSemanticSearch],
  );

  // Apply filter.
  //   - searchMode === "semantic": gate by `semanticHits` (backend result).
  //   - searchMode === "keyword" or filter set without explicit semantic
  //     submit: local substring match on title (existing v1 behaviour).
  //   - searchMode === null and filter !== "": as-you-type substring match
  //     (legacy behaviour preserved for users who never hit Enter).
  const filtered = useMemo<ChatSummary[]>(() => {
    if (searchMode === "semantic" && semanticHits !== null) {
      return chats.filter((c) => semanticHits.has(c.id));
    }
    if (filter.trim() === "") return chats;
    return chats.filter((c) =>
      c.title.toLowerCase().includes(filter.toLowerCase()),
    );
  }, [chats, filter, searchMode, semanticHits]);

  // Partition: pinned first, then grouped by folder.
  // Within each group sort by display_order.
  const pinned = [...filtered.filter((c) => c.pinned)].sort(
    (a, b) => a.display_order - b.display_order,
  );
  const unpinned = filtered.filter((c) => !c.pinned);

  // Group unpinned by folder (null folder → last group).
  const folderMap = new Map<string | null, ChatSummary[]>();
  for (const c of unpinned) {
    const key = c.folder ?? null;
    const arr = folderMap.get(key) ?? [];
    arr.push(c);
    folderMap.set(key, arr);
  }
  // Include user-created empty folders from the prefs catalogue.
  // These are folders the user has named but not yet filled with any chats.
  for (const f of foldersData ?? []) {
    if (!folderMap.has(f)) folderMap.set(f, []);
  }
  // Sort each folder group by display_order.
  for (const [key, arr] of folderMap.entries()) {
    folderMap.set(
      key,
      [...arr].sort((a, b) => a.display_order - b.display_order),
    );
  }
  // Sort folders alphabetically; null folder goes last.
  const sortedFolders = [...folderMap.keys()].sort((a, b) => {
    if (a === null) return 1;
    if (b === null) return -1;
    return a.localeCompare(b);
  });

  // Folder CRUD handlers.
  async function handleCreateFolder(): Promise<void> {
    const trimmed = folderCreateName.trim();
    if (trimmed === "") {
      setFolderCreateOpen(false);
      return;
    }
    try {
      await addFolder.mutateAsync({ name: trimmed });
      setFolderCreateName("");
      setFolderCreateOpen(false);
    } catch {
      push({
        variant: "error",
        message: "Couldn't create that folder — try again.",
      });
    }
  }

  async function handleRenameFolder(oldName: string): Promise<void> {
    const trimmed = folderRenameValue.trim();
    if (trimmed === "" || trimmed === oldName) {
      setFolderRenameOpen(null);
      return;
    }
    try {
      await renameFolder.mutateAsync({ oldName, newName: trimmed });
      setFolderRenameOpen(null);
      setFolderRenameValue("");
    } catch {
      push({
        variant: "error",
        message: "Couldn't rename that folder — try again.",
      });
    }
  }

  async function handleDeleteFolder(name: string): Promise<void> {
    try {
      await deleteFolder.mutateAsync({ name });
      setFolderDeleteConfirm(null);
      setFolderMenuOpen(null);
    } catch {
      push({
        variant: "error",
        message: "Couldn't delete that folder — try again.",
      });
    }
  }

  function handleDragStart(event: DragStartEvent) {
    const activeTitle =
      chatsRef.current.find((c) => c.id === Number(event.active.id))?.title ??
      "item";
    setAnnouncement(
      `Picked up: ${activeTitle}. Use arrow keys to move, Space or Enter to drop, Escape to cancel.`,
    );
  }

  function handleDragEnd(event: DragEndEvent) {
    const { active, over } = event;
    if (!over || active.id === over.id) {
      setAnnouncement("Drop cancelled.");
      return;
    }

    // Resolve the target folder + display_order from `over.id` — it may be
    // a chat id (drop between/onto items, in ANY folder or pinned) or a
    // folder-container id (drop onto an empty folder or its header/
    // whitespace). See resolveDropTarget for the full contract.
    const target = resolveDropTarget(over.id, { pinned, folderMap });
    if (target === null) {
      setAnnouncement("Drop cancelled.");
      return;
    }

    const activeTitle =
      chatsRef.current.find((c) => c.id === Number(active.id))?.title ?? "item";
    setAnnouncement(
      `Dropped: ${activeTitle} at position ${String(target.display_order + 1)}.`,
    );

    reorderChat.mutate({
      chat_id: Number(active.id),
      folder: target.folder,
      display_order: target.display_order,
    });
  }

  return (
    <aside
      aria-label="Chats"
      className={`lmchat-sidebar${collapsed ? " lmchat-sidebar--collapsed" : ""}`}
      style={sidebarShellStyle(collapsed, mobile)}
    >
      {/* Header — CHAPTER block: brand plate + collapse toggle.
          Hidden when mobile=true because the drawer shell already
          renders its own header (LM Chat wordmark + close button). Showing both
          produced a double-header ("nested sidebar inside another sidebar"). */}
      {!mobile && (
        <div className="lmchat-sidebar-header">
          <Link
            to="/"
            aria-label={`${BRAND_NAME} — home`}
            className="atelier-brand lmchat-brand-link"
          >
            <BrandMark size={22} />
            {!collapsed && (
              <span className="lmchat-brand-wordmark">{BRAND_NAME}</span>
            )}
          </Link>
          <button
            type="button"
            aria-label={collapsed ? "Expand sidebar" : "Collapse sidebar"}
            onClick={onToggle}
            className="lmchat-collapse-toggle"
          >
            <span className="lmchat-chevron" aria-hidden>
              {collapsed ? (
                <ChevronRight size={14} aria-hidden />
              ) : (
                <ChevronLeft size={14} aria-hidden />
              )}
            </span>
          </button>
        </div>
      )}

      {/* Projects entry — single row ABOVE the search bar. In chats-mode
          it opens the Projects pane; in projects-mode it acts as the back
          link. Shell rows (search / library nav / stats / user menu) stay
          visible across both modes — only the chat list area swaps. */}
      {!collapsed && (
        <button
          type="button"
          onClick={() => {
            setViewMode((v) => (v === "projects" ? "chats" : "projects"));
          }}
          className={`lmchat-projects-link${viewMode === "projects" ? " lmchat-projects-link--active" : ""}`}
          data-testid="sidebar-projects-link"
          aria-label={
            viewMode === "projects" ? "Back to chats" : "Open Projects"
          }
          title={viewMode === "projects" ? "Back to chats" : "Open Projects"}
          aria-current={viewMode === "projects" ? "page" : undefined}
        >
          <span className="lmchat-projects-link-icon" aria-hidden>
            {viewMode === "projects" ? (
              <span className="lmchat-chevron" aria-hidden>
                <ChevronLeft size={14} aria-hidden />
              </span>
            ) : (
              <FolderKanban size={14} />
            )}
          </span>
          <span className="lmchat-projects-link-label">
            {viewMode === "projects" ? "Back to chats" : "Projects"}
          </span>
          {viewMode === "chats" && (
            <span className="lmchat-projects-link-chevron" aria-hidden>
              <ChevronRight size={14} aria-hidden />
            </span>
          )}
        </button>
      )}

      {/* Big CTA — mode-aware. In chats-mode this is the New Chat button;
          in projects-mode it becomes New Project and drives the inline
          create form below. Same rhythm, different verb per mode. */}
      {!collapsed && viewMode === "chats" && (
        <div className="lmchat-new-chat-row">
          <button
            type="button"
            onClick={() => {
              void handleNewChat();
            }}
            disabled={createChat.isPending}
            className="lmchat-new-chat-btn"
            data-testid="new-chat-btn"
          >
            <Plus size={15} aria-hidden />
            {incognitoNext ? "Incognito Chat" : "New Chat"}
          </button>
          <button
            type="button"
            aria-label={
              incognitoNext
                ? "Incognito mode on — next chat will be incognito"
                : "Incognito mode off — toggle on to create an incognito chat"
            }
            aria-pressed={incognitoNext}
            title={
              incognitoNext
                ? "Incognito ON — click to disable"
                : "Incognito OFF — click to make the next chat private"
            }
            onClick={() => {
              setIncognitoNext((v) => !v);
            }}
            className={`lmchat-incognito-toggle ${incognitoNext ? "lmchat-incognito-toggle--on" : "lmchat-incognito-toggle--off"}`}
            data-testid="new-chat-incognito-toggle"
          >
            {incognitoNext ? (
              <Lock size={14} aria-hidden />
            ) : (
              <Unlock size={14} aria-hidden />
            )}
          </button>
        </div>
      )}
      {!collapsed && viewMode === "projects" && (
        <>
          <div className="lmchat-new-chat-row">
            <button
              type="button"
              onClick={() => {
                setProjectCreateOpen((v) => !v);
              }}
              disabled={createProject.isPending}
              className="lmchat-new-chat-btn"
              data-testid="sidebar-new-project-btn"
            >
              <Plus size={15} aria-hidden />
              New Project
            </button>
          </div>
          {projectCreateOpen && (
            <form
              onSubmit={(e) => {
                e.preventDefault();
                void handleCreateProject();
              }}
              className="lmchat-folder-form"
              data-testid="sidebar-projects-pane-create-form"
              style={{ margin: "var(--space-glue) var(--space-sibling) 0" }}
            >
              <input
                autoFocus
                type="text"
                value={projectCreateName}
                onChange={(e) => {
                  setProjectCreateName(e.target.value);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Escape") {
                    setProjectCreateOpen(false);
                    setProjectCreateName("");
                  }
                }}
                placeholder="Project name"
                aria-label="New project name"
                className="lmchat-folder-input"
                data-testid="sidebar-projects-pane-create-input"
                maxLength={256}
              />
              <button
                type="submit"
                disabled={createProject.isPending}
                className="lmchat-folder-submit"
                data-testid="sidebar-projects-pane-create-submit"
              >
                {createProject.isPending ? "…" : "Save"}
              </button>
            </form>
          )}
        </>
      )}

      {/* Filter — GROUP distance from New Chat; carved-into-parchment input.
          Stays visible across both viewModes: shell rows persist; only the
          chat list area swaps. */}
      {!collapsed && (
        <div className="lmchat-search-wrap">
          <input
            type="search"
            placeholder="Search chats…"
            value={filter}
            onChange={(e) => {
              setFilter(e.target.value);
              // Resetting the text clears any active semantic-search gate so
              // the user can resume typing without stale results sticking.
              if (searchMode === "semantic") {
                setSearchMode(null);
                setSemanticHits(null);
              }
            }}
            onKeyDown={handleSearchKey}
            aria-label="Filter chats"
            data-testid="sidebar-search-input"
            data-search-mode={searchMode ?? "off"}
            className="lmchat-search-input"
          />
          {filter.trim() !== "" && (
            <p className="lmchat-search-hint" data-testid="sidebar-search-hint">
              {searchPending
                ? "Searching…"
                : searchMode === "semantic"
                  ? `Semantic (Enter) · ${modEnter} for keyword`
                  : searchMode === "keyword"
                    ? "Keyword filter · Enter for semantic"
                    : `Enter: semantic · ${modEnter}: keyword`}
            </p>
          )}
        </div>
      )}

      {/* Collapsed sidebar (icon-rail only) still shows the projects
          section so the icons fit in the rail. The full chats-mode
          inline list moved to the Projects pane (see the link above). */}
      {viewMode === "chats" && collapsed && (
        <ProjectsSection
          collapsed={collapsed}
          onOpenAllProjects={() => {
            void navigate("/projects");
          }}
        />
      )}

      {/* Projects pane — replaces ONLY the chat list area in projects-mode.
          Search bar / library nav / stats / user menu all stay above and
          below. */}
      {viewMode === "projects" && !collapsed && (
        <div className="lmchat-projects-pane-body">
          <ProjectsSection
            collapsed={false}
            onOpenAllProjects={() => {
              void navigate("/projects");
            }}
          />
        </div>
      )}

      {/* Chat list — GROUP-RELAXED below search; siblings at 12px.
          Hidden in projects-mode so the pane above replaces only this section. */}
      {viewMode === "chats" && !collapsed && (
        <nav className="lmchat-nav">
          {isLoading && (
            <div
              className="lmchat-chat-skeleton"
              aria-label="Loading chats"
              role="status"
            >
              <div className="lmchat-chat-skeleton__row" />
              <div className="lmchat-chat-skeleton__row" />
              <div className="lmchat-chat-skeleton__row" />
              <div className="lmchat-chat-skeleton__row" />
              <div className="lmchat-chat-skeleton__row" />
            </div>
          )}
          {isError && (
            <p className="lmchat-hint" style={{ color: "var(--color-danger)" }}>
              Couldn't load chats — try again
            </p>
          )}

          {/* Pinned + every folder share ONE DndContext so a drag started in
              one container can resolve a drop in another (cross-folder
              moves) — a per-container DndContext would make
              cross-container drops structurally impossible.
              Each container is additionally a useDroppable target via
              FolderDroppable, including EMPTY folders (previously zero
              sortable items meant nothing to drop onto).
              closestCorners is dnd-kit's recommended collision strategy
              for multi-container sortable lists (closestCenter can pick
              the wrong container when rows are tightly packed). */}
          <DndContext
            sensors={sensors}
            collisionDetection={closestCorners}
            onDragStart={handleDragStart}
            onDragEnd={handleDragEnd}
          >
            {/* Pinned */}
            {pinned.length > 0 && (
              <FolderDroppable id={PINNED_CONTAINER_ID}>
                <SortableContext
                  items={pinned.map((c) => c.id)}
                  strategy={verticalListSortingStrategy}
                >
                  <ChatGroup
                    label="Pinned"
                    chats={pinned}
                    activeChatId={activeChatId}
                  />
                </SortableContext>
              </FolderDroppable>
            )}

            {/* Folder CRUD — "+ New folder" trigger + inline input. */}
            <div className="lmchat-folder-crud-wrap">
              {!folderCreateOpen ? (
                <button
                  type="button"
                  onClick={() => {
                    setFolderCreateOpen(true);
                  }}
                  className="lmchat-add-folder-btn"
                  data-testid="sidebar-new-folder-btn"
                >
                  <FolderPlus size={13} aria-hidden />
                  New folder
                </button>
              ) : (
                <form
                  onSubmit={(e) => {
                    e.preventDefault();
                    void handleCreateFolder();
                  }}
                  className="lmchat-folder-form"
                  data-testid="sidebar-new-folder-form"
                >
                  <input
                    autoFocus
                    type="text"
                    value={folderCreateName}
                    onChange={(e) => {
                      setFolderCreateName(e.target.value);
                    }}
                    onKeyDown={(e) => {
                      if (e.key === "Escape") {
                        setFolderCreateOpen(false);
                        setFolderCreateName("");
                      }
                    }}
                    placeholder="Folder name"
                    aria-label="New folder name"
                    className="lmchat-folder-input"
                    data-testid="sidebar-new-folder-input"
                  />
                  <button
                    type="submit"
                    className="lmchat-folder-submit"
                    data-testid="sidebar-new-folder-submit"
                  >
                    Save
                  </button>
                </form>
              )}
            </div>

            {/* Folder groups */}
            {sortedFolders.map((folder) => {
              const groupChats = folderMap.get(folder) ?? [];
              const isNamedFolder = folder !== null;
              return (
                <div key={folder ?? "__ungrouped__"}>
                  {isNamedFolder && folderRenameOpen === folder ? (
                    <form
                      onSubmit={(e) => {
                        e.preventDefault();
                        void handleRenameFolder(folder);
                      }}
                      className="lmchat-folder-rename-row"
                      data-testid={`sidebar-folder-rename-form-${folder}`}
                    >
                      <input
                        autoFocus
                        type="text"
                        value={folderRenameValue}
                        onChange={(e) => {
                          setFolderRenameValue(e.target.value);
                        }}
                        onKeyDown={(e) => {
                          if (e.key === "Escape") {
                            setFolderRenameOpen(null);
                            setFolderRenameValue("");
                          }
                        }}
                        aria-label="Rename folder"
                        className="lmchat-folder-input"
                        data-testid={`sidebar-folder-rename-input-${folder}`}
                      />
                      <button
                        type="submit"
                        className="lmchat-folder-submit"
                        data-testid={`sidebar-folder-rename-submit-${folder}`}
                      >
                        Save
                      </button>
                    </form>
                  ) : isNamedFolder && folderDeleteConfirm === folder ? (
                    <div
                      className="lmchat-folder-delete-row"
                      data-testid={`sidebar-folder-delete-confirm-${folder}`}
                      role="alertdialog"
                      aria-label={`Delete folder ${folder}`}
                    >
                      <span className="lmchat-folder-delete-text">
                        Delete "{folder}"? Chats inside become unfoldered.
                      </span>
                      <div className="lmchat-folder-delete-actions">
                        <button
                          type="button"
                          onClick={() => {
                            void handleDeleteFolder(folder);
                          }}
                          className="lmchat-folder-delete-confirm-btn"
                          data-testid={`sidebar-folder-delete-yes-${folder}`}
                        >
                          Delete
                        </button>
                        <button
                          type="button"
                          onClick={() => {
                            setFolderDeleteConfirm(null);
                          }}
                          className="lmchat-folder-delete-cancel-btn"
                          data-testid={`sidebar-folder-delete-no-${folder}`}
                        >
                          Cancel
                        </button>
                      </div>
                    </div>
                  ) : (
                    <FolderDroppable id={folderContainerId(folder)}>
                      <SortableContext
                        items={groupChats.map((c) => c.id)}
                        strategy={verticalListSortingStrategy}
                      >
                        <ChatGroup
                          label={folder ?? ""}
                          chats={groupChats}
                          activeChatId={activeChatId}
                          menuOpen={folderMenuOpen === folder}
                          onMenuToggle={() => {
                            setFolderMenuOpen((cur) =>
                              cur === folder ? null : folder,
                            );
                          }}
                          onRename={() => {
                            if (folder !== null) {
                              setFolderRenameValue(folder);
                              setFolderRenameOpen(folder);
                              setFolderMenuOpen(null);
                            }
                          }}
                          onDelete={() => {
                            if (folder !== null) {
                              setFolderDeleteConfirm(folder);
                              setFolderMenuOpen(null);
                            }
                          }}
                        />
                      </SortableContext>
                    </FolderDroppable>
                  )}
                </div>
              );
            })}
          </DndContext>

          {filtered.length === 0 && !isLoading && (
            <p className="lmchat-hint">
              {filter.trim() !== ""
                ? "No matches"
                : "No chats yet — click + New Chat above."}
            </p>
          )}

          {/* Archived chats — collapsed section, mirrors the Projects
              page's active/archived split. Not part of the DnD tree
              (archived chats aren't reorderable/foldered while archived). */}
          {archivedChats.length > 0 && (
            <details
              className="lmchat-folder-header-row"
              style={{ display: "block", marginTop: "var(--space-group)" }}
              data-testid="sidebar-archived-section"
            >
              <summary className="lmchat-group-label" style={{ cursor: "pointer" }}>
                Archived ({archivedChats.length})
              </summary>
              {archivedChats.map((c) => (
                <ArchivedChatItem
                  key={c.id}
                  chat={c}
                  onUnarchive={() => {
                    unarchiveChat.mutate(c.id, {
                      onSuccess: () => {
                        push({ variant: "success", message: "Chat unarchived." });
                      },
                      onError: () => {
                        push({
                          variant: "error",
                          message: "Couldn't unarchive that chat.",
                        });
                      },
                    });
                  }}
                />
              ))}
            </details>
          )}
        </nav>
      )}

      {/* Library nav — global page links (Settings + reading rooms).
          Horizontal icon strip; persists across both viewModes so these
          remain reachable at all times. */}
      {!collapsed && (
        <>
          <div className="lmchat-library-nav-divider" aria-hidden="true" />
          <nav
            aria-label="Library"
            className="lmchat-library-nav lmchat-library-nav--strip"
          >
            {(
              [
                {
                  to: "/settings",
                  icon: <Settings size={16} aria-hidden />,
                  label: "Settings",
                },
                {
                  to: "/memory",
                  icon: <Brain size={16} aria-hidden />,
                  label: "Memory",
                },
                {
                  to: "/documents",
                  icon: <FileText size={16} aria-hidden />,
                  label: "Documents",
                },
                {
                  to: "/analytics",
                  icon: <BarChart2 size={16} aria-hidden />,
                  label: "Analytics",
                },
                {
                  to: "/prompts",
                  icon: <BookOpen size={16} aria-hidden />,
                  label: "Prompts",
                },
                {
                  to: "/docs",
                  icon: <Library size={16} aria-hidden />,
                  label: "Docs",
                },
                {
                  to: "/help",
                  icon: <HelpCircle size={16} aria-hidden />,
                  label: "Help",
                },
              ] as const
            ).map(({ to, icon, label }) => (
              <Link
                key={to}
                to={to}
                aria-label={label}
                title={label}
                className={`lmchat-library-nav-item lmchat-sidebar-item${location.pathname === to || location.pathname.startsWith(to + "/") ? " lmchat-library-nav-item--active" : ""}`}
              >
                <span className="lmchat-library-nav-icon">{icon}</span>
              </Link>
            ))}
          </nav>
        </>
      )}

      {/* Stats footer — leather material layer, CHAPTER above.
           Collapsed sidebar drops this row to maintain the 48px icon-rail.
           Hidden in projects-mode so the pane focuses on the project list. */}
      {!collapsed && stats.isReady && (
        <div
          className="lmchat-stats-footer lmchat-stats-footer--compact"
          data-testid="sidebar-stats-footer"
          title={
            stats.streamingTps !== null
              ? `Today: ${formatTokens(stats.tokensToday)} tokens · ${String(stats.streamingTps)} tok/s · saved vs cloud`
              : `Today: ${formatTokens(stats.tokensToday)} tokens · saved vs cloud`
          }
        >
          {/* Collapsed 3-row footer into a single muted line.
              Avg speed shows up inline only when there's a real number to
              report (no more dash-only row taking vertical space). */}
          <span className="lmchat-stats-token">
            {formatTokens(stats.tokensToday)} today
          </span>
          {stats.streamingTps !== null && (
            <>
              <span className="lmchat-stats-sep" aria-hidden>
                ·
              </span>
              <span className="lmchat-stats-speed">
                {String(stats.streamingTps)} tok/s
              </span>
            </>
          )}
          <span className="lmchat-stats-sep" aria-hidden>
            ·
          </span>
          <span title="Estimated cost saved by running locally instead of a cloud frontier model">
            <AnimatedSavedCounter
              valueUsd={stats.approxSavedUsd}
              style={savedValueInlineStyle}
            />
          </span>
        </div>
      )}

      {/* User menu footer + keyboard-help button.
           Collapsed sidebar hides both rows to maintain the 48px icon-rail. */}
      {!collapsed && (
        <div className="lmchat-footer-row">
          <UserMenu
            onSignOut={handleSignOut}
            onSettings={() => {
              void navigate("/settings");
            }}
          />
          {onShowKeyboardHelp !== undefined && (
            <button
              type="button"
              aria-label="Keyboard shortcuts"
              title="Keyboard shortcuts (press ?)"
              onClick={onShowKeyboardHelp}
              className="lmchat-help-btn"
              data-testid="sidebar-keyboard-help-btn"
            >
              ?
            </button>
          )}
        </div>
      )}

      {/* ARIA live region — announces drag-and-drop status to screen readers. */}
      <div
        role="status"
        aria-live="assertive"
        aria-atomic="true"
        style={srOnlyStyle}
      >
        {announcement}
      </div>
    </aside>
  );
}

// ─── DnD multi-container helpers ─────────────────────────────────────────────

/** Container id for the Pinned section's useDroppable target. */
export const PINNED_CONTAINER_ID = "sidebar-container:pinned";
/** Prefix for every folder's useDroppable container id (ungrouped included —
 * the ungrouped bucket uses folder key `null`, encoded as a fixed suffix so
 * it can never collide with a real, user-chosen folder name). */
const FOLDER_CONTAINER_PREFIX = "sidebar-container:folder:";
const UNGROUPED_CONTAINER_SUFFIX = "__ungrouped__";

/** Build the useDroppable container id for a folder (`null` = ungrouped). */
export function folderContainerId(folder: string | null): string {
  return `${FOLDER_CONTAINER_PREFIX}${folder ?? UNGROUPED_CONTAINER_SUFFIX}`;
}

export interface DropTargetContext {
  pinned: ChatSummary[];
  folderMap: Map<string | null, ChatSummary[]>;
}

export interface DropTarget {
  /** Folder value to persist — null covers both the ungrouped bucket and
   * the Pinned section (matching the pre-existing convention: reordering
   * within Pinned always persists folder=null). */
  folder: string | null;
  /** 0-based position within the resolved bucket. */
  display_order: number;
}

/**
 * Resolve the (folder, display_order) PATCH target for a DnD drop, given
 * the raw `over.id` from a dnd-kit `DragEndEvent` and the current
 * pinned/folder partitions.
 *
 * Pure + exported so it's unit-testable without a full DnD harness — this
 * captures ALL the folder-resolution logic that would otherwise live inline,
 * duplicated, in each per-container `handleDragEnd` closure. Those
 * per-container closures could only ever resolve `over` ids from their OWN
 * container, making cross-folder drops structurally impossible.
 *
 * `over.id` is one of:
 *  - a folder-container id (``PINNED_CONTAINER_ID`` or
 *    ``folderContainerId(name)``) — dropped onto an EMPTY folder, or onto
 *    a folder's header/whitespace rather than a specific row. Lands at the
 *    front of that bucket (display_order 0).
 *  - a chat id — dropped between/onto existing rows. The chat's OWN
 *    bucket (pinned, or whichever folder currently contains it) and index
 *    become the target. This covers same-folder reorder (pre-existing
 *    behavior, unchanged) AND cross-folder drops onto an existing row
 *    (new).
 *
 * Returns null when `over` can't be resolved to any known bucket (the
 * drop is cancelled).
 */
export function resolveDropTarget(
  overId: string | number,
  ctx: DropTargetContext,
): DropTarget | null {
  const overIdStr = String(overId);

  if (overIdStr === PINNED_CONTAINER_ID) {
    return { folder: null, display_order: 0 };
  }
  if (overIdStr.startsWith(FOLDER_CONTAINER_PREFIX)) {
    const raw = overIdStr.slice(FOLDER_CONTAINER_PREFIX.length);
    const folder = raw === UNGROUPED_CONTAINER_SUFFIX ? null : raw;
    return { folder, display_order: 0 };
  }

  const overChatId = Number(overId);
  if (Number.isNaN(overChatId)) return null;

  const pinnedIdx = ctx.pinned.findIndex((c) => c.id === overChatId);
  if (pinnedIdx !== -1) {
    return { folder: null, display_order: pinnedIdx };
  }
  for (const [folder, group] of ctx.folderMap.entries()) {
    const idx = group.findIndex((c) => c.id === overChatId);
    if (idx !== -1) {
      return { folder, display_order: idx };
    }
  }
  return null;
}

/**
 * Wraps a Pinned/folder container in `useDroppable` so it's a valid drop
 * target even when it holds zero sortable items (an EMPTY folder — without
 * this an empty folder has no droppable registered anywhere, so
 * there is nothing to aim a drag at). A subtle outline marks the
 * container while a drag is over it.
 */
function FolderDroppable({
  id,
  children,
}: {
  id: string;
  children: ReactNode;
}) {
  const { setNodeRef, isOver } = useDroppable({ id });
  const style: CSSProperties = isOver
    ? {
        outline: "2px dashed var(--color-accent, currentColor)",
        outlineOffset: "-2px",
        borderRadius: "var(--radius-sm, 4px)",
      }
    : {};
  return (
    <div ref={setNodeRef} style={style} data-testid={`sidebar-droppable-${id}`}>
      {children}
    </div>
  );
}

// ─── ChatGroup ───────────────────────────────────────────────────────────────

interface ChatGroupProps {
  label: string;
  chats: ChatSummary[];
  activeChatId: number | null;
  /**
   * When set, the group header renders a 3-dot menu trigger.
   * ``menuOpen`` controls whether the dropdown is visible; the parent
   * tracks open state so only one menu can be open at a time.
   */
  menuOpen?: boolean;
  onMenuToggle?: () => void;
  onRename?: () => void;
  onDelete?: () => void;
}

function ChatGroup({
  label,
  chats,
  activeChatId,
  menuOpen,
  onMenuToggle,
  onRename,
  onDelete,
}: ChatGroupProps) {
  const showMenu = label !== "" && onMenuToggle !== undefined;
  return (
    <div>
      {label !== "" && (
        <div className="lmchat-folder-header-row">
          <p className="lmchat-group-label">{label}</p>
          {showMenu && (
            <div style={{ position: "relative" }}>
              <button
                type="button"
                aria-label={`${label} folder actions`}
                aria-haspopup="menu"
                aria-expanded={menuOpen === true}
                onClick={onMenuToggle}
                className="lmchat-folder-menu-btn"
                data-testid={`sidebar-folder-menu-${label}`}
              >
                ⋯
              </button>
              {menuOpen === true && (
                <div
                  role="menu"
                  className="lmchat-folder-menu-dropdown"
                  data-testid={`sidebar-folder-menu-dropdown-${label}`}
                >
                  <button
                    role="menuitem"
                    type="button"
                    onClick={onRename}
                    className="lmchat-folder-menu-item"
                    data-testid={`sidebar-folder-rename-${label}`}
                  >
                    Rename
                  </button>
                  <button
                    role="menuitem"
                    type="button"
                    onClick={onDelete}
                    className="lmchat-folder-menu-item"
                    style={{ color: "var(--color-danger)" }}
                    data-testid={`sidebar-folder-delete-${label}`}
                  >
                    Delete
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      )}
      {chats.map((c) => (
        <SortableChatItem key={c.id} chat={c} active={activeChatId === c.id} />
      ))}
    </div>
  );
}

// ─── ArchivedChatItem ──────────────────────────────────────────────────────
//
// Simple (non-DnD) row for the "Archived" section — archived chats aren't
// reorderable/foldered, so this skips useSortable entirely rather than
// carrying dead drag machinery. Mirrors Projects.tsx's ProjectCard.

interface ArchivedChatItemProps {
  chat: ChatSummary;
  onUnarchive: () => void;
}

function ArchivedChatItem({ chat, onUnarchive }: ArchivedChatItemProps) {
  return (
    <div
      className="lmchat-chat-item lmchat-chat-item--inactive lmchat-sidebar-item"
      data-testid={`sidebar-archived-chat-${String(chat.id)}`}
      style={{ opacity: 0.75 }}
    >
      <span
        style={{
          flex: 1,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {chat.title || "New Chat"}
      </span>
      <button
        type="button"
        aria-label={`Unarchive "${chat.title}"`}
        title="Unarchive"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          onUnarchive();
        }}
        className="lmchat-chat-row-delete-btn"
        data-testid={`sidebar-chat-${String(chat.id)}-unarchive`}
      >
        <ArchiveRestore size={14} aria-hidden />
      </button>
    </div>
  );
}

// ─── SortableChatItem ────────────────────────────────────────────────────────

interface SortableChatItemProps {
  chat: ChatSummary;
  active: boolean;
}

function SortableChatItem({ chat, active }: SortableChatItemProps) {
  const {
    attributes,
    listeners,
    setNodeRef,
    transform,
    transition,
    isDragging,
  } = useSortable({ id: chat.id });

  // Per-row hover actions — Move to folder + Delete chat.
  // The previous hover-revealed move-to-PROJECT menu overlapped the title
  // text and was philosophically out of place — projects-management lives
  // inside the project view, not on every chat row (that's still reachable
  // from the chat detail's More actions menu). Move-to-FOLDER is different:
  // it's the reliable, keyboard/mobile-accessible fallback for the one
  // thing DnD alone can't guarantee — moving a chat into a folder —
  // so it lives here, next to Delete.
  const deleteChat = useDeleteChat();
  const reorderChat = useReorderChat();
  const updateChat = useUpdateChat(chat.id);
  const archiveChat = useArchiveChat();
  const push = useToastStore((s) => s.push);
  const navigate = useNavigate();

  // Sub-sessions are ephemeral — don't expose destructive actions on
  // a row that's showing one. The same guard the move affordance used.
  const subSessionActiveChatId = useSubSessionStore((s) => s.activeChatId);
  const hideRowActions = subSessionActiveChatId === chat.id;

  function handleDeleteClick(e: ReactMouseEvent): void {
    e.preventDefault();
    e.stopPropagation();
    // The in-button two-step confirm was easy to mis-click and didn't
    // read as a confirmation prompt. Push a toast-styled prompt instead
    // — the toast.action button confirms the delete, the toast's
    // built-in dismiss (X) cancels.
    const titleLabel =
      chat.title && chat.title.trim() !== "" ? chat.title : "this chat";
    push({
      variant: "warning",
      title: "Delete chat?",
      message: `"${titleLabel}" will be removed from the list. This can't be undone.`,
      duration: 8_000,
      action: {
        label: "Delete",
        onClick: () => {
          deleteChat.mutate(chat.id, {
            onSuccess: () => {
              push({ variant: "success", message: "Chat deleted." });
              // Navigate away when the user deletes the chat they're
              // currently viewing — leaving them on a 404'd chat URL
              // is surprising and the message list shows nothing.
              if (active) void navigate("/");
            },
            onError: () => {
              push({ variant: "error", message: "Couldn't delete that chat." });
            },
          });
        },
      },
    });
  }

  // Picking a folder (or "Remove from folder") from the MoveToFolderMenu.
  // display_order uses a large sentinel — chat_service.reorder clamps it
  // to the target bucket's length, i.e. append at the end, rather than
  // requiring this row to know the target bucket's current size.
  function handleMoveToFolder(folder: string | null): void {
    reorderChat.mutate(
      { chat_id: chat.id, folder, display_order: Number.MAX_SAFE_INTEGER },
      {
        onSuccess: () => {
          push({
            variant: "success",
            message:
              folder === null
                ? `Removed "${chat.title || "chat"}" from its folder.`
                : `Moved "${chat.title || "chat"}" to "${folder}".`,
          });
        },
        onError: () => {
          push({ variant: "error", message: "Couldn't move that chat." });
        },
      },
    );
  }

  // Replace the chat's whole tag list (ChatTagsMenu computes the desired
  // end state — add or remove — and hands back the full array).
  function handleTagsChange(tags: string[]): void {
    updateChat.mutate(
      { tags },
      {
        onError: () => {
          push({ variant: "error", message: "Couldn't update tags." });
        },
      },
    );
  }

  // Archive is reversible (see the sidebar's "Archived" section), so it
  // fires immediately without a delete-style confirm prompt.
  function handleArchiveClick(e: ReactMouseEvent): void {
    e.preventDefault();
    e.stopPropagation();
    archiveChat.mutate(chat.id, {
      onSuccess: () => {
        push({ variant: "success", message: "Chat archived." });
        if (active) void navigate("/");
      },
      onError: () => {
        push({ variant: "error", message: "Couldn't archive that chat." });
      },
    });
  }

  // Incognito chats render dimmer + with an eye-with-slash glyph
  // so the user can tell at a glance which chats will be swept on
  // logout / TTL expiry.
  const isIncognito = chat.incognito === true;
  // Render an italic, dimmed "Generating title…"
  // placeholder while the backend titler is in flight for this chat.
  const isGeneratingTitle = useTitleGenerationStore((s) => s.has(chat.id));

  const dndStyle: CSSProperties = {
    transform: CSS.Transform.toString(transform),
    transition,
    opacity: isDragging ? 0.5 : isIncognito ? 0.65 : 1,
    position: "relative",
  };

  // Display title: while a backend auto-title is in flight AND the chat
  // is still at a default title, show a generating placeholder.  Once the
  // mutation lands and the chat list cache is patched, this falls back to
  // the real title naturally.
  const titleIsDefault =
    chat.title === "" ||
    chat.title === "New Chat" ||
    chat.title === "Incognito Chat";
  const showPlaceholder = isGeneratingTitle && titleIsDefault;
  const displayTitle = showPlaceholder ? "Generating title…" : chat.title;

  const titleStyle: CSSProperties = {
    flex: 1,
    overflow: "hidden",
    textOverflow: "ellipsis",
    whiteSpace: "nowrap",
    fontStyle: showPlaceholder || isIncognito ? "italic" : "normal",
    opacity: showPlaceholder ? 0.7 : 1,
    transition: "opacity 200ms ease",
  };

  return (
    <div
      ref={setNodeRef}
      style={dndStyle}
      data-incognito={isIncognito ? "1" : "0"}
    >
      {/* Drag handle — keyboard: Tab to focus, Space/Enter to grab,
           Arrow keys to move, Space/Enter or Escape to drop/cancel. */}
      <span
        {...attributes}
        {...listeners}
        className="lmchat-drag-handle"
        aria-label={`Reorder: ${chat.title}`}
        aria-roledescription="sortable"
        role="button"
        tabIndex={0}
      >
        <span aria-hidden="true">⠿</span>
      </span>
      <Link
        to={`/chats/${String(chat.id)}`}
        className={`lmchat-chat-item ${active ? "lmchat-chat-item--active" : "lmchat-chat-item--inactive lmchat-sidebar-item"}`}
        aria-label={isIncognito ? `${chat.title} (incognito)` : chat.title}
      >
        {isIncognito && (
          <span
            aria-hidden="true"
            title="Incognito chat — purged on logout or TTL expiry"
            className="lmchat-incognito-badge"
            data-testid="sidebar-incognito-badge"
          >
            <Lock size={11} aria-hidden />
          </span>
        )}
        <span
          style={titleStyle}
          data-testid={
            showPlaceholder ? "sidebar-title-generating" : "sidebar-title"
          }
        >
          {displayTitle}
        </span>
        {chat.tags.length > 0 && (
          <span
            className="lmchat-chat-meta"
            title={chat.tags.join(", ")}
            data-testid={`sidebar-chat-${String(chat.id)}-tags-badge`}
            style={{ display: "inline-flex", alignItems: "center", gap: 2 }}
          >
            <TagIcon size={11} aria-hidden />
            {chat.tags.length}
          </span>
        )}
        <span className="lmchat-chat-meta">
          {relativeTime(chat.updated_at)}
        </span>
      </Link>
      {/* Per-row hover actions — Move to folder + Delete chat. Delete's
          two-step click confirms before firing the mutation — destructive
          actions without a confirm are a recipe for lost work. The
          chat-row reserves right padding so the icons never overlap the
          title. */}
      {!hideRowActions && (
        <span className="lmchat-chat-row-actions">
          <MoveToFolderMenu
            currentFolder={chat.folder}
            onPick={handleMoveToFolder}
            testIdPrefix={`sidebar-chat-${String(chat.id)}-move-to-folder`}
            ariaLabel={`Move "${chat.title}" to folder`}
          />
          <ChatTagsMenu
            tags={chat.tags}
            onChange={handleTagsChange}
            testIdPrefix={`sidebar-chat-${String(chat.id)}-tags`}
            ariaLabel={`Edit tags for "${chat.title}"`}
          />
          <button
            type="button"
            className="lmchat-chat-row-delete-btn"
            aria-label={`Archive "${chat.title}"`}
            title={`Archive "${chat.title}"`}
            onClick={handleArchiveClick}
            data-testid={`sidebar-chat-${String(chat.id)}-archive`}
          >
            <Archive size={14} aria-hidden />
          </button>
          <button
            type="button"
            className="lmchat-chat-row-delete-btn"
            aria-label={`Delete "${chat.title}"`}
            title={`Delete "${chat.title}"`}
            onClick={handleDeleteClick}
            data-testid={`sidebar-chat-${String(chat.id)}-delete`}
          >
            <Trash2 size={14} aria-hidden />
          </button>
        </span>
      )}
    </div>
  );
}

// ─── Helpers ─────────────────────────────────────────────────────────────────

function relativeTime(iso: string): string {
  const diff = Date.now() - new Date(iso).getTime();
  const min = Math.floor(diff / 60_000);
  if (min < 1) return "now";
  if (min < 60) return `${String(min)}m`;
  const hr = Math.floor(min / 60);
  if (hr < 24) return `${String(hr)}h`;
  const day = Math.floor(hr / 24);
  return `${String(day)}d`;
}

// ─── Minimal inline styles (only what CSS classes cannot express) ─────────────

/**
 * Shell geometry: width/min-width are JS-driven (collapsed state).
 * Everything else (background, border, flex direction, overflow) lives
 * in sidebar.css via .lmchat-sidebar.
 * mobile=true → 100% width (fills the drawer shell), no right border.
 */
function sidebarShellStyle(collapsed: boolean, mobile = false): CSSProperties {
  if (mobile) {
    return {
      width: "100%",
      minWidth: 0,
      // Use flex:1 + minHeight:0 instead of height:100% so the aside
      // fills only the remaining space after the drawer header, not the full
      // shell height. height:100% + a 69px drawer header = 637px total in a
      // 568px shell, causing the shell to scroll and the footer to clip.
      flex: 1,
      minHeight: 0,
      background: "transparent",
      borderRight: "none",
      display: "flex",
      flexDirection: "column",
      overflow: "hidden",
      flexShrink: 0,
    };
  }
  return {
    // 240px was tight — the per-row delete button on the right edge sat
    // with only 4px of breathing room and the chat titles wrapped earlier
    // than they should. Bumped to 272px to give the hover-action column
    // real space without making the sidebar feel chunky.
    width: collapsed ? "48px" : "272px",
    minWidth: collapsed ? "48px" : "272px",
    height: "100%",
    background: "var(--color-surface)",
    borderRight: "1px solid var(--color-border)",
    display: "flex",
    flexDirection: "column",
    overflow: "hidden",
    flexShrink: 0,
  };
}

/** AnimatedSavedCounter takes a `style` prop — pass the mint value style here. */
const savedValueInlineStyle: CSSProperties = {
  fontFamily: "var(--font-display)",
  fontSize: "var(--fs-label)",
  fontWeight: 700,
  color: "var(--color-success)",
  fontVariantNumeric: "tabular-nums",
};

/** Screen-reader-only: visually hidden but accessible to AT. */
const srOnlyStyle: CSSProperties = {
  position: "absolute",
  width: "1px",
  height: "1px",
  padding: 0,
  margin: "-1px",
  overflow: "hidden",
  clip: "rect(0, 0, 0, 0)",
  whiteSpace: "nowrap",
  border: 0,
};
