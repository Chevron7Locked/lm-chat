/* SPDX-License-Identifier: Apache-2.0 */
/**
 * PromptLibrary page unit tests — render + browse + edit affordance.
 *
 * Locked behaviours:
 *   - Empty prompt list renders the "shelves are bare" empty-state.
 *   - A populated list renders one row per prompt with the name + truncated
 *     content preview.
 *   - The create form submit calls useCreatePrompt().mutateAsync with the
 *     trimmed { name, content } payload.
 *   - Clicking "Edit" on a row enters edit mode for that row (the inline
 *     edit form replaces the static row).
 *
 * usePrompts hooks, useToast, useDropdownKeyboard, and Sidebar (via AppShell)
 * are mocked at module level so the suite does not need a TanStack Query
 * provider.
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

import type { Prompt } from "@/hooks/usePrompts";

// ─── Mock toast store ────────────────────────────────────────────────────────

const mockPush = vi.fn();
vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: mockPush, dismiss: vi.fn() }),
  useToastStore: { getState: () => ({ push: mockPush, dismiss: vi.fn() }) },
}));

// ─── Mock authStore — signed-in user (Sidebar/AppShell consult it) ───────────

vi.mock("@/stores/authStore", () => ({
  useAuthStore: () => ({
    user: { id: 1, username: "alice", is_admin: false, totp_enabled: false },
    isInitializing: false,
  }),
}));

// ─── Mock usePrompts hooks ────────────────────────────────────────────────────

interface MockPromptsQueryResult {
  data: Prompt[];
  isLoading: boolean;
  isError: boolean;
}

const mockUsePrompts = vi.fn<() => MockPromptsQueryResult>();
const mockCreateMutate = vi.fn();
const mockUpdateMutate = vi.fn();
const mockDeleteMutate = vi.fn();

vi.mock("@/hooks/usePrompts", () => ({
  usePrompts: () => mockUsePrompts(),
  useCreatePrompt: () => ({ mutateAsync: mockCreateMutate, isPending: false }),
  useUpdatePrompt: () => ({
    mutateAsync: mockUpdateMutate,
    isPending: false,
  }),
  useDeletePrompt: () => ({ mutateAsync: mockDeleteMutate, isPending: false }),
}));

// ─── Mock useDropdownKeyboard (the inline edit row pulls it in) ─────────────

vi.mock("@/hooks/useDropdownKeyboard", () => ({
  useDropdownKeyboard: () => ({
    containerProps: { onKeyDown: () => { /* noop */ } },
  }),
}));

// ─── Mock Sidebar (AppShell pulls it in) ─────────────────────────────────────

vi.mock("@/components/Sidebar", () => ({
  Sidebar: () =>
    createElement("div", { "data-testid": "mock-sidebar" }, "Sidebar"),
}));

// ─── Fixtures ────────────────────────────────────────────────────────────────

const PROMPT_A: Prompt = {
  id: 1,
  name: "summarize-code",
  content: "Summarize the following code into three bullet points.",
  user_id: 1,
  created_at: "2026-01-01T00:00:00Z",
  updated_at: "2026-01-01T00:00:00Z",
};

const PROMPT_B: Prompt = {
  id: 2,
  name: "explain-error",
  content: "Explain this error in plain language and propose a fix.",
  user_id: 1,
  created_at: "2026-01-02T00:00:00Z",
  updated_at: "2026-01-02T00:00:00Z",
};

async function freshPromptLibrary() {
  vi.resetModules();
  const mod = await import("@/pages/PromptLibrary");
  return mod.default;
}

function renderPromptLibrary(Page: React.ComponentType) {
  return render(
    <MemoryRouter initialEntries={["/prompts"]}>
      <Routes>
        <Route path="/prompts" element={<Page />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("PromptLibrary", () => {
  beforeEach(() => {
    mockPush.mockClear();
    mockUsePrompts.mockReset();
    mockCreateMutate.mockReset();
    mockUpdateMutate.mockReset();
    mockDeleteMutate.mockReset();
    cleanup();
  });

  it("renders the empty-state copy when no prompts exist", async () => {
    mockUsePrompts.mockReturnValue({ data: [], isLoading: false, isError: false });
    const Page = await freshPromptLibrary();
    renderPromptLibrary(Page);

    expect(
      screen.getByText(/The recipes are empty\./i),
    ).toBeTruthy();
  });

  it("renders one row per prompt with name and truncated content", async () => {
    mockUsePrompts.mockReturnValue({
      data: [PROMPT_A, PROMPT_B],
      isLoading: false,
      isError: false,
    });
    const Page = await freshPromptLibrary();
    renderPromptLibrary(Page);

    expect(screen.getByText(PROMPT_A.name)).toBeTruthy();
    expect(screen.getByText(PROMPT_B.name)).toBeTruthy();
    expect(screen.getByText(PROMPT_A.content)).toBeTruthy();
  });

  it("submitting the create form calls useCreatePrompt with the trimmed payload", async () => {
    mockUsePrompts.mockReturnValue({ data: [], isLoading: false, isError: false });
    mockCreateMutate.mockResolvedValue({ id: 99 });
    const Page = await freshPromptLibrary();
    renderPromptLibrary(Page);

    fireEvent.change(screen.getByLabelText(/^Prompt name$/), {
      target: { value: "  refactor  " },
    });
    fireEvent.change(screen.getByLabelText(/^Prompt content$/), {
      target: { value: "  Refactor this code  " },
    });
    fireEvent.click(screen.getByRole("button", { name: /Create prompt/ }));

    await waitFor(() => {
      expect(mockCreateMutate).toHaveBeenCalledWith({
        name: "refactor",
        content: "Refactor this code",
      });
    });
  });

  it("clicking Edit on a row swaps it for the inline edit form", async () => {
    mockUsePrompts.mockReturnValue({
      data: [PROMPT_A],
      isLoading: false,
      isError: false,
    });
    const Page = await freshPromptLibrary();
    renderPromptLibrary(Page);

    fireEvent.click(screen.getByRole("button", { name: "Edit" }));
    expect(screen.getByLabelText("Edit prompt name")).toBeTruthy();
    expect(screen.getByLabelText("Edit prompt content")).toBeTruthy();
    expect(screen.getByRole("button", { name: /Save/ })).toBeTruthy();
    expect(screen.getByRole("button", { name: /Cancel/ })).toBeTruthy();
  });
});
