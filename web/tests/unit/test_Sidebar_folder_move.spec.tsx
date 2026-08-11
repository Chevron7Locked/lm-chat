/**
 * Unit tests for BUG 4 — "you can create a folder in the sidebar but
 * there is no working way to move a chat into one".
 *
 * Covers:
 *  (a) MoveToFolderMenu — renders folder names from a mocked useFolders;
 *      picking a folder / "Remove from folder" invokes onPick with the
 *      right value. Sidebar.tsx wires that straight into
 *      reorderChat.mutate({ chat_id, folder, display_order }) — the second
 *      test in each pair simulates that exact wiring.
 *  (b) resolveDropTarget — the pure helper Sidebar.tsx's single shared
 *      DndContext uses to resolve a drop's target folder + display_order
 *      from `over.id` (a chat id OR a folder-container id), extracted so
 *      it's testable without a full DnD harness.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";

// ─── Mocks ────────────────────────────────────────────────────────────────────

const mockUseFolders = vi.fn();
vi.mock("@/hooks/useFolders", () => ({
  useFolders: (...args: unknown[]) => mockUseFolders(...args),
}));

beforeEach(() => {
  vi.clearAllMocks();
});

// ─── MoveToFolderMenu ────────────────────────────────────────────────────────

describe("MoveToFolderMenu (BUG 4 — menu path)", () => {
  it("renders folder names from a mocked useFolders", async () => {
    mockUseFolders.mockReturnValue({ data: ["Work", "Personal"] });
    const { MoveToFolderMenu } = await import("@/components/MoveToFolderMenu");

    render(<MoveToFolderMenu currentFolder={null} onPick={vi.fn()} />);

    fireEvent.click(screen.getByTestId("move-to-folder-trigger"));
    await waitFor(() => {
      expect(screen.getByTestId("move-to-folder-menu")).toBeTruthy();
    });
    expect(screen.getByTestId("move-to-folder-pick-Work")).toBeTruthy();
    expect(screen.getByTestId("move-to-folder-pick-Personal")).toBeTruthy();
  });

  it("picking a folder feeds reorderChat.mutate({ chat_id, folder, display_order })", async () => {
    mockUseFolders.mockReturnValue({ data: ["Work", "Personal"] });
    const { MoveToFolderMenu } = await import("@/components/MoveToFolderMenu");
    const mockMutate = vi.fn();
    // Mirrors Sidebar.tsx's SortableChatItem.handleMoveToFolder wiring:
    // onPick(folder) → reorderChat.mutate({ chat_id, folder, display_order }).
    const onPick = (folder: string | null) => {
      mockMutate({
        chat_id: 7,
        folder,
        display_order: Number.MAX_SAFE_INTEGER,
      });
    };

    render(<MoveToFolderMenu currentFolder={null} onPick={onPick} />);

    fireEvent.click(screen.getByTestId("move-to-folder-trigger"));
    const workItem = await screen.findByTestId("move-to-folder-pick-Work");
    fireEvent.click(workItem);

    expect(mockMutate).toHaveBeenCalledWith({
      chat_id: 7,
      folder: "Work",
      display_order: Number.MAX_SAFE_INTEGER,
    });
  });

  it('"Remove from folder" feeds reorderChat.mutate with folder: null', async () => {
    mockUseFolders.mockReturnValue({ data: ["Work"] });
    const { MoveToFolderMenu } = await import("@/components/MoveToFolderMenu");
    const mockMutate = vi.fn();
    const onPick = (folder: string | null) => {
      mockMutate({
        chat_id: 9,
        folder,
        display_order: Number.MAX_SAFE_INTEGER,
      });
    };

    render(<MoveToFolderMenu currentFolder="Work" onPick={onPick} />);

    fireEvent.click(screen.getByTestId("move-to-folder-trigger"));
    const removeItem = await screen.findByTestId("move-to-folder-remove");
    fireEvent.click(removeItem);

    expect(mockMutate).toHaveBeenCalledWith({
      chat_id: 9,
      folder: null,
      display_order: Number.MAX_SAFE_INTEGER,
    });
  });

  it("shows Remove from folder only when the chat is currently foldered", async () => {
    mockUseFolders.mockReturnValue({ data: ["Work"] });
    const { MoveToFolderMenu } = await import("@/components/MoveToFolderMenu");

    render(<MoveToFolderMenu currentFolder={null} onPick={vi.fn()} />);
    fireEvent.click(screen.getByTestId("move-to-folder-trigger"));
    await waitFor(() => {
      expect(screen.getByTestId("move-to-folder-menu")).toBeTruthy();
    });
    expect(screen.queryByTestId("move-to-folder-remove")).toBeNull();
  });

  it("hides entirely when there are no folders and the chat is unfoldered", async () => {
    mockUseFolders.mockReturnValue({ data: [] });
    const { MoveToFolderMenu } = await import("@/components/MoveToFolderMenu");

    const { container } = render(
      <MoveToFolderMenu currentFolder={null} onPick={vi.fn()} />,
    );
    expect(container.firstChild).toBeNull();
  });
});

// ─── resolveDropTarget (pure helper) ────────────────────────────────────────

interface TestChat {
  id: number;
  title: string;
  folder: string | null;
  pinned: boolean;
  updated_at: string;
  model_id: string | null;
  display_order: number;
}

function makeChat(
  id: number,
  folder: string | null,
  display_order: number,
): TestChat {
  return {
    id,
    title: `chat ${String(id)}`,
    folder,
    pinned: false,
    updated_at: "2026-01-01T00:00:00Z",
    model_id: null,
    display_order,
  };
}

describe("resolveDropTarget (pure helper — BUG 4 drag path)", () => {
  it("over = an empty folder's container id → { folder, display_order: 0 }", async () => {
    const { resolveDropTarget, folderContainerId } = await import(
      "@/components/Sidebar"
    );

    const ctx = {
      pinned: [],
      folderMap: new Map<string | null, TestChat[]>([["Empty", []]]),
    };

    expect(resolveDropTarget(folderContainerId("Empty"), ctx)).toEqual({
      folder: "Empty",
      display_order: 0,
    });
  });

  it("over = the ungrouped container id → { folder: null, display_order: 0 }", async () => {
    const { resolveDropTarget, folderContainerId } = await import(
      "@/components/Sidebar"
    );

    const ctx = { pinned: [], folderMap: new Map<string | null, TestChat[]>() };
    expect(resolveDropTarget(folderContainerId(null), ctx)).toEqual({
      folder: null,
      display_order: 0,
    });
  });

  it("over = the pinned container id → { folder: null, display_order: 0 }", async () => {
    const { resolveDropTarget, PINNED_CONTAINER_ID } = await import(
      "@/components/Sidebar"
    );

    const ctx = { pinned: [], folderMap: new Map<string | null, TestChat[]>() };
    expect(resolveDropTarget(PINNED_CONTAINER_ID, ctx)).toEqual({
      folder: null,
      display_order: 0,
    });
  });

  it("over = a chat id in another folder → that folder + correct index", async () => {
    const { resolveDropTarget } = await import("@/components/Sidebar");

    const ctx = {
      pinned: [],
      folderMap: new Map<string | null, TestChat[]>([
        [null, [makeChat(1, null, 0)]],
        ["Work", [makeChat(2, "Work", 0), makeChat(3, "Work", 1)]],
      ]),
    };

    // Chat 3 lives in "Work" at index 1 — dropping the dragged chat onto
    // chat 3 (from anywhere, including a different folder) targets "Work"
    // at that same index. This is the cross-folder case BUG 4's per-
    // container DndContexts made structurally impossible.
    expect(resolveDropTarget(3, ctx)).toEqual({
      folder: "Work",
      display_order: 1,
    });
  });

  it("over = a pinned chat id → { folder: null, display_order: pinned index }", async () => {
    const { resolveDropTarget } = await import("@/components/Sidebar");

    const ctx = {
      pinned: [makeChat(10, null, 0), makeChat(11, "Work", 1)],
      folderMap: new Map<string | null, TestChat[]>(),
    };

    expect(resolveDropTarget(11, ctx)).toEqual({
      folder: null,
      display_order: 1,
    });
  });

  it("over = an unresolvable id → null (drop cancelled)", async () => {
    const { resolveDropTarget } = await import("@/components/Sidebar");

    const ctx = { pinned: [], folderMap: new Map<string | null, TestChat[]>() };
    expect(resolveDropTarget(999, ctx)).toBeNull();
    expect(resolveDropTarget("not-a-real-container-id", ctx)).toBeNull();
  });
});
