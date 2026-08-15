/**
 * Unit tests for AdminModels page.
 *
 * Covers:
 *   - List renders all models from useModels
 *   - Loaded indicator shows "loaded" when loaded, "not loaded" when not
 *   - Action buttons present for each model row
 *   - Load button calls loadModel mutation
 *   - Unload button single-click for N=1 instance
 *   - Unload button N>1 opens instance picker
 *   - "Unload all instances" opens confirmation modal
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { MemoryRouter } from "react-router-dom";

// ------------------------------------------------------------------
// Module-level mocks — must precede imports of the modules under test
// ------------------------------------------------------------------

const mockLoadModel = vi.fn();
const mockUnloadModel = vi.fn();
const mockRefetch = vi.fn();

vi.mock("@/hooks/useModels", () => ({
  useModels: vi.fn(),
  useRefreshModels: vi.fn(() => ({
    mutate: vi.fn(),
    isPending: false,
  })),
  modelKeys: { all: ["models"], list: () => ["models", "list"] },
}));

vi.mock("@/hooks/useModelOps", () => ({
  useLoadModel: vi.fn(() => ({
    mutate: mockLoadModel,
    isPending: false,
    isError: false,
    error: null,
  })),
  useUnloadModel: vi.fn(() => ({
    mutate: mockUnloadModel,
    isPending: false,
    isError: false,
    error: null,
  })),
}));

import { useModels } from "@/hooks/useModels";
import AdminModels from "@/pages/AdminModels";

// ------------------------------------------------------------------
// Helpers
// ------------------------------------------------------------------

function makeQC() {
  return new QueryClient({ defaultOptions: { queries: { retry: false } } });
}

function Wrapper({ children }: { children: ReactNode }) {
  return (
    <QueryClientProvider client={makeQC()}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

/** Indexed array access that throws (rather than silently continuing) if out of range. */
function at<T>(arr: readonly T[], i: number): T {
  const v = arr[i];
  if (v === undefined) {
    throw new Error(`expected element at index ${String(i)}, array has length ${String(arr.length)}`);
  }
  return v;
}

const loadedModel = {
  id: "qwen3-8b",
  name: "Qwen3.6 35B",
  capabilities: { vision: true, trained_for_tool_use: true },
  loaded: true,
  size_bytes: 19_000_000_000,
  params_string: "35.4B",
  quantization: { name: "4bit", bits_per_weight: 4 },
  loaded_instance_ids: ["qwen3-8b"],
};

const notLoadedModel = {
  id: "deepseek-r1-7b",
  name: "DeepSeek R1 7B",
  capabilities: { vision: false, trained_for_tool_use: false },
  loaded: false,
  size_bytes: 7_000_000_000,
  params_string: "7B",
  quantization: null,
  loaded_instance_ids: [],
};

const multiInstanceModel = {
  id: "qwen3.5-122b",
  name: "Qwen3.5 122B",
  capabilities: { vision: true, trained_for_tool_use: true },
  loaded: true,
  size_bytes: 69_000_000_000,
  params_string: "122B",
  quantization: { name: "4bit", bits_per_weight: 4 },
  loaded_instance_ids: ["qwen3.5-122b", "qwen3.5-122b:2"],
};

// ------------------------------------------------------------------
// Tests
// ------------------------------------------------------------------

