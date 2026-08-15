/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Memory page unit tests — render + pin/unpin interaction.
 *
 * Locked behaviours:
 *   - Empty pin list renders the empty-state copy and example card.
 *   - A populated pin list renders one row per insight with the body text.
 *   - Typing into the input and submitting calls usePinInsight().mutateAsync
 *     and pushes a success toast on resolution.
 *   - Empty-text submit is blocked: the mutation is not invoked.
 *   - Clicking the unpin button on a row calls useUnpinInsight().mutateAsync
 *     with the row id.
 *
 * useMemory hooks, useModels, useToast, and Sidebar (via AppShell) are
 * mocked at module level so the suite does not need a TanStack Query
 * provider and does not pull live sidebar data.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
} from "@testing-library/react";
import { createElement } from "react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

import type { MemoryInsight } from "@/hooks/useMemory";

// ─── Mock toast store ────────────────────────────────────────────────────────

const mockPush = vi.fn();
vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: mockPush, dismiss: vi.fn() }),
  useToastStore: { getState: () => ({ push: mockPush, dismiss: vi.fn() }) },
}));

// ─── Mock authStore — mutable so individual tests can flip is_admin to
//     exercise the admin-only reindex picker. Default: non-admin alice.

const mockAuthState: {
  user: { id: number; username: string; is_admin: boolean; totp_enabled: boolean };
  isInitializing: boolean;
} = {
  user: { id: 1, username: "alice", is_admin: false, totp_enabled: false },
  isInitializing: false,
};
vi.mock("@/stores/authStore", () => ({
  useAuthStore: () => mockAuthState,
}));

// ─── Mock useMemory hooks ────────────────────────────────────────────────────

const mockPinMutate = vi.fn();
const mockUnpinMutate = vi.fn();
const mockEditMutate = vi.fn();
const mockReindexMutate = vi.fn();
const mockRefineMutate = vi.fn();
const mockRestoreMutate = vi.fn();
interface MockMemoryQueryResult {
  data: MemoryInsight[];
  isLoading: boolean;
  isError: boolean;
}

const mockUseMemoryPins = vi.fn<() => MockMemoryQueryResult>();
const mockUseAutoMemories = vi.fn<() => MockMemoryQueryResult>();

vi.mock("@/hooks/useMemory", () => ({
  useMemoryPins: () => mockUseMemoryPins(),
  useAutoMemories: () => mockUseAutoMemories(),
  usePinInsight: () => ({ mutateAsync: mockPinMutate, isPending: false }),
  useUnpinInsight: () => ({ mutateAsync: mockUnpinMutate, isPending: false }),
  useEditInsight: () => ({ mutateAsync: mockEditMutate, isPending: false }),
  useMemoryReindex: () => ({ mutateAsync: mockReindexMutate, isPending: false }),
  useRefineMemory: () => ({ mutateAsync: mockRefineMutate, isPending: false }),
  useRestoreMemory: () => ({ mutateAsync: mockRestoreMutate, isPending: false }),
}));

// ─── Mock useModels (Memory pulls it for the admin reindex dropdown) ────────
//     Mutable so the reindex-picker test can supply loaded + unloaded
//     embedders and assert the not-loaded options are disabled.

const mockModelsState: { data: { models: unknown[] }; isLoading: boolean } = {
  data: { models: [] },
  isLoading: false,
};
vi.mock("@/hooks/useModels", () => ({
  useModels: () => mockModelsState,
}));

// ─── Mock useEmbeddingStatus (Memory auto-defaults the reindex picker
//     to the active embedding model; without this the hook tries to
//     read from a real QueryClient that the test harness doesn't set up).
const mockEmbeddingStatusState: { data: unknown; isLoading: boolean; isError: boolean } = {
  data: undefined,
  isLoading: false,
  isError: false,
};
vi.mock("@/hooks/useEmbeddingStatus", () => ({
  useEmbeddingStatus: () => mockEmbeddingStatusState,
}));

// ─── Mock Sidebar (AppShell pulls it in) ─────────────────────────────────────

vi.mock("@/components/Sidebar", () => ({
  Sidebar: () =>
    createElement("div", { "data-testid": "mock-sidebar" }, "Sidebar"),
}));

// ─── Fixtures ────────────────────────────────────────────────────────────────

const PIN_A: MemoryInsight = {
  id: 1,
  text: "Prefers concise responses with TypeScript examples.",
  created_at: "2026-01-01T00:00:00Z",
  pinned: true,
};

const PIN_B: MemoryInsight = {
  id: 2,
  text: "Working on lm-chat v1.",
  created_at: "2026-01-02T00:00:00Z",
  pinned: true,
};

const AUTO_A: MemoryInsight = {
  id: 10,
  text: "Name is Kevin.",
  created_at: "2026-02-01T00:00:00Z",
  pinned: false,
};

async function freshMemory() {
  vi.resetModules();
  const mod = await import("@/pages/Memory");
  return mod.default;
}

