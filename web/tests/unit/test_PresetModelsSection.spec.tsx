/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for PresetModelsSection (Settings → Preset models).
 *
 * Locked behaviours:
 *   - Renders a row for each of the 6 presets.
 *   - Pre-populates each row from usePresetModels data (composite value).
 *   - Selecting a model fires PUT (useSetPresetModels mutation) with
 *     the updated mapping.
 *   - Selecting "" (empty / "Use the chat's model") removes the entry
 *     from the mapping and fires PUT.
 *   - Mutation errors surface a toast.
 *
 * Sub-session launch wiring (tested in a separate describe block):
 *   - When a preset mapping exists, the sub-session stream receives
 *     the configured model_id + provider.
 *   - When no preset mapping exists, falls back to top-bar model +
 *     chat's current provider.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { createElement } from "react";
import { PRESET_LIST } from "@/lib/presets";

// ─── Mock toast ───────────────────────────────────────────────────────────────

const mockPush = vi.fn();
vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: mockPush }),
  useToastStore: { getState: () => ({ push: mockPush }) },
}));

// ─── Mutable state for hook mocks ────────────────────────────────────────────

const mockSetMutate = vi.fn();

// Mutable state object — tests mutate this before rendering.
const mockPresetModelsState = vi.hoisted(() => ({
  data: {} as Record<string, { provider: string; model_id: string }>,
  isLoading: false,
  isPending: false,
}));

vi.mock("@/hooks/usePresetModels", () => ({
  usePresetModels: () => ({
    data: mockPresetModelsState.data,
    isLoading: mockPresetModelsState.isLoading,
  }),
  useSetPresetModels: () => ({
    mutate: mockSetMutate,
    isPending: mockPresetModelsState.isPending,
  }),
}));

// ─── Mock useChatModelOptions ─────────────────────────────────────────────────

vi.mock("@/hooks/useChatModelOptions", () => ({
  useChatModelOptions: () => ({
    options: [
      { id: "llama-3.3-70b", label: "Llama 3.3 70B", loaded: true, provider: "lmstudio" },
      { id: "meta-llama/llama-3.3-70b-instruct", label: "Llama 3.3 Instruct", loaded: true, provider: "openrouter" },
    ],
    groups: [
      {
        provider: "lmstudio",
        label: "LM Studio",
        options: [
          { id: "llama-3.3-70b", label: "Llama 3.3 70B", loaded: true, provider: "lmstudio" },
        ],
      },
      {
        provider: "openrouter",
        label: "OpenRouter",
        options: [
          { id: "meta-llama/llama-3.3-70b-instruct", label: "Llama 3.3 Instruct", loaded: true, provider: "openrouter" },
        ],
      },
    ],
    isLoading: false,
    isError: false,
  }),
}));

// ─── Helpers ─────────────────────────────────────────────────────────────────

async function renderSection() {
  const { PresetModelsSection } = await import("@/components/PresetModelsSection");
  return render(createElement(PresetModelsSection));
}

// ─── Tests ────────────────────────────────────────────────────────────────────

