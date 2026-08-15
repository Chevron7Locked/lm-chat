/**
 * P13h — Composer MCP integrations chip-row.
 *
 * Asserts three invariants on the per-request picker:
 *
 *  1. The chip-row renders one button per available integration.
 *  2. Toggling a chip adds the value to the selection (visible via
 *     `aria-checked` and the value submission path).
 *  3. Entries with `enabled_by_default=true` are pre-selected on mount;
 *     entries with `enabled_by_default=false` start unselected.
 *
 * These hooks and components are mocked so the test can run without a
 * QueryClientProvider:
 *  - useIntegrationsList — returns a controlled list per test.
 *  - useChatPreset       — stubbed to a no-op preset state.
 *  - useSTT, MicButton   — irrelevant to the picker; stubbed away.
 *  - usePrompts          — irrelevant; stubbed.
 *  - SlashMenu           — irrelevant; rendered as null.
 */
import type { ComponentProps } from "react";
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { Composer, resolveChatIntegrationsField } from "@/components/Composer";

// Real onSubmit signature (not hand-duplicated) — a bare vi.fn() types
// .mock.calls[0] ambiguously, which fails the `[, payload]` destructure
// under noUncheckedIndexedAccess.
type OnSubmitFn = ComponentProps<typeof Composer>["onSubmit"];

// ─── Mocks ───────────────────────────────────────────────────────────────────

vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: vi.fn() }),
}));

vi.mock("@/hooks/useSTT", () => ({
  useSTT: () => ({
    capability: { available: false, engine: null },
    state: { listening: false, error: null },
    start: vi.fn(),
    stop: vi.fn(),
  }),
  detectSTT: () => ({ available: false, engine: null }),
}));

vi.mock("@/components/MicButton", () => ({
  MicButton: () => null,
}));

// PROJECTS-V1 Phase 6: InProjectChip uses query hooks; stub it.
vi.mock("@/components/InProjectChip", () => ({
  InProjectChip: () => null,
}));

// PROJECTS-V1 additions Phase 11: RagModeBadge same reason.
vi.mock("@/components/RagModeBadge", () => ({
  RagModeBadge: () => null,
}));

vi.mock("@/components/SlashMenu", () => ({
  SlashMenu: () => null,
  parseSlashCommand: () => null,
  BUILTIN_COMMANDS: [],
}));

vi.mock("@/hooks/usePrompts", () => ({
  usePrompts: () => ({ data: [], isLoading: false, isError: false }),
}));

vi.mock("@/hooks/useChatPreset", () => ({
  useChatPreset: () => ({
    activePreset: "",
    preset: null,
    setPreset: vi.fn(),
    clearPreset: vi.fn(),
  }),
  useHydrateChatPresets: () => undefined,
  useChatPresetStore: vi.fn(),
}));

// Controllable mock — each test re-imports a fresh fixture via vi.doMock.
let mockEntries: Array<{
  id: number;
  value: string;
  sort_order: number;
  enabled_by_default?: boolean;
  source?: string;
  created_at: string;
  updated_at: string;
}> = [];

vi.mock("@/hooks/useIntegrationsList", () => ({
  useIntegrationsList: () => ({
    data: mockEntries,
    isLoading: false,
    isError: false,
  }),
  useUpdateIntegrationsList: () => ({ mutate: vi.fn(), isPending: false }),
}));

// Cluster 3a (audit 2026-06-10): Composer now calls useModels() for
// capability gating. Tests override this per-suite using a module-level
// mutable ref so gating tests can control the returned capabilities.
let mockModelsData: { models: Array<{ id: string; capabilities: unknown; loaded: boolean; loaded_instance_ids: string[]; name: string; max_context_length: number; size_bytes: number; params_string: string }> } | undefined = undefined;

vi.mock("@/hooks/useModels", () => ({
  useModels: () => ({ data: mockModelsData, isLoading: false, isError: false, refetch: vi.fn() }),
}));

// Bug A (2026-07-18 dogfood investigation): controllable mock for the
// resolved LM Studio config's endpoint-mode field. Defaults to `undefined`
// (query not yet resolved) so existing tests — which rely on the implicit
// "no data yet → native" fallback — are unaffected. Tests that care about
// the ACTUAL endpointMode → activeSystem → toolSource chain set this
// explicitly.
let mockLmStudioEndpointMode: "native" | "openai_compat" | undefined =
  undefined;

