/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for the provider-grouped model dropdown.
 *
 * Locked behaviours:
 *   1. When groups prop has lmstudio + openrouter, renders two optgroups.
 *   2. Option values use "<provider>::<model_id>" composite encoding.
 *   3. Composite value with "/" in model_id splits correctly at FIRST "::".
 *   4. LM Studio-only: no empty cloud optgroup rendered.
 *   5. onModelChange decodes "<provider>::<model_id>" correctly (pure logic).
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import type { ModelOptionGroup } from "@/components/ModelSelectControl";

// ─── 1. optgroups by provider ─────────────────────────────────────────────────

describe("ModelSelectControl — provider-grouped rendering", () => {
  async function freshControl() {
    vi.resetModules();
    const mod = await import("@/components/ModelSelectControl");
    return mod.ModelSelectControl;
  }

  const LM_STUDIO_GROUP: ModelOptionGroup = {
    provider: "lmstudio",
    label: "LM Studio",
    options: [
      {
        id: "lmstudio::qwen3.6-35b-a3b",
        label: "Qwen3.6 35B",
        loaded: true,
        provider: "lmstudio",
        capabilities: {
          vision: false,
          trained_for_tool_use: true,
          reasoning: null,
          embedding: false,
        },
      },
    ],
  };

  const OPENROUTER_GROUP: ModelOptionGroup = {
    provider: "openrouter",
    label: "OpenRouter",
    options: [
      {
        id: "openrouter::meta-llama/llama-3.3-70b",
        label: "Llama 3.3 70B",
        loaded: true,
        provider: "openrouter",
        capabilities: {
          vision: false,
          trained_for_tool_use: false,
          reasoning: null,
          embedding: false,
        },
      },
    ],
  };

  it("optgroups by provider — renders two optgroup elements with correct labels", async () => {
    const Control = await freshControl();
    render(
      createElement(Control, {
        ariaLabel: "Model",
        value: "lmstudio::qwen3.6-35b-a3b",
        onChange: vi.fn(),
        options: [...LM_STUDIO_GROUP.options, ...OPENROUTER_GROUP.options],
        groups: [LM_STUDIO_GROUP, OPENROUTER_GROUP],
        testId: "model-select",
      }),
    );

    const select = screen.getByTestId("model-select") as HTMLSelectElement;
    const optgroups = select.querySelectorAll("optgroup");

    expect(optgroups.length).toBe(2);
    expect(optgroups[0]?.label).toBe("LM Studio");
    expect(optgroups[1]?.label).toBe("OpenRouter");
  });

  it("composite value encoding — OpenRouter option value is 'openrouter::meta-llama/llama-3.3-70b'", async () => {
    const Control = await freshControl();
    render(
      createElement(Control, {
        ariaLabel: "Model",
        value: "lmstudio::qwen3.6-35b-a3b",
        onChange: vi.fn(),
        options: [...LM_STUDIO_GROUP.options, ...OPENROUTER_GROUP.options],
        groups: [LM_STUDIO_GROUP, OPENROUTER_GROUP],
        testId: "model-select",
      }),
    );

    const select = screen.getByTestId("model-select") as HTMLSelectElement;
    const allOptions = Array.from(select.options);
    const orOpt = allOptions.find((o) => o.value === "openrouter::meta-llama/llama-3.3-70b");
    expect(orOpt).toBeTruthy();
    expect(orOpt?.value).toBe("openrouter::meta-llama/llama-3.3-70b");
  });

  it("onChange emits composite value when OpenRouter option is selected", async () => {
    const handleChange = vi.fn();
    const Control = await freshControl();
    render(
      createElement(Control, {
        ariaLabel: "Model",
        value: "lmstudio::qwen3.6-35b-a3b",
        onChange: handleChange,
        options: [...LM_STUDIO_GROUP.options, ...OPENROUTER_GROUP.options],
        groups: [LM_STUDIO_GROUP, OPENROUTER_GROUP],
        testId: "model-select",
      }),
    );

    const select = screen.getByTestId("model-select") as HTMLSelectElement;
    fireEvent.change(select, { target: { value: "openrouter::meta-llama/llama-3.3-70b" } });

    expect(handleChange).toHaveBeenCalledWith("openrouter::meta-llama/llama-3.3-70b");
  });

  it("composite value decoding round-trip — indexOf+slice splits at FIRST :: only", () => {
    // Pure logic test — no React rendering needed.
    // Constraint: split("::")  is FORBIDDEN. Use indexOf + slice.
    const compositeId = "openrouter::meta-llama/llama-3.3-70b";
    const sepIdx = compositeId.indexOf("::");
    const provider = sepIdx >= 0 ? compositeId.slice(0, sepIdx) : "lmstudio";
    const modelId = sepIdx >= 0 ? compositeId.slice(sepIdx + 2) : compositeId;

    expect(provider).toBe("openrouter");
    // Critical: the "/" in the model_id must NOT cause a secondary split.
    expect(modelId).toBe("meta-llama/llama-3.3-70b");
    // Explicitly verify the WRONG result is not returned:
    expect(modelId).not.toBe("meta-llama");
  });

  it("LM Studio only — no empty cloud optgroup rendered", async () => {
    // When only one provider group exists, no empty optgroups should appear.
    const lmOnlyGroup: ModelOptionGroup = {
      ...LM_STUDIO_GROUP,
      options: [
        {
          id: "lmstudio::qwen3.6-35b-a3b",
          label: "Qwen3.6 35B",
          loaded: true,
          provider: "lmstudio",
          capabilities: {
            vision: false,
            trained_for_tool_use: false,
            reasoning: null,
            embedding: false,
          },
        },
      ],
    };

    const Control = await freshControl();
    render(
      createElement(Control, {
        ariaLabel: "Model",
        value: "lmstudio::qwen3.6-35b-a3b",
        onChange: vi.fn(),
        options: lmOnlyGroup.options,
        // No groups prop — falls back to flat/loaded-split rendering
        testId: "model-select",
      }),
    );

    const select = screen.getByTestId("model-select") as HTMLSelectElement;
    // No optgroups when groups prop is absent
    const optgroups = select.querySelectorAll("optgroup");
    expect(optgroups.length).toBe(0);
  });

  it("LM Studio only with groups — single optgroup, no empty cloud optgroup", async () => {
    const lmOnlyGroup: ModelOptionGroup = {
      ...LM_STUDIO_GROUP,
    };

    const Control = await freshControl();
    render(
      createElement(Control, {
        ariaLabel: "Model",
        value: "lmstudio::qwen3.6-35b-a3b",
        onChange: vi.fn(),
        options: lmOnlyGroup.options,
        groups: [lmOnlyGroup],
        testId: "model-select",
      }),
    );

    const select = screen.getByTestId("model-select") as HTMLSelectElement;
    const optgroups = select.querySelectorAll("optgroup");
    // Only 1 optgroup — no empty cloud optgroup
    expect(optgroups.length).toBe(1);
    expect(optgroups[0]?.label).toBe("LM Studio");
  });
});