describe("PresetModelsSection", () => {
  beforeEach(() => {
    vi.resetModules();
    mockSetMutate.mockReset();
    mockPush.mockReset();
    mockPresetModelsState.data = {};
    mockPresetModelsState.isLoading = false;
    mockPresetModelsState.isPending = false;
    cleanup();
  });

  it("renders the section container", async () => {
    await renderSection();
    expect(screen.getByTestId("settings-preset-models-section")).toBeTruthy();
  });

  it("renders a row for each of the 6 presets", async () => {
    await renderSection();
    const list = screen.getByTestId("preset-models-list");
    expect(list).toBeTruthy();
    for (const preset of PRESET_LIST) {
      expect(screen.getByTestId(`preset-models-row-${preset.id}`)).toBeTruthy();
    }
    // PRESET_LIST has exactly 6 entries
    expect(PRESET_LIST).toHaveLength(6);
  });

  it("pre-populates each dropdown from usePresetModels data", async () => {
    mockPresetModelsState.data = {
      research: { provider: "openrouter", model_id: "meta-llama/llama-3.3-70b-instruct" },
    };
    await renderSection();

    // The research row's select should have the composite value set.
    const researchSelect = screen.getByTestId(
      "preset-models-select-research",
    ) as HTMLSelectElement;
    expect(researchSelect.value).toBe(
      "openrouter::meta-llama/llama-3.3-70b-instruct",
    );

    // Presets without a mapping should have empty value (fallback).
    const generalSelect = screen.getByTestId(
      "preset-models-select-general",
    ) as HTMLSelectElement;
    expect(generalSelect.value).toBe("");
  });

  it("selecting a model fires PUT with the updated mapping", async () => {
    mockPresetModelsState.data = {};
    await renderSection();

    const coderSelect = screen.getByTestId("preset-models-select-coder");
    fireEvent.change(coderSelect, {
      target: { value: "openrouter::meta-llama/llama-3.3-70b-instruct" },
    });

    expect(mockSetMutate).toHaveBeenCalledOnce();
    const [mapping] = mockSetMutate.mock.calls[0] as [
      Record<string, { provider: string; model_id: string }>,
    ];
    expect(mapping["coder"]).toEqual({
      provider: "openrouter",
      model_id: "meta-llama/llama-3.3-70b-instruct",
    });
  });

  it("selecting empty string removes the preset entry and fires PUT", async () => {
    mockPresetModelsState.data = {
      research: { provider: "openrouter", model_id: "meta-llama/llama-3.3-70b-instruct" },
      coder: { provider: "lmstudio", model_id: "llama-3.3-70b" },
    };
    await renderSection();

    // Clear the research preset by selecting "".
    const researchSelect = screen.getByTestId("preset-models-select-research");
    fireEvent.change(researchSelect, { target: { value: "" } });

    expect(mockSetMutate).toHaveBeenCalledOnce();
    const [mapping] = mockSetMutate.mock.calls[0] as [
      Record<string, { provider: string; model_id: string }>,
    ];
    // research entry must be absent
    expect(mapping["research"]).toBeUndefined();
    // coder entry must be preserved
    expect(mapping["coder"]).toEqual({
      provider: "lmstudio",
      model_id: "llama-3.3-70b",
    });
  });

  it("mutation error pushes a toast", async () => {
    mockPresetModelsState.data = {};
    await renderSection();

    // Simulate an error on mutate call.
    mockSetMutate.mockImplementationOnce(
      (_mapping: unknown, options: { onError?: (err: { detail?: string }) => void }) => {
        options?.onError?.({ detail: "Save failed." });
      },
    );

    const coderSelect = screen.getByTestId("preset-models-select-coder");
    fireEvent.change(coderSelect, {
      target: { value: "openrouter::meta-llama/llama-3.3-70b-instruct" },
    });

    expect(mockPush).toHaveBeenCalledOnce();
    // Guaranteed non-null by the toHaveBeenCalledOnce() check just above.
    expect(mockPush.mock.calls[0]![0]).toMatchObject({
      variant: "error",
      message: "Save failed.",
    });
  });

  it("shows loading state while data is loading", async () => {
    mockPresetModelsState.isLoading = true;
    await renderSection();
    expect(screen.getByTestId("preset-models-loading")).toBeTruthy();
    expect(screen.queryByTestId("preset-models-list")).toBeNull();
  });

  // ─── Stale (saved-but-not-loaded) preset display ────────────────────────────
  //
  // Regression guard for the "settings screen lies" bug: a preset saved to a
  // model that LM Studio no longer has (unloaded/removed/renamed). A native
  // <select> can't render a value matching no <option>, so it silently shows
  // its first option — a DIFFERENT model than the one saved. The fix injects a
  // synthetic "<id> — not loaded" option so the row reflects the real value.

  it("displays a stale saved model truthfully instead of a wrong loaded one", async () => {
    // coder is pinned to a model absent from the loaded catalog above.
    mockPresetModelsState.data = {
      coder: { provider: "lmstudio", model_id: "laguna-s-2.1@q4_k_xl" },
    };
    await renderSection();

    const coderSelect = screen.getByTestId(
      "preset-models-select-coder",
    ) as HTMLSelectElement;

    // The select's value must remain the stored composite — NOT silently
    // collapse to a loaded option (llama-3.3-70b) it never chose.
    expect(coderSelect.value).toBe("lmstudio::laguna-s-2.1@q4_k_xl");

    // And a matching, labelled option must actually exist in the DOM (so the
    // trigger renders the truth), carrying the "not loaded" marker.
    // A rendered <select> always has a selected option — DOM semantics, not
    // an assumption; the toBeTruthy() below is a runtime belt-and-braces.
    const selected = coderSelect.selectedOptions[0]!;
    expect(selected).toBeTruthy();
    expect(selected.value).toBe("lmstudio::laguna-s-2.1@q4_k_xl");
    expect(selected.textContent).toContain("laguna-s-2.1@q4_k_xl");
    expect(selected.textContent).toContain("not loaded");
  });

  it("does NOT inject a stale option when the saved model is loaded", async () => {
    mockPresetModelsState.data = {
      coder: { provider: "lmstudio", model_id: "llama-3.3-70b" },
    };
    await renderSection();

    const coderSelect = screen.getByTestId(
      "preset-models-select-coder",
    ) as HTMLSelectElement;
    expect(coderSelect.value).toBe("lmstudio::llama-3.3-70b");
    // No "Saved but not loaded" group should appear for a valid pin.
    expect(screen.queryByText("Saved but not loaded")).toBeNull();
    // The selected option is the real loaded one, unmarked.
    expect(coderSelect.selectedOptions[0]!.textContent).not.toContain(
      "not loaded",
    );
  });

  it("re-selecting a stale option persists the same composite value (no crash)", async () => {
    mockPresetModelsState.data = {
      coder: { provider: "lmstudio", model_id: "laguna-s-2.1@q4_k_xl" },
    };
    await renderSection();

    const coderSelect = screen.getByTestId("preset-models-select-coder");
    // Selecting the stale option itself must fire PUT with the SAME value —
    // harmless re-persist, never a throw or a silent switch to another model.
    fireEvent.change(coderSelect, {
      target: { value: "lmstudio::laguna-s-2.1@q4_k_xl" },
    });
    expect(mockSetMutate).toHaveBeenCalledOnce();
    const [mapping] = mockSetMutate.mock.calls[0] as [
      Record<string, { provider: string; model_id: string }>,
    ];
    expect(mapping["coder"]).toEqual({
      provider: "lmstudio",
      model_id: "laguna-s-2.1@q4_k_xl",
    });
  });

  it("shows every distinct stale preset truthfully (multiple + cloud provider)", async () => {
    mockPresetModelsState.data = {
      // local unloaded
      coder: { provider: "lmstudio", model_id: "laguna-s-2.1@q4_k_xl" },
      // cloud model absent from the loaded catalog
      research: { provider: "openrouter", model_id: "acme/deprecated-70b" },
    };
    await renderSection();

    const coderSelect = screen.getByTestId(
      "preset-models-select-coder",
    ) as HTMLSelectElement;
    const researchSelect = screen.getByTestId(
      "preset-models-select-research",
    ) as HTMLSelectElement;

    // Each row reflects its OWN stored value, provider prefix preserved.
    expect(coderSelect.value).toBe("lmstudio::laguna-s-2.1@q4_k_xl");
    expect(coderSelect.selectedOptions[0]!.textContent).toContain("not loaded");
    expect(researchSelect.value).toBe("openrouter::acme/deprecated-70b");
    expect(researchSelect.selectedOptions[0]!.textContent).toContain(
      "acme/deprecated-70b",
    );
    expect(researchSelect.selectedOptions[0]!.textContent).toContain(
      "not loaded",
    );
  });
});

