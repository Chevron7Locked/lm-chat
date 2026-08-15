/* SPDX-License-Identifier: Apache-2.0 */
/**
 * TopBar — the chat-header model picker's "Auto" entry + placeholder gating.
 *
 * Locks the decision matrix that decides whether the picker offers a
 * selectable "Auto" (resolves to the user's default) vs a disabled prompt:
 *
 *   - reachable + models available + a default exists → selectable "Auto",
 *     no placeholder.
 *   - reachable + models available + NO default → no "Auto"; prompt
 *     "Select a model…" (Auto would resolve to nothing / the composer blocks).
 *   - no model loaded / unreachable → the informative disabled placeholder.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { createElement } from "react";

// ─── Hook mocks ──────────────────────────────────────────────────────────────

vi.mock("@/hooks/useViewport", () => ({
  useViewport: () => ({ isMobile: false }),
}));

vi.mock("react-router-dom", () => ({
  useNavigate: () => vi.fn(),
}));

let mockLmStatus = "connected";
vi.mock("@/hooks/useModelList", () => ({
  useModelList: () => ({ status: mockLmStatus }),
}));

let mockOptions: unknown[] = [
  {
    id: "alpha",
    label: "Alpha",
    loaded: true,
    provider: "lmstudio",
    capabilities: {
      vision: false,
      trained_for_tool_use: false,
      reasoning: null,
      embedding: false,
    },
  },
];
vi.mock("@/hooks/useChatModelOptions", () => ({
  useChatModelOptions: () => ({
    options: mockOptions,
    groups: [],
    isLoading: false,
    isError: false,
  }),
}));

// Surfaces value / placeholder / autoOption so the gating is observable.
vi.mock("@/components/ModelSelectControl", () => ({
  ModelSelectControl: ({
    value,
    placeholder,
    autoOption,
  }: {
    value: string;
    placeholder?: string;
    autoOption?: { value: string; label: string };
  }) =>
    createElement(
      "div",
      { "data-testid": "ms" },
      createElement("span", { "data-testid": "ms-value" }, value),
      createElement("span", { "data-testid": "ms-placeholder" }, placeholder ?? ""),
      createElement("span", { "data-testid": "ms-auto" }, autoOption ? autoOption.label : ""),
    ),
  ModelCapabilityIcons: () => null,
}));

vi.mock("@/components/OverflowMenu", () => ({
  OverflowMenu: () => createElement("div", { "data-testid": "overflow" }),
}));
vi.mock("@/components/LmStudioStatusBadge", () => ({
  LmStudioStatusBadge: () => createElement("div", { "data-testid": "lm-badge" }),
}));
vi.mock("@/components/ChatHeaderMenu", () => ({
  ChatHeaderMenu: () => createElement("div", { "data-testid": "chat-header-menu" }),
}));

import { TopBar } from "@/components/chat/TopBar";
import { AUTO_MODEL_VALUE } from "@/components/chat/shared";

function renderTopBar(props: {
  hasDefaultModel: boolean;
  modelId: string;
}) {
  return render(
    createElement(TopBar, {
      title: "Chat",
      modelId: props.modelId,
      onModelChange: vi.fn(),
      hasDefaultModel: props.hasDefaultModel,
      pinned: false,
      onPinToggle: vi.fn(),
      onDelete: vi.fn(),
      onFork: vi.fn(),
      onSettingsOpen: vi.fn(),
      onMemoryOpen: vi.fn(),
      onDocumentsOpen: vi.fn(),
      onPinsOpen: vi.fn(),
      pinsOpen: false,
      panelView: null,
      chatId: 1,
    }),
  );
}

function placeholder(): string {
  return screen.getByTestId("ms-placeholder").textContent;
}
function autoLabel(): string {
  return screen.getByTestId("ms-auto").textContent;
}

describe("TopBar — Auto entry + placeholder gating", () => {
  beforeEach(() => {
    mockLmStatus = "connected";
    mockOptions = [
      {
        id: "alpha",
        label: "Alpha",
        loaded: true,
        provider: "lmstudio",
        capabilities: {
          vision: false,
          trained_for_tool_use: false,
          reasoning: null,
          embedding: false,
        },
      },
    ];
    vi.clearAllMocks();
  });

  it("offers a selectable Auto (no placeholder) when a default exists", () => {
    renderTopBar({ hasDefaultModel: true, modelId: AUTO_MODEL_VALUE });
    expect(autoLabel()).toBe("Auto");
    expect(placeholder()).toBe("");
  });

  it("prompts 'Select a model…' (no Auto) when no default is configured", () => {
    renderTopBar({ hasDefaultModel: false, modelId: "" });
    expect(autoLabel()).toBe("");
    expect(placeholder()).toBe("Select a model…");
  });

  it("shows the informative placeholder (no Auto) when no model is loaded", () => {
    mockLmStatus = "no_models";
    mockOptions = [];
    renderTopBar({ hasDefaultModel: true, modelId: AUTO_MODEL_VALUE });
    expect(autoLabel()).toBe("");
    expect(placeholder()).toBe("No model loaded — open Settings");
  });
});

describe("TopBar — focus-mode toggle", () => {
  beforeEach(() => {
    mockLmStatus = "connected";
    mockOptions = [
      {
        id: "alpha",
        label: "Alpha",
        loaded: true,
        provider: "lmstudio",
        capabilities: {
          vision: false,
          trained_for_tool_use: false,
          reasoning: null,
          embedding: false,
        },
      },
    ];
    vi.clearAllMocks();
  });

  function renderWithFocus(props: {
    focusMode: boolean;
    onToggleFocusMode: () => void;
  }) {
    return render(
      createElement(TopBar, {
        title: "Chat",
        modelId: AUTO_MODEL_VALUE,
        onModelChange: vi.fn(),
        hasDefaultModel: true,
        pinned: false,
        onPinToggle: vi.fn(),
        onDelete: vi.fn(),
        onFork: vi.fn(),
        onSettingsOpen: vi.fn(),
        onMemoryOpen: vi.fn(),
        onDocumentsOpen: vi.fn(),
        onPinsOpen: vi.fn(),
        pinsOpen: false,
        panelView: null,
        chatId: 1,
        focusMode: props.focusMode,
        onToggleFocusMode: props.onToggleFocusMode,
      }),
    );
  }

  it("renders an 'Enter focus mode' toggle that fires onToggleFocusMode", () => {
    const onToggleFocusMode = vi.fn();
    renderWithFocus({ focusMode: false, onToggleFocusMode });
    const btn = screen.getByTestId("topbar-focus-toggle");
    expect(btn.getAttribute("aria-label")).toBe("Enter focus mode");
    expect(btn.getAttribute("data-active")).toBe("false");
    fireEvent.click(btn);
    expect(onToggleFocusMode).toHaveBeenCalledOnce();
  });

  it("reflects the active (copper) state when focus mode is on", () => {
    renderWithFocus({ focusMode: true, onToggleFocusMode: vi.fn() });
    const btn = screen.getByTestId("topbar-focus-toggle");
    expect(btn.getAttribute("aria-label")).toBe("Exit focus mode");
    expect(btn.getAttribute("aria-pressed")).toBe("true");
    expect(btn.getAttribute("data-active")).toBe("true");
  });

  it("omits the toggle entirely when no handler is provided", () => {
    render(
      createElement(TopBar, {
        title: "Chat",
        modelId: AUTO_MODEL_VALUE,
        onModelChange: vi.fn(),
        hasDefaultModel: true,
        pinned: false,
        onPinToggle: vi.fn(),
        onDelete: vi.fn(),
        onFork: vi.fn(),
        onSettingsOpen: vi.fn(),
        onMemoryOpen: vi.fn(),
        onDocumentsOpen: vi.fn(),
        onPinsOpen: vi.fn(),
        pinsOpen: false,
        panelView: null,
        chatId: 1,
      }),
    );
    expect(screen.queryByTestId("topbar-focus-toggle")).toBeNull();
  });
});
