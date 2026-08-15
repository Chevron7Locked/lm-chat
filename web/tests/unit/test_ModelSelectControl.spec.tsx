/**
 * Unit tests for ModelSelectControl — the canonical model-picker.
 *
 * Pins the three behaviors that the consumer sites (Chat.tsx header,
 * Settings → Chat default, Settings → LM Studio default) all rely on.
 * If any of these break, exactly one file changes (this component)
 * instead of three inlined `<select>`s drifting independently.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ModelSelectControl, ModelCapabilityIcons } from "@/components/ModelSelectControl";

describe("ModelSelectControl", () => {
  it("renders a real native <select> (no-defaulting rule)", () => {
    render(
      <ModelSelectControl
        ariaLabel="Model"
        value=""
        onChange={() => {}}
        options={[{ id: "a", label: "Alpha" }]}
      />,
    );
    const select = screen.getByLabelText("Model");
    expect(select.tagName).toBe("SELECT");
  });

  it("applies the canonical `lmchat-model-select` class", () => {
    const { container } = render(
      <ModelSelectControl
        ariaLabel="Model"
        value=""
        onChange={() => {}}
        options={[]}
      />,
    );
    const select = container.querySelector("select");
    if (!select) throw new Error("expected a <select> element to render");
    expect(select.className).toContain("lmchat-model-select");
  });

  it("merges caller-supplied className alongside the canonical class", () => {
    const { container } = render(
      <ModelSelectControl
        ariaLabel="Model"
        value=""
        onChange={() => {}}
        className="lmchat-model-select--mobile-wide"
        options={[]}
      />,
    );
    const select = container.querySelector("select");
    if (!select) throw new Error("expected a <select> element to render");
    expect(select.className).toContain("lmchat-model-select");
    expect(select.className).toContain("lmchat-model-select--mobile-wide");
  });

  it("renders the placeholder as a disabled first option when supplied", () => {
    render(
      <ModelSelectControl
        ariaLabel="Model"
        value=""
        onChange={() => {}}
        placeholder="Pick a model"
        options={[{ id: "a", label: "Alpha" }]}
      />,
    );
    const placeholder = screen.getByText("Pick a model") as HTMLOptionElement;
    expect(placeholder.disabled).toBe(true);
    expect(placeholder.value).toBe("");
  });

  it("omits the placeholder when not supplied", () => {
    render(
      <ModelSelectControl
        ariaLabel="Model"
        value="a"
        onChange={() => {}}
        options={[{ id: "a", label: "Alpha" }]}
      />,
    );
    expect(screen.queryByText("Pick a model")).toBeNull();
  });

  it("renders autoOption as a SELECTABLE first option (distinct from the disabled placeholder)", () => {
    const { container } = render(
      <ModelSelectControl
        ariaLabel="Model"
        value="__auto__"
        onChange={() => {}}
        autoOption={{ value: "__auto__", label: "Auto" }}
        options={[
          { id: "a", label: "Alpha", loaded: true },
          { id: "b", label: "Beta", loaded: false },
        ]}
      />,
    );
    const auto = screen.getByText("Auto") as HTMLOptionElement;
    // Selectable (NOT disabled) — the user must be able to pick it to reset.
    expect(auto.disabled).toBe(false);
    expect(auto.value).toBe("__auto__");
    // Rendered as the very first option, ahead of the loaded/not-loaded split.
    const firstOption = container.querySelector("option");
    expect(firstOption?.textContent).toBe("Auto");
    // Survives the loaded/unloaded optgroup split (a bare prepend would vanish).
    const select = screen.getByLabelText("Model") as HTMLSelectElement;
    expect(select.value).toBe("__auto__");
  });

  it("fires onChange with the autoOption value when the user re-selects Auto", () => {
    const onChange = vi.fn();
    render(
      <ModelSelectControl
        ariaLabel="Model"
        value="a"
        onChange={onChange}
        autoOption={{ value: "__auto__", label: "Auto" }}
        options={[{ id: "a", label: "Alpha" }]}
      />,
    );
    fireEvent.change(screen.getByLabelText("Model"), {
      target: { value: "__auto__" },
    });
    expect(onChange).toHaveBeenCalledWith("__auto__");
  });

  it("renders no Auto entry when autoOption is omitted", () => {
    render(
      <ModelSelectControl
        ariaLabel="Model"
        value="a"
        onChange={() => {}}
        options={[{ id: "a", label: "Alpha" }]}
      />,
    );
    expect(screen.queryByText("Auto")).toBeNull();
  });

  it("renders a flat option list when no loaded/not-loaded split exists", () => {
    const { container } = render(
      <ModelSelectControl
        ariaLabel="Model"
        value=""
        onChange={() => {}}
        options={[
          { id: "a", label: "Alpha" },
          { id: "b", label: "Beta" },
        ]}
      />,
    );
    expect(container.querySelector("optgroup")).toBeNull();
    expect(container.querySelectorAll("option").length).toBe(2);
  });

  it("renders Loaded / Not loaded optgroups when both states are present", () => {
    const { container } = render(
      <ModelSelectControl
        ariaLabel="Model"
        value=""
        onChange={() => {}}
        options={[
          { id: "a", label: "Alpha", loaded: true },
          { id: "b", label: "Beta", loaded: false },
        ]}
      />,
    );
    const groups = container.querySelectorAll("optgroup");
    expect(groups.length).toBe(2);
    expect(groups[0]?.getAttribute("label")).toBe("Loaded");
    expect(groups[1]?.getAttribute("label")).toBe("Not loaded");
  });

  it("calls onChange with the selected value in controlled mode", () => {
    const onChange = vi.fn();
    render(
      <ModelSelectControl
        ariaLabel="Model"
        value="a"
        onChange={onChange}
        options={[
          { id: "a", label: "Alpha" },
          { id: "b", label: "Beta" },
        ]}
      />,
    );
    fireEvent.change(screen.getByLabelText("Model"), {
      target: { value: "b" },
    });
    expect(onChange).toHaveBeenCalledWith("b");
  });

  it("supports uncontrolled mode with defaultValue", () => {
    render(
      <ModelSelectControl
        ariaLabel="Default model"
        defaultValue="b"
        options={[
          { id: "a", label: "Alpha" },
          { id: "b", label: "Beta" },
        ]}
      />,
    );
    const select = screen.getByLabelText("Default model") as HTMLSelectElement;
    expect(select.value).toBe("b");
  });

  it("uncontrolled mode: capability icons track the live selection", () => {
    // Cluster 3a closeout (audit 2026-06-10, Finding 4): selectedCaps was
    // derived from defaultValue only, so changing the selection in
    // uncontrolled mode left the mount-time icons frozen. The control now
    // tracks the live selection in local state synced via onChange.
    render(
      <ModelSelectControl
        ariaLabel="Default model"
        defaultValue="plain"
        testId="uncontrolled-caps"
        options={[
          {
            id: "plain",
            label: "Plain LLM",
            capabilities: {
              vision: false,
              trained_for_tool_use: false,
              reasoning: null,
              embedding: false,
            },
          },
          {
            id: "vision",
            label: "Vision LLM",
            capabilities: {
              vision: true,
              trained_for_tool_use: false,
              reasoning: null,
              embedding: false,
            },
          },
        ]}
      />,
    );
    // Mount-time selection has no active capabilities → no icon row.
    expect(screen.queryByTestId("uncontrolled-caps-cap-icons")).toBeNull();
    // User switches to the vision model — the Eye icon must appear.
    fireEvent.change(screen.getByLabelText("Default model"), {
      target: { value: "vision" },
    });
    expect(screen.getByTestId("uncontrolled-caps-cap-icons")).toBeTruthy();
    expect(screen.getByTestId("cap-icon-vision")).toBeTruthy();
    // The select itself stays uncontrolled and reflects the user's pick.
    const select = screen.getByLabelText("Default model") as HTMLSelectElement;
    expect(select.value).toBe("vision");
  });

  it("forwards the testId, title, and id attributes", () => {
    render(
      <ModelSelectControl
        ariaLabel="Model"
        value=""
        onChange={() => {}}
        options={[]}
        testId="my-test-id"
        title="Tooltip text"
        id="my-html-id"
      />,
    );
    const select = screen.getByTestId("my-test-id");
    expect(select.getAttribute("title")).toBe("Tooltip text");
    expect(select.getAttribute("id")).toBe("my-html-id");
  });

  // Findings 1+2 fix (Round 3, 2026-06-10): ModelCapabilityIcons must render
  // INSIDE ModelSelectControl's output (not just in isolation). Locked decision 1
  // requires lucide-react Eye/Wrench/Brain icons to be visible to users.
  it("test_ModelSelectControl_mounts_capability_icons_in_output: icons render adjacent to select for selected vision model", () => {
    render(
      <ModelSelectControl
        ariaLabel="Model"
        value="vision-model"
        onChange={() => {}}
        options={[
          {
            id: "vision-model",
            label: "Vision LLM",
            capabilities: {
              vision: true,
              trained_for_tool_use: false,
              reasoning: null,
              embedding: false,
            },
          },
          {
            id: "plain-model",
            label: "Plain LLM",
            capabilities: {
              vision: false,
              trained_for_tool_use: false,
              reasoning: null,
              embedding: false,
            },
          },
        ]}
      />,
    );
    // The Eye icon must be in the DOM — rendered by ModelCapabilityIcons
    // adjacent to the <select> inside ModelSelectControl's output.
    expect(screen.getByTestId("cap-icon-vision")).toBeTruthy();
  });

  it("test_ModelSelectControl_mounts_all_icons_for_full_model: Eye+Wrench+Brain render for full-capability model", () => {
    render(
      <ModelSelectControl
        ariaLabel="Model"
        value="full-model"
        onChange={() => {}}
        options={[
          {
            id: "full-model",
            label: "Full LLM",
            capabilities: {
              vision: true,
              trained_for_tool_use: true,
              reasoning: { default: "on", allowed_options: ["off", "on"] },
              embedding: false,
            },
          },
        ]}
      />,
    );
    expect(screen.getByTestId("cap-icon-vision")).toBeTruthy();
    expect(screen.getByTestId("cap-icon-tool")).toBeTruthy();
    expect(screen.getByTestId("cap-icon-reasoning")).toBeTruthy();
  });

  it("test_ModelSelectControl_no_icons_when_no_capability_model_selected: no icons for plain model", () => {
    const { container } = render(
      <ModelSelectControl
        ariaLabel="Model"
        value="plain-model"
        onChange={() => {}}
        options={[
          {
            id: "plain-model",
            label: "Plain LLM",
            capabilities: {
              vision: false,
              trained_for_tool_use: false,
              reasoning: null,
              embedding: false,
            },
          },
        ]}
      />,
    );
    expect(screen.queryByTestId("cap-icon-vision")).toBeNull();
    expect(screen.queryByTestId("cap-icon-tool")).toBeNull();
    expect(screen.queryByTestId("cap-icon-reasoning")).toBeNull();
    // Wrapper span should still be in the DOM (ModelCapabilityIcons returns null,
    // but the span wrapper around the select is always rendered).
    expect(container.querySelector(".lmchat-model-select-wrap")).toBeTruthy();
  });

  // Cluster 3a Task 2 (audit 2026-06-10): capability glyph suffix.
  // Locked decision 1 (revision fix): lucide-react icons (Eye/Wrench/Brain) rendered
  // via ModelCapabilityIcons companion component. Text tokens [V][T][R] are retained
  // inside native <option> elements because SVG cannot render inside <option>.
  it("test_ModelSelectControl_renders_capability_icons: appends capability glyph suffix to option text", () => {
    const { container } = render(
      <ModelSelectControl
        ariaLabel="Model"
        value=""
        onChange={() => {}}
        options={[
          {
            id: "vision-model",
            label: "Vision LLM",
            capabilities: {
              vision: true,
              trained_for_tool_use: false,
              reasoning: null,
              embedding: false,
            },
          },
          {
            id: "tool-model",
            label: "Tool LLM",
            capabilities: {
              vision: false,
              trained_for_tool_use: true,
              reasoning: null,
              embedding: false,
            },
          },
          {
            id: "reasoning-model",
            label: "Reasoning LLM",
            capabilities: {
              vision: false,
              trained_for_tool_use: false,
              reasoning: { default: "medium", allowed_options: ["off", "low", "medium", "high"] },
              embedding: false,
            },
          },
          {
            id: "full-model",
            label: "Full LLM",
            capabilities: {
              vision: true,
              trained_for_tool_use: true,
              reasoning: { default: "on", allowed_options: ["off", "on"] },
              embedding: false,
            },
          },
          {
            id: "plain-model",
            label: "Plain LLM",
            capabilities: {
              vision: false,
              trained_for_tool_use: false,
              reasoning: null,
              embedding: false,
            },
          },
        ]}
      />,
    );
    const options = Array.from(container.querySelectorAll("option"));
    const texts = options.map((o) => o.textContent);
    expect(texts.find((t) => t.includes("Vision LLM"))).toBe("Vision LLM [V]");
    expect(texts.find((t) => t.includes("Tool LLM"))).toBe("Tool LLM [T]");
    expect(texts.find((t) => t.includes("Reasoning LLM"))).toBe("Reasoning LLM [R]");
    expect(texts.find((t) => t.includes("Full LLM"))).toBe("Full LLM [VTR]");
    // No glyph suffix for plain model.
    expect(texts.find((t) => t.includes("Plain LLM"))).toBe("Plain LLM");
  });

  // Locked decision 1 (audit 2026-06-10, Finding 1 fix): ModelCapabilityIcons
  // renders lucide-react Eye/Wrench/Brain icons for the selected model's capabilities.
  // These are the actual SVG icons (not text tokens) mandated by the locked decision.
  describe("ModelCapabilityIcons", () => {
    it("renders Eye icon for vision capability", () => {
      render(
        <ModelCapabilityIcons
          caps={{ vision: true, trained_for_tool_use: false, reasoning: null, embedding: false }}
          data-testid="cap-icons"
        />,
      );
      expect(screen.getByTestId("cap-icon-vision")).toBeTruthy();
      expect(screen.queryByTestId("cap-icon-tool")).toBeNull();
      expect(screen.queryByTestId("cap-icon-reasoning")).toBeNull();
    });

    it("renders Wrench icon for tool_use capability", () => {
      render(
        <ModelCapabilityIcons
          caps={{ vision: false, trained_for_tool_use: true, reasoning: null, embedding: false }}
        />,
      );
      expect(screen.queryByTestId("cap-icon-vision")).toBeNull();
      expect(screen.getByTestId("cap-icon-tool")).toBeTruthy();
      expect(screen.queryByTestId("cap-icon-reasoning")).toBeNull();
    });

    it("renders Brain icon for reasoning capability", () => {
      render(
        <ModelCapabilityIcons
          caps={{
            vision: false,
            trained_for_tool_use: false,
            reasoning: { default: "medium", allowed_options: ["off", "low", "medium", "high"] },
            embedding: false,
          }}
        />,
      );
      expect(screen.queryByTestId("cap-icon-vision")).toBeNull();
      expect(screen.queryByTestId("cap-icon-tool")).toBeNull();
      expect(screen.getByTestId("cap-icon-reasoning")).toBeTruthy();
    });

    it("renders all three icons for a fully-capable model", () => {
      render(
        <ModelCapabilityIcons
          caps={{
            vision: true,
            trained_for_tool_use: true,
            reasoning: { default: "on", allowed_options: ["off", "on"] },
            embedding: false,
          }}
          data-testid="cap-icons-full"
        />,
      );
      expect(screen.getByTestId("cap-icon-vision")).toBeTruthy();
      expect(screen.getByTestId("cap-icon-tool")).toBeTruthy();
      expect(screen.getByTestId("cap-icon-reasoning")).toBeTruthy();
    });

    it("returns null when no capabilities are active", () => {
      const { container } = render(
        <ModelCapabilityIcons
          caps={{ vision: false, trained_for_tool_use: false, reasoning: null, embedding: false }}
        />,
      );
      expect(container.firstChild).toBeNull();
    });

    it("returns null when caps is null", () => {
      const { container } = render(<ModelCapabilityIcons caps={null} />);
      expect(container.firstChild).toBeNull();
    });
  });
});