describe("AdminModels", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    mockRefetch.mockResolvedValue(undefined);
    (useModels as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { models: [loadedModel, notLoadedModel], total: 2 },
      isLoading: false,
      error: null,
      refetch: mockRefetch,
    });
  });

  it("renders model names from useModels", () => {
    render(<AdminModels />, { wrapper: Wrapper });
    expect(screen.getByText("Qwen3.6 35B")).toBeTruthy();
    expect(screen.getByText("DeepSeek R1 7B")).toBeTruthy();
  });

  it("shows loaded badge for loaded models", () => {
    render(<AdminModels />, { wrapper: Wrapper });
    expect(screen.getByText("loaded")).toBeTruthy();
  });

  it("shows not-loaded badge for unloaded models", () => {
    render(<AdminModels />, { wrapper: Wrapper });
    expect(screen.getByText("not loaded")).toBeTruthy();
  });

  it("renders Load and Unload buttons for each model", () => {
    render(<AdminModels />, { wrapper: Wrapper });
    // aria-label is "Load <model name>" — use partial match
    const loadBtns = screen.getAllByRole("button", { name: /^load /i });
    expect(loadBtns.length).toBe(2);
    const unloadBtns = screen.getAllByRole("button", { name: /^unload /i });
    expect(unloadBtns.length).toBe(2);
  });

  it("clicking Load on unloaded model calls loadModel mutation directly", async () => {
    render(<AdminModels />, { wrapper: Wrapper });
    const loadBtns = screen.getAllByRole("button", { name: /^load /i });
    // Click the second Load button (not-loaded model has 0 instances → no confirm modal).
    act(() => { fireEvent.click(at(loadBtns, 1)); });
    await waitFor(() => {
      expect(mockLoadModel).toHaveBeenCalledWith({ model: "deepseek-r1-7b" });
    });
  });

  it("N=1: clicking Unload calls unloadInstance directly", async () => {
    render(<AdminModels />, { wrapper: Wrapper });
    const unloadBtns = screen.getAllByRole("button", { name: /^unload /i });
    // First model has 1 instance.
    act(() => { fireEvent.click(at(unloadBtns, 0)); });
    await waitFor(() => {
      expect(mockUnloadModel).toHaveBeenCalledWith({
        instance_id: "qwen3-8b",
      });
    });
  });

  it("Unload button disabled when no instances loaded", () => {
    render(<AdminModels />, { wrapper: Wrapper });
    // The not-loaded model's Unload button should be disabled.
    const unloadBtns = screen.getAllByRole("button", { name: /^unload /i });
    // Find the disabled one — notLoadedModel has 0 instances.
    const disabledUnload = unloadBtns.find(
      (btn) => (btn as HTMLButtonElement).disabled
    );
    expect(disabledUnload).toBeTruthy();
  });

  it("N>1: clicking Unload opens instance picker", async () => {
    (useModels as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { models: [multiInstanceModel], total: 1 },
      isLoading: false,
      error: null,
      refetch: mockRefetch,
    });

    render(<AdminModels />, { wrapper: Wrapper });
    const unloadBtns = screen.getAllByRole("button", { name: /^unload /i });
    act(() => { fireEvent.click(at(unloadBtns, 0)); });

    await waitFor(() => {
      // Instance picker should show the second instance ID (it won't appear
      // in the model table row, only in the picker overlay).
      expect(screen.getByText("qwen3.5-122b:2")).toBeTruthy();
    });
  });

  it("N>1: 'Unload all instances' toggle opens confirmation modal", async () => {
    (useModels as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { models: [multiInstanceModel], total: 1 },
      isLoading: false,
      error: null,
      refetch: mockRefetch,
    });

    render(<AdminModels />, { wrapper: Wrapper });
    const unloadBtns = screen.getAllByRole("button", { name: /^unload /i });
    act(() => { fireEvent.click(at(unloadBtns, 0)); });

    // Wait for picker to appear.
    await waitFor(() => {
      expect(screen.getByText("Unload all instances")).toBeTruthy();
    });

    // Click "Unload all instances" — should open confirmation modal.
    act(() => { fireEvent.click(screen.getByText("Unload all instances")); });
    await waitFor(() => {
      expect(screen.getByText("Confirm")).toBeTruthy();
    });
  });

  it("Load on already-loaded model shows confirmation modal", async () => {
    (useModels as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { models: [loadedModel], total: 1 },
      isLoading: false,
      error: null,
      refetch: mockRefetch,
    });

    render(<AdminModels />, { wrapper: Wrapper });
    const loadBtns = screen.getAllByRole("button", { name: /^load /i });
    // loadedModel has 1 instance — clicking Load should open confirm modal.
    act(() => { fireEvent.click(at(loadBtns, 0)); });

    await waitFor(() => {
      expect(screen.getByText(/already has 1 loaded instance/i)).toBeTruthy();
    });

    // mutation should NOT have fired yet.
    expect(mockLoadModel).not.toHaveBeenCalled();
  });

  it("Load duplicate confirm → mutation fires; cancel → mutation does not fire", async () => {
    (useModels as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { models: [loadedModel], total: 1 },
      isLoading: false,
      error: null,
      refetch: mockRefetch,
    });

    // --- Cancel path ---
    const { unmount } = render(<AdminModels />, { wrapper: Wrapper });
    let loadBtns = screen.getAllByRole("button", { name: /^load /i });
    act(() => { fireEvent.click(at(loadBtns, 0)); });

    await waitFor(() => {
      expect(screen.getByText(/already has 1 loaded instance/i)).toBeTruthy();
    });

    act(() => { fireEvent.click(screen.getByRole("button", { name: /cancel/i })); });
    await waitFor(() => {
      expect(screen.queryByText(/already has 1 loaded instance/i)).toBeNull();
    });
    expect(mockLoadModel).not.toHaveBeenCalled();

    unmount();

    // --- Confirm path ---
    render(<AdminModels />, { wrapper: Wrapper });
    loadBtns = screen.getAllByRole("button", { name: /^load /i });
    act(() => { fireEvent.click(at(loadBtns, 0)); });

    await waitFor(() => {
      expect(screen.getByText(/already has 1 loaded instance/i)).toBeTruthy();
    });

    act(() => { fireEvent.click(screen.getByRole("button", { name: /confirm/i })); });
    await waitFor(() => {
      expect(mockLoadModel).toHaveBeenCalledWith({ model: "qwen3-8b" });
    });
  });

  it("shows loading state when isLoading=true", () => {
    (useModels as ReturnType<typeof vi.fn>).mockReturnValue({
      data: null,
      isLoading: true,
      error: null,
      refetch: mockRefetch,
    });
    render(<AdminModels />, { wrapper: Wrapper });
    expect(screen.getByText("Loading models…")).toBeTruthy();
  });

  it("shows error message when models fetch fails", () => {
    (useModels as ReturnType<typeof vi.fn>).mockReturnValue({
      data: null,
      isLoading: false,
      error: { message: "Network error", detail: "Network error", status: 500 },
      refetch: mockRefetch,
    });
    render(<AdminModels />, { wrapper: Wrapper });
    expect(screen.getByText(/couldn't load models/i)).toBeTruthy();
  });

  // ── P12c: Escape key + focus-trap ────────────────────────────────────────────

  it("Escape dismisses ConfirmModal (load duplicate path)", async () => {
    (useModels as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { models: [loadedModel], total: 1 },
      isLoading: false,
      error: null,
      refetch: mockRefetch,
    });

    render(<AdminModels />, { wrapper: Wrapper });
    const loadBtns = screen.getAllByRole("button", { name: /^load /i });
    act(() => { fireEvent.click(at(loadBtns, 0)); });

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeNull();
    });

    // Escape should close the modal.
    act(() => { fireEvent.keyDown(document, { key: "Escape" }); });

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("ConfirmModal Cancel button receives initial focus", async () => {
    (useModels as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { models: [loadedModel], total: 1 },
      isLoading: false,
      error: null,
      refetch: mockRefetch,
    });

    render(<AdminModels />, { wrapper: Wrapper });
    const loadBtns = screen.getAllByRole("button", { name: /^load /i });
    act(() => { fireEvent.click(at(loadBtns, 0)); });

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeNull();
    });

    const cancelBtn = screen.getByRole("button", { name: /^cancel$/i });
    expect(document.activeElement).toBe(cancelBtn);
  });

  it("Escape dismisses InstancePicker (N>1 unload path)", async () => {
    (useModels as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { models: [multiInstanceModel], total: 1 },
      isLoading: false,
      error: null,
      refetch: mockRefetch,
    });

    render(<AdminModels />, { wrapper: Wrapper });
    const unloadBtns = screen.getAllByRole("button", { name: /^unload /i });
    act(() => { fireEvent.click(at(unloadBtns, 0)); });

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeNull();
    });

    act(() => { fireEvent.keyDown(document, { key: "Escape" }); });

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).toBeNull();
    });
  });

  it("InstancePicker Cancel button receives initial focus", async () => {
    (useModels as ReturnType<typeof vi.fn>).mockReturnValue({
      data: { models: [multiInstanceModel], total: 1 },
      isLoading: false,
      error: null,
      refetch: mockRefetch,
    });

    render(<AdminModels />, { wrapper: Wrapper });
    const unloadBtns = screen.getAllByRole("button", { name: /^unload /i });
    act(() => { fireEvent.click(at(unloadBtns, 0)); });

    await waitFor(() => {
      expect(screen.queryByRole("dialog")).not.toBeNull();
    });

    // The Cancel button (close button in the picker) should be focused.
    const cancelBtn = screen.getByRole("button", { name: /^cancel$/i });
    expect(document.activeElement).toBe(cancelBtn);
  });
});