vi.mock("@/hooks/useLmStudioConfig", () => ({
  useLmStudioConfig: () => ({
    data:
      mockLmStudioEndpointMode === undefined
        ? undefined
        : { lm_studio_endpoint_mode: mockLmStudioEndpointMode },
    isLoading: false,
    isError: false,
  }),
  lmStudioConfigKeys: { resolved: () => ["lmstudio-config", "resolved"] },
}));

// ─── Tests ───────────────────────────────────────────────────────────────────

describe("Composer integrations chip-row (P13h)", () => {
  const baseProps = {
    chatId: 1,
    streaming: false,
    onSubmit: vi.fn(),
    onStop: vi.fn(),
    onClear: vi.fn(),
    onFork: vi.fn(),
    onCompact: vi.fn(),
    onMemoryPin: vi.fn(),
    modelId: "stub-model-q4",
  };

  beforeEach(() => {
    mockEntries = [];
    mockModelsData = undefined;
    mockLmStudioEndpointMode = undefined;
    vi.clearAllMocks();
    // 2026-06-12: the Composer now persists selectedIntegrations in
    // localStorage keyed by chatId so refresh/navigation doesn't reset
    // them. Clear between tests so a prior test's selection can't bleed
    // into the next test's expected-empty state.
    if (typeof localStorage !== "undefined") {
      localStorage.clear();
    }
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("renders one chip per available integration", () => {
    mockEntries = [
      {
        id: 1,
        value: "mcp/searxng",
        sort_order: 0,
        enabled_by_default: false,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
      {
        id: 2,
        value: "mcp/filesystem",
        sort_order: 1,
        enabled_by_default: false,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
    ];
    render(<Composer {...baseProps} />);

    expect(screen.getByTestId("integration-pill-mcp/searxng")).toBeTruthy();
    expect(screen.getByTestId("integration-pill-mcp/filesystem")).toBeTruthy();
  });

  it("toggling a chip flips aria-checked", () => {
    mockEntries = [
      {
        id: 1,
        value: "mcp/searxng",
        sort_order: 0,
        enabled_by_default: false,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
    ];
    render(<Composer {...baseProps} />);

    const pill = screen.getByTestId("integration-pill-mcp/searxng");
    expect(pill.getAttribute("aria-checked")).toBe("false");

    fireEvent.click(pill);
    expect(pill.getAttribute("aria-checked")).toBe("true");

    fireEvent.click(pill);
    expect(pill.getAttribute("aria-checked")).toBe("false");
  });

  it("seeds chips marked enabled_by_default=true as selected on mount", () => {
    mockEntries = [
      {
        id: 1,
        value: "mcp/searxng",
        sort_order: 0,
        enabled_by_default: true, // default ON
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
      {
        id: 2,
        value: "mcp/filesystem",
        sort_order: 1,
        enabled_by_default: false, // opt-in
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
    ];
    render(<Composer {...baseProps} />);

    expect(
      screen.getByTestId("integration-pill-mcp/searxng").getAttribute("aria-checked"),
    ).toBe("true");
    expect(
      screen
        .getByTestId("integration-pill-mcp/filesystem")
        .getAttribute("aria-checked"),
    ).toBe("false");
  });

  it("submit carries the selected integrations on the payload", () => {
    mockEntries = [
      {
        id: 1,
        value: "mcp/searxng",
        sort_order: 0,
        enabled_by_default: true,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
      {
        id: 2,
        value: "mcp/filesystem",
        sort_order: 1,
        enabled_by_default: false,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
    ];
    const onSubmit = vi.fn<OnSubmitFn>();
    render(<Composer {...baseProps} onSubmit={onSubmit} />);

    // Toggle the second integration on so both are selected.
    fireEvent.click(screen.getByTestId("integration-pill-mcp/filesystem"));

    // Type a message and submit via Cmd+Enter.
    const textarea = screen.getByLabelText("Message") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "ping" } });
    fireEvent.keyDown(textarea, { key: "Enter", metaKey: true });

    expect(onSubmit).toHaveBeenCalledTimes(1);
    const onSubmitCall = onSubmit.mock.calls[0];
    if (onSubmitCall === undefined) throw new Error("expected onSubmit to have been called");
    const [, payload] = onSubmitCall;
    expect(payload.integrations).toEqual(["mcp/searxng", "mcp/filesystem"]);
  });

  it("hydrates selectedIntegrations from localStorage (refresh wiped tools)", () => {
    // The Composer persists selectedIntegrations in localStorage keyed by
    // chatId. On mount it hydrates from there BEFORE the
    // enabled_by_default seed runs — so a manually-enabled integration
    // (admin flag off but user toggled on) survives a page refresh /
    // navigate-away-and-back. This was the exact regression that produced
    // "this chat was the same chat that previously used firecrawl, but
    // tools didn't fire": Composer remount + seed-only-defaults skipped
    // the manually-enabled firecrawl.
    const props = { ...baseProps, chatId: 42 };
    // Pre-populate localStorage as if the user had toggled mcp/firecrawl
    // on in this chat earlier — even though firecrawl's
    // enabled_by_default=false (so the seed wouldn't pick it up).
    localStorage.setItem(
      "lmchat:composer:integrations:42",
      JSON.stringify(["mcp/firecrawl"]),
    );
    mockEntries = [
      {
        id: 1,
        value: "mcp/firecrawl",
        sort_order: 0,
        enabled_by_default: false,  // ← seed would skip this
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
    ];
    const onSubmit = vi.fn<OnSubmitFn>();
    render(<Composer {...props} onSubmit={onSubmit} />);
    const textarea = screen.getByLabelText("Message") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "ping" } });
    fireEvent.keyDown(textarea, { key: "Enter", metaKey: true });
    expect(onSubmit).toHaveBeenCalledTimes(1);
    const onSubmitCall = onSubmit.mock.calls[0];
    if (onSubmitCall === undefined) throw new Error("expected onSubmit to have been called");
    const [, payload] = onSubmitCall;
    // The hydrated value MUST win over the (would-have-been-empty) seed.
    expect(payload.integrations).toEqual(["mcp/firecrawl"]);
  });

  it("persists selection back to localStorage on toggle", () => {
    const props = { ...baseProps, chatId: 88 };
    mockEntries = [
      {
        id: 1,
        value: "mcp/searxng",
        sort_order: 0,
        enabled_by_default: false,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
    ];
    render(<Composer {...props} />);
    // Toggle searxng on by clicking its chip.
    const chip = screen.getByTestId("integration-pill-mcp/searxng");
    fireEvent.click(chip);
    // localStorage now reflects the selection.
    expect(localStorage.getItem("lmchat:composer:integrations:88")).toBe(
      JSON.stringify(["mcp/searxng"]),
    );
    // Toggle off → localStorage reflects empty selection (NOT removed).
    fireEvent.click(chip);
    expect(localStorage.getItem("lmchat:composer:integrations:88")).toBe(
      JSON.stringify([]),
    );
  });

  it("pins the legacy storage key shape — writes land at lmchat:composer:integrations:<chatId>, not a hook-synthesized key (Item 6, 2026-06-12)", () => {
    // The Composer adopted useChatScopedState (Item 6) with a
    // localStorageKeyOverride that preserves the 98b1d93 key shape. This
    // test pins the LITERAL key string so a future hook/key rename can't
    // silently orphan users' stored selections.
    const props = { ...baseProps, chatId: 42 };
    mockEntries = [
      {
        id: 1,
        value: "mcp/searxng",
        sort_order: 0,
        enabled_by_default: false,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
    ];
    render(<Composer {...props} />);
    fireEvent.click(screen.getByTestId("integration-pill-mcp/searxng"));
    // Legacy key shape holds the selection…
    expect(localStorage.getItem("lmchat:composer:integrations:42")).toBe(
      JSON.stringify(["mcp/searxng"]),
    );
    // …and the hook's default synthesized shape was NOT used.
    expect(localStorage.getItem("lmchat:chat-scoped:integrations:42")).toBeNull();
  });

  // ─── P13h-fix: FE submit-field semantics ────────────────────────────────────

  it("P13h-fix: omits integrations when chat is fully untouched (no localStorage entry, nothing selected, no defaults)", () => {
    // No localStorage entry, no enabled_by_default entries, nothing selected →
    // field must be absent so the BE can distinguish "not touched" from "explicitly empty".
    // Note: if any entry has enabled_by_default=true and no stored entry exists,
    // the seed effect fires and pre-selects it — so that's a non-empty selection, not
    // the "untouched" case. This test covers the scenario where no defaults exist
    // (all entries are opt-in / enabled_by_default=false) and nothing stored.
    mockEntries = [
      {
        id: 1,
        value: "mcp/firecrawl",
        sort_order: 0,
        enabled_by_default: false, // opt-in only — seed does NOT fire
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
    ];
    const onSubmit = vi.fn<OnSubmitFn>();
    render(<Composer {...baseProps} chatId={99} onSubmit={onSubmit} />);

    const textarea = screen.getByLabelText("Message") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "ping" } });
    fireEvent.keyDown(textarea, { key: "Enter", metaKey: true });

    expect(onSubmit).toHaveBeenCalledTimes(1);
    const onSubmitCall = onSubmit.mock.calls[0];
    if (onSubmitCall === undefined) throw new Error("expected onSubmit to have been called");
    const [, payload] = onSubmitCall;
    // No stored entry + no selection (seed didn't fire — no defaults) → omit.
    // BE treats omission as "apply admin defaults server-side".
    expect(payload.integrations).toBeUndefined();
  });

  it("P13h-fix: includes integrations (even as []) when localStorage has a stored entry for this chat", () => {
    // A stored empty array means "user explicitly turned tools off for this chat".
    // The field MUST be included in the submit payload so the BE honours [] not defaults.
    localStorage.setItem("lmchat:composer:integrations:77", JSON.stringify([]));
    mockEntries = [
      {
        id: 1,
        value: "mcp/firecrawl",
        sort_order: 0,
        enabled_by_default: true,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
    ];
    const onSubmit = vi.fn<OnSubmitFn>();
    render(<Composer {...baseProps} chatId={77} onSubmit={onSubmit} />);

    const textarea = screen.getByLabelText("Message") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "ping" } });
    fireEvent.keyDown(textarea, { key: "Enter", metaKey: true });

    expect(onSubmit).toHaveBeenCalledTimes(1);
    const onSubmitCall = onSubmit.mock.calls[0];
    if (onSubmitCall === undefined) throw new Error("expected onSubmit to have been called");
    const [, payload] = onSubmitCall;
    // Stored [] entry → field is present as [] (BE will honour it, not apply defaults).
    expect(payload.integrations).toEqual([]);
  });

  it("honours a persisted explicit empty selection — defaults are NOT re-seeded (Item 6, 2026-06-12)", () => {
    // A stored "[]" means the user turned tools off for this chat. The
    // defaults seed must not resurrect enabled_by_default entries.
    localStorage.setItem("lmchat:composer:integrations:42", JSON.stringify([]));
    mockEntries = [
      {
        id: 1,
        value: "mcp/searxng",
        sort_order: 0,
        enabled_by_default: true, // would seed if the entry were absent
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
    ];
    render(<Composer {...baseProps} chatId={42} />);
    expect(
      screen.getByTestId("integration-pill-mcp/searxng").getAttribute("aria-checked"),
    ).toBe("false");
  });

  it("omits integrations from payload when nothing is selected", () => {
    mockEntries = [
      {
        id: 1,
        value: "mcp/searxng",
        sort_order: 0,
        enabled_by_default: false,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
    ];
    const onSubmit = vi.fn<OnSubmitFn>();
    render(<Composer {...baseProps} onSubmit={onSubmit} />);

    const textarea = screen.getByLabelText("Message") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "ping" } });
    fireEvent.keyDown(textarea, { key: "Enter", metaKey: true });

    expect(onSubmit).toHaveBeenCalledTimes(1);
    const onSubmitCall = onSubmit.mock.calls[0];
    if (onSubmitCall === undefined) throw new Error("expected onSubmit to have been called");
    const [, payload] = onSubmitCall;
    expect(payload.integrations).toBeUndefined();
  });

  // Cluster 3a Task 7 (audit 2026-06-10): integrations disclosure is gated on
  // capabilities.trained_for_tool_use. When the current model is known-non-tool
  // the chip-row must be hidden; when capabilities are unknown (null/undefined)
  // it must show (backward-compat).
  it("test_Composer_integrations_panel_gated_on_tool_capability: hides chips when model has trained_for_tool_use=false", () => {
    mockEntries = [
      {
        id: 1,
        value: "mcp/searxng",
        sort_order: 0,
        enabled_by_default: false,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
    ];
    mockModelsData = {
      models: [
        {
          id: "stub-model-q4",
          name: "Stub Q4",
          loaded: true,
          loaded_instance_ids: ["stub-model-q4"],
          capabilities: {
            vision: false,
            trained_for_tool_use: false,
            reasoning: null,
            embedding: false,
          },
          max_context_length: 8192,
          size_bytes: 0,
          params_string: "",
        },
      ],
    };
    render(<Composer {...baseProps} />);
    // Chip should not render — model is known non-tool.
    expect(screen.queryByTestId("integration-pill-mcp/searxng")).toBeNull();
  });

  it("shows integration chips when capabilities are unknown (null data — backward-compat)", () => {
    mockEntries = [
      {
        id: 1,
        value: "mcp/searxng",
        sort_order: 0,
        enabled_by_default: false,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
    ];
    // mockModelsData = undefined (default after beforeEach reset)
    render(<Composer {...baseProps} />);
    // With unknown capabilities the integrations section should be visible.
    expect(screen.getByTestId("integration-pill-mcp/searxng")).toBeTruthy();
  });

  // ─── T1-10: endpoint drives tools — cross-system leak + hidden-tools signal ──

  it("T1-10: drops cross-system (Store) integrations from the submit payload on a native model", () => {
    // Default env here: no model selected → treated as LM Studio, endpoint mode
    // native → activeSystem = "lmstudio". A Store-sourced tool is cross-system:
    // it can't run on the native wire, so it must NOT ship (it would silently
    // no-op). The stored selection retains both; only the runnable one ships.
    localStorage.setItem(
      "lmchat:composer:integrations:55",
      JSON.stringify(["mcp/searxng", "mcp/store-only"]),
    );
    mockEntries = [
      {
        id: 1,
        value: "mcp/searxng",
        sort_order: 0,
        source: "lmstudio",
        enabled_by_default: false,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
      {
        id: 2,
        value: "mcp/store-only",
        sort_order: 1,
        source: "store", // hidden for a native LM Studio model
        enabled_by_default: false,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
    ];
    const onSubmit = vi.fn<OnSubmitFn>();
    render(<Composer {...baseProps} chatId={55} onSubmit={onSubmit} />);

    const textarea = screen.getByLabelText("Message") as HTMLTextAreaElement;
    fireEvent.change(textarea, { target: { value: "ping" } });
    fireEvent.keyDown(textarea, { key: "Enter", metaKey: true });

    expect(onSubmit).toHaveBeenCalledTimes(1);
    const onSubmitCall = onSubmit.mock.calls[0];
    if (onSubmitCall === undefined) throw new Error("expected onSubmit to have been called");
    const [, payload] = onSubmitCall;
    // Only the same-system (lmstudio) tool ships; the Store tool is filtered out.
    expect(payload.integrations).toEqual(["mcp/searxng"]);
  });

  it("T1-10: surfaces an explicit hint when tools live in the other endpoint's system", () => {
    // A native LM Studio model with a Store-sourced tool available → the tool is
    // hidden from the picker, but the endpoint→tools split must not be silent.
    mockEntries = [
      {
        id: 1,
        value: "mcp/store-a",
        sort_order: 0,
        source: "store",
        enabled_by_default: false,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
    ];
    render(<Composer {...baseProps} chatId={56} />);
    const hint = screen.getByTestId("integrations-other-system-hint");
    expect(hint.textContent).toContain("1 Store tool");
    expect(hint.textContent).toContain("OpenAI-compat mode");
  });

  // ─── Bug A (2026-07-18 dogfood investigation) ──────────────────────────────
  //
  // Reported as "composer shows native-mode tools UI even when the endpoint
  // mode is openai_compat" — hypothesized cause: the composer's
  // `lmStudioConfig` doesn't carry the resolved `lm_studio_endpoint_mode`, so
  // `endpointMode` falls back to "native" regardless of the real setting.
  // These tests pin the ACTUAL resolution chain end-to-end (mocked
  // useLmStudioConfig → endpointMode → activeSystem → visible checkboxes /
  // hint) for BOTH values, so a future regression in that wiring (wrong
  // field name, hook pointed at the wrong query, fallback used even when
  // data IS present) fails loudly here instead of only showing up live.

  it("Bug A: lm_studio_endpoint_mode='openai_compat' renders Store-sourced checkboxes and hides lmstudio-sourced tools", () => {
    mockLmStudioEndpointMode = "openai_compat";
    mockEntries = [
      {
        id: 1,
        value: "mcp/searxng",
        sort_order: 0,
        source: "lmstudio",
        enabled_by_default: false,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
      {
        id: 2,
        value: "mcp/crawl4ai",
        sort_order: 1,
        source: "store",
        enabled_by_default: false,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
    ];
    render(<Composer {...baseProps} chatId={60} />);

    // The Store-sourced tool is visible as a checkbox…
    expect(screen.getByTestId("integration-pill-mcp/crawl4ai")).toBeTruthy();
    // …the lmstudio-sourced tool is NOT (it can't run over the compat wire).
    expect(screen.queryByTestId("integration-pill-mcp/searxng")).toBeNull();
    // …and the hint names the hidden lmstudio tool, not the reverse.
    const hint = screen.getByTestId("integrations-other-system-hint");
    expect(hint.textContent).toContain("1 LM Studio tool");
    // Cross-mode hint: the tools live in the OTHER (Native) mode — the copy
    // says "switch to Native mode" so it can't be misread as the current mode.
    expect(hint.textContent).toContain("switch to Native mode");
  });

  it("Bug A: lm_studio_endpoint_mode='native' renders lmstudio-sourced checkboxes and hides Store-sourced tools", () => {
    mockLmStudioEndpointMode = "native";
    mockEntries = [
      {
        id: 1,
        value: "mcp/searxng",
        sort_order: 0,
        source: "lmstudio",
        enabled_by_default: false,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
      {
        id: 2,
        value: "mcp/crawl4ai",
        sort_order: 1,
        source: "store",
        enabled_by_default: false,
        created_at: "2026-05-22T00:00:00Z",
        updated_at: "2026-05-22T00:00:00Z",
      },
    ];
    render(<Composer {...baseProps} chatId={61} />);

    // The lmstudio-sourced tool is visible as a checkbox…
    expect(screen.getByTestId("integration-pill-mcp/searxng")).toBeTruthy();
    // …the Store-sourced tool is NOT (native mode doesn't drive the Store).
    expect(screen.queryByTestId("integration-pill-mcp/crawl4ai")).toBeNull();
    // …and the hint names the hidden Store tool, not the reverse.
    const hint = screen.getByTestId("integrations-other-system-hint");
    expect(hint.textContent).toContain("1 Store tool");
    expect(hint.textContent).toContain("OpenAI-compat mode");
  });
});

// ─── resolveChatIntegrationsField unit tests (Fix B helper) ──────────────────
describe("resolveChatIntegrationsField", () => {
  beforeEach(() => {
    if (typeof localStorage !== "undefined") localStorage.clear();
  });

  it("returns undefined when no localStorage entry exists for the chat", () => {
    // Untouched chat — no stored entry → omit from payload → BE applies defaults.
    expect(resolveChatIntegrationsField(42)).toBeUndefined();
  });

  it("returns [] when stored entry is an explicit empty array", () => {
    // User explicitly cleared tools — must honour [] not apply defaults.
    localStorage.setItem("lmchat:composer:integrations:7", JSON.stringify([]));
    expect(resolveChatIntegrationsField(7)).toEqual([]);
  });

  it("returns the stored array when it contains integration ids", () => {
    localStorage.setItem(
      "lmchat:composer:integrations:99",
      JSON.stringify(["mcp/searxng", "mcp/firecrawl"]),
    );
    expect(resolveChatIntegrationsField(99)).toEqual(["mcp/searxng", "mcp/firecrawl"]);
  });

  it("returns undefined for chatId=null (no active chat)", () => {
    expect(resolveChatIntegrationsField(null)).toBeUndefined();
  });

  it("returns undefined when stored value is malformed JSON", () => {
    localStorage.setItem("lmchat:composer:integrations:5", "not-json{{");
    expect(resolveChatIntegrationsField(5)).toBeUndefined();
  });

  it("returns undefined when stored value is not a string array", () => {
    localStorage.setItem("lmchat:composer:integrations:6", JSON.stringify({ bad: true }));
    expect(resolveChatIntegrationsField(6)).toBeUndefined();
  });
});