function renderMemory(Page: React.ComponentType) {
  return render(
    <MemoryRouter initialEntries={["/memory"]}>
      <Routes>
        <Route path="/memory" element={<Page />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("Memory", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mockPush.mockClear();
    mockPinMutate.mockReset();
    mockUnpinMutate.mockReset();
    mockUseMemoryPins.mockReset();
    mockUseAutoMemories.mockReset();
    // Default: no auto (distilled) memories. Tests that exercise the
    // "Remembered automatically" section override this.
    mockUseAutoMemories.mockReturnValue({
      data: [],
      isLoading: false,
      isError: false,
    });
    // Restore mutable mock-state defaults (plain objects aren't reset by
    // vi.resetAllMocks()).
    mockAuthState.user = {
      id: 1,
      username: "alice",
      is_admin: false,
      totp_enabled: false,
    };
    mockAuthState.isInitializing = false;
    mockModelsState.data = { models: [] };
    mockModelsState.isLoading = false;
    mockEmbeddingStatusState.data = undefined;
    mockEmbeddingStatusState.isLoading = false;
    mockEmbeddingStatusState.isError = false;
    cleanup();
  });

  it("renders the empty-state when no pins exist", async () => {
    mockUseMemoryPins.mockReturnValue({ data: [], isLoading: false, isError: false });
    const Page = await freshMemory();
    renderMemory(Page);

    expect(screen.getByTestId("memory-empty-state")).toBeTruthy();
    expect(screen.getByText("Nothing remembered yet.")).toBeTruthy();
    expect(screen.getByPlaceholderText(/Add an insight to pin/)).toBeTruthy();
  });

  it("renders a row per pin when the list is populated", async () => {
    mockUseMemoryPins.mockReturnValue({
      data: [PIN_A, PIN_B],
      isLoading: false,
      isError: false,
    });
    const Page = await freshMemory();
    renderMemory(Page);

    expect(screen.getByTestId("memory-insight-1")).toBeTruthy();
    expect(screen.getByTestId("memory-insight-2")).toBeTruthy();
    expect(screen.getByText(PIN_A.text)).toBeTruthy();
    expect(screen.getByText(PIN_B.text)).toBeTruthy();
  });

  it("submitting the pin form calls usePinInsight and toasts on success", async () => {
    mockUseMemoryPins.mockReturnValue({ data: [], isLoading: false, isError: false });
    mockPinMutate.mockResolvedValue({ id: 3, text: "new", created_at: "2026-01-03T00:00:00Z", pinned: true });
    const Page = await freshMemory();
    renderMemory(Page);

    const input = screen.getByLabelText("New insight");
    fireEvent.change(input, { target: { value: "Brand new insight" } });
    fireEvent.click(screen.getByRole("button", { name: "Pin" }));

    await waitFor(() => {
      expect(mockPinMutate).toHaveBeenCalledWith({ text: "Brand new insight" });
    });
    await waitFor(() => {
      expect(mockPush).toHaveBeenCalledWith(
        expect.objectContaining({ variant: "success" }),
      );
    });
  });

  it("blocks empty-text submits — pin mutation is not called", async () => {
    mockUseMemoryPins.mockReturnValue({ data: [], isLoading: false, isError: false });
    const Page = await freshMemory();
    renderMemory(Page);

    // The submit button is disabled when input is empty; clicking it should
    // not call the mutation even if a click is forced.
    const pinBtn = screen.getByRole("button", { name: "Pin" });
    expect(pinBtn.hasAttribute("disabled")).toBe(true);

    // Defensive: even firing the form's submit event with empty text
    // short-circuits before the mutation.
    fireEvent.click(pinBtn);
    expect(mockPinMutate).not.toHaveBeenCalled();
  });

  it("clicking the unpin button on a row calls useUnpinInsight with that id", async () => {
    mockUseMemoryPins.mockReturnValue({
      data: [PIN_A],
      isLoading: false,
      isError: false,
    });
    mockUnpinMutate.mockResolvedValue(undefined);
    const Page = await freshMemory();
    renderMemory(Page);

    const unpinBtn = screen.getByLabelText(`Unpin: ${PIN_A.text}`);
    fireEvent.click(unpinBtn);

    await waitFor(() => {
      expect(mockUnpinMutate).toHaveBeenCalledWith(PIN_A.id);
    });
  });

  it("admin reindex picker disables not-loaded embedders and marks the active one", async () => {
    // Admin sees the reindex picker. A downloaded-but-unloaded embedder must
    // be DISABLED (reindexing under it fails; pinning one is what silently
    // killed memory on 2026-06-25). The active embedder carries `· active`.
    mockAuthState.user = {
      id: 1,
      username: "alice",
      is_admin: true,
      totp_enabled: false,
    };
    mockModelsState.data = {
      models: [
        {
          id: "text-embedding-nomic-embed-text-v1.5",
          name: "text-embedding-nomic-embed-text-v1.5",
          loaded: true,
          capabilities: { embedding: true },
        },
        {
          id: "text-embedding-nomic-embed-text-v1.5@q8_0",
          name: "text-embedding-nomic-embed-text-v1.5@q8_0",
          loaded: false, // downloaded only — must be disabled
          capabilities: { embedding: true },
        },
      ],
    };
    mockEmbeddingStatusState.data = {
      active_model_id: "text-embedding-nomic-embed-text-v1.5",
      loaded_embedding_models: ["text-embedding-nomic-embed-text-v1.5"],
      total_indexed_messages: 0,
      last_indexed_at: null,
      models_in_use: {},
      embedding_status: "ok",
    };
    mockUseMemoryPins.mockReturnValue({
      data: [PIN_A],
      isLoading: false,
      isError: false,
    });
    const Page = await freshMemory();
    renderMemory(Page);

    const select = (await screen.findByTestId(
      "memory-reindex-model-select",
    )) as HTMLSelectElement;
    const byValue = (v: string): HTMLOptionElement => {
      const opt = Array.from(select.options).find((o) => o.value === v);
      if (opt === undefined) {
        throw new Error(`expected an <option value="${v}"> in the reindex model select`);
      }
      return opt;
    };

    // Loaded model: enabled, marked active.
    const loaded = byValue("text-embedding-nomic-embed-text-v1.5");
    expect(loaded.disabled).toBe(false);
    expect(loaded.textContent).toContain("· loaded");
    expect(loaded.textContent).toContain("· active");

    // Unloaded quant variant: DISABLED + labelled not loaded.
    const unloaded = byValue("text-embedding-nomic-embed-text-v1.5@q8_0");
    expect(unloaded.disabled).toBe(true);
    expect(unloaded.textContent).toContain("· not loaded");
    expect(unloaded.textContent).not.toContain("· active");
  });

  // Bug B (2026-07-18 dogfood): the reindex embedder dropdown visibly
  // rendered "Nomic Embed Text v1.5" three times. `loaded_embedding_models`
  // is polled independently of /api/models and isn't guaranteed unique on
  // its own — a repeated id here (for a model /api/models doesn't report at
  // all, so the existing cross-list filter can't catch it) must collapse
  // to a single <option>, not render once per array entry.
  it("test_Memory_reindex_picker_dedupes_repeated_status_ids: a duplicated loaded_embedding_models entry renders once", async () => {
    mockAuthState.user = {
      id: 1,
      username: "alice",
      is_admin: true,
      totp_enabled: false,
    };
    // /api/models doesn't know about this embedder at all — only the
    // independently-polled embedding-status snapshot reports it, and it
    // reports the SAME id three times.
    mockModelsState.data = { models: [] };
    mockEmbeddingStatusState.data = {
      active_model_id: "text-embedding-nomic-embed-text-v1.5",
      loaded_embedding_models: [
        "text-embedding-nomic-embed-text-v1.5",
        "text-embedding-nomic-embed-text-v1.5",
        "text-embedding-nomic-embed-text-v1.5",
      ],
      total_indexed_messages: 0,
      last_indexed_at: null,
      models_in_use: {},
      embedding_status: "ok",
    };
    mockUseMemoryPins.mockReturnValue({
      data: [PIN_A],
      isLoading: false,
      isError: false,
    });
    const Page = await freshMemory();
    renderMemory(Page);

    const select = (await screen.findByTestId(
      "memory-reindex-model-select",
    )) as HTMLSelectElement;
    const matches = Array.from(select.options).filter(
      (o) => o.value === "text-embedding-nomic-embed-text-v1.5",
    );
    expect(matches).toHaveLength(1);
  });

  it("renders the 'Remembered automatically' section for auto memories", async () => {
    mockUseMemoryPins.mockReturnValue({
      data: [],
      isLoading: false,
      isError: false,
    });
    mockUseAutoMemories.mockReturnValue({
      data: [AUTO_A],
      isLoading: false,
      isError: false,
    });
    const Page = await freshMemory();
    renderMemory(Page);

    // The auto section renders, labelled and distinct from pins.
    expect(screen.getByTestId("memory-auto-section")).toBeTruthy();
    expect(screen.getByText("Remembered automatically")).toBeTruthy();
    expect(screen.getByTestId("memory-auto-10")).toBeTruthy();
    expect(screen.getByText("Name is Kevin.")).toBeTruthy();
    // An auto-only library is NOT the empty state.
    expect(screen.queryByTestId("memory-empty-state")).toBeNull();
  });

  it("forgetting an auto memory calls useUnpinInsight with that id", async () => {
    mockUseMemoryPins.mockReturnValue({
      data: [],
      isLoading: false,
      isError: false,
    });
    mockUseAutoMemories.mockReturnValue({
      data: [AUTO_A],
      isLoading: false,
      isError: false,
    });
    mockUnpinMutate.mockResolvedValue({ status: "ok" });
    const Page = await freshMemory();
    renderMemory(Page);

    fireEvent.click(screen.getByTestId("memory-auto-forget-10"));
    await waitFor(() => {
      expect(mockUnpinMutate).toHaveBeenCalledWith(10);
    });
  });
});