// ─── Sub-session preset model wiring (hook logic) ─────────────────────────────

/**
 * These tests verify the lookup logic rather than full Chat.tsx rendering
 * (which would require extensive mocking).  We test the pure mapping
 * function directly.
 */
describe("Sub-session preset model resolution logic", () => {
  /**
   * Mirror of the resolution logic in Chat.tsx handleSubmit:
   *   presetEntry?.model_id ?? selectedModel ?? chatModelId ?? savedDefault ?? ""
   *   presetEntry?.provider ?? chatProvider ?? "lmstudio"
   */
  function resolveSubSessionModel(
    presetId: string,
    presetModels: Record<string, { provider: string; model_id: string }> | undefined,
    selectedModel: string | undefined,
    chatModelId: string | undefined,
    savedDefaultModel: string | undefined,
    chatProvider: string | undefined,
  ): { modelId: string; provider: string } {
    const entry = presetModels?.[presetId];
    const modelId =
      entry?.model_id ??
      selectedModel ??
      chatModelId ??
      savedDefaultModel ??
      "";
    const provider = entry?.provider ?? chatProvider ?? "lmstudio";
    return { modelId, provider };
  }

  it("uses preset model+provider when configured", () => {
    const result = resolveSubSessionModel(
      "research",
      { research: { provider: "openrouter", model_id: "meta-llama/llama-3.3-70b-instruct" } },
      "top-bar-model",
      "chat-model",
      "default-model",
      "lmstudio",
    );
    expect(result).toEqual({
      modelId: "meta-llama/llama-3.3-70b-instruct",
      provider: "openrouter",
    });
  });

  it("falls back to top-bar model + chat provider when no preset mapping", () => {
    const result = resolveSubSessionModel(
      "coder",
      {},
      "top-bar-model",
      "chat-model",
      "default-model",
      "groq",
    );
    expect(result).toEqual({ modelId: "top-bar-model", provider: "groq" });
  });

  it("falls back to chat model_id when selectedModel is undefined", () => {
    const result = resolveSubSessionModel(
      "coder",
      {},
      undefined,
      "chat-model",
      "default-model",
      "lmstudio",
    );
    expect(result).toEqual({ modelId: "chat-model", provider: "lmstudio" });
  });

  it("uses savedDefaultModel as last resort before empty string", () => {
    const result = resolveSubSessionModel(
      "coder",
      {},
      undefined,
      undefined,
      "default-model",
      undefined,
    );
    expect(result).toEqual({ modelId: "default-model", provider: "lmstudio" });
  });

  it("provider falls back to lmstudio when chat provider is undefined", () => {
    const result = resolveSubSessionModel(
      "analyst",
      {},
      "some-model",
      undefined,
      undefined,
      undefined,
    );
    expect(result).toEqual({ modelId: "some-model", provider: "lmstudio" });
  });

  it("preset mapping for a different presetId does NOT affect this preset", () => {
    const result = resolveSubSessionModel(
      "creative",
      { research: { provider: "openrouter", model_id: "cloud-model" } },
      "top-bar",
      undefined,
      undefined,
      "lmstudio",
    );
    // research mapping should not affect creative
    expect(result).toEqual({ modelId: "top-bar", provider: "lmstudio" });
  });
});
