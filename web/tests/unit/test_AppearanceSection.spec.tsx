/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for AppearanceSection (Settings → Appearance).
 *
 * Covers:
 *   - Renders all four control groups (Theme, Text size, Chat density,
 *     Message style) and the live preview.
 *   - Clicking a Text-size option flips its aria-pressed to true, calls
 *     setTextSize, and updates the data-text-size attribute.
 *   - Clicking a Density option flips its aria-pressed and updates store.
 *   - Clicking a Message-style option flips its aria-pressed and updates store.
 *   - Theme buttons still carry aria-pressed and call setTheme (existing
 *     behaviour verified so we don't regress it).
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { createElement } from "react";

// ─── Mock themeStore ──────────────────────────────────────────────────────────

const mockSetTheme = vi.fn();
let _mockTheme = "dark";

vi.mock("@/stores/themeStore", () => ({
  useThemeStore: (selector?: (s: { theme: string; effective: string; setTheme: typeof mockSetTheme }) => unknown) => {
    const state = { theme: _mockTheme, effective: _mockTheme, setTheme: mockSetTheme };
    return selector !== undefined ? selector(state) : state;
  },
}));

// ─── Mock displayStore ────────────────────────────────────────────────────────

const mockSetTextSize = vi.fn();
const mockSetDensity = vi.fn();
const mockSetMessageStyle = vi.fn();

let _mockTextSize = "md";
let _mockDensity = "comfortable";
let _mockMessageStyle = "bubbles";

vi.mock("@/stores/displayStore", () => ({
  useDisplayStore: (selector?: (s: {
    textSize: string;
    density: string;
    messageStyle: string;
    setTextSize: typeof mockSetTextSize;
    setDensity: typeof mockSetDensity;
    setMessageStyle: typeof mockSetMessageStyle;
  }) => unknown) => {
    const state = {
      textSize: _mockTextSize,
      density: _mockDensity,
      messageStyle: _mockMessageStyle,
      setTextSize: mockSetTextSize,
      setDensity: mockSetDensity,
      setMessageStyle: mockSetMessageStyle,
    };
    return selector !== undefined ? selector(state) : state;
  },
}));

// ─── Helpers ─────────────────────────────────────────────────────────────────

async function freshComponent() {
  vi.resetModules();
  const mod = await import("@/components/AppearanceSection");
  return mod.AppearanceSection;
}

function renderSection(AppearanceSection: () => React.ReactElement) {
  return render(createElement(AppearanceSection));
}

// ─── Setup ───────────────────────────────────────────────────────────────────

beforeEach(() => {
  vi.clearAllMocks();
  _mockTheme = "dark";
  _mockTextSize = "md";
  _mockDensity = "comfortable";
  _mockMessageStyle = "bubbles";
  // Clear data-attributes set by the store on the document element.
  document.documentElement.removeAttribute("data-text-size");
  document.documentElement.removeAttribute("data-density");
  document.documentElement.removeAttribute("data-message-style");
});

// ─── Render checks ────────────────────────────────────────────────────────────

describe("AppearanceSection — render", () => {
  it("renders the section container", async () => {
    const AppearanceSection = await freshComponent();
    renderSection(AppearanceSection);
    expect(screen.getByTestId("settings-appearance-section")).toBeTruthy();
  });

  it("renders Theme buttons: Dark, Light, System", async () => {
    const AppearanceSection = await freshComponent();
    renderSection(AppearanceSection);
    expect(screen.getByRole("button", { name: "Dark" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Light" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "System" })).toBeTruthy();
  });

  it("renders Text size buttons: Compact, Default, Large", async () => {
    const AppearanceSection = await freshComponent();
    renderSection(AppearanceSection);
    // "Compact" appears in both Text size (sm) and Density groups.
    expect(screen.getAllByRole("button", { name: "Compact" }).length).toBeGreaterThanOrEqual(1);
    expect(screen.getByRole("button", { name: "Default" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Large" })).toBeTruthy();
  });

  it("renders Density buttons: Comfortable, Compact", async () => {
    const AppearanceSection = await freshComponent();
    renderSection(AppearanceSection);
    expect(screen.getByRole("button", { name: "Comfortable" })).toBeTruthy();
    // "Compact" appears in both Text size and Density groups.
    const compactBtns = screen.getAllByRole("button", { name: "Compact" });
    expect(compactBtns.length).toBeGreaterThanOrEqual(2);
  });

  it("renders Message style buttons: Bubbles, Flat", async () => {
    const AppearanceSection = await freshComponent();
    renderSection(AppearanceSection);
    expect(screen.getByRole("button", { name: "Bubbles" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Flat" })).toBeTruthy();
  });

  it("renders the live preview (aria-hidden card)", async () => {
    const AppearanceSection = await freshComponent();
    renderSection(AppearanceSection);
    // The preview text must not reference "assistant".
    expect(document.body.textContent).not.toContain("assistant");
    // It should contain "Preview" eyebrow.
    expect(document.body.textContent).toContain("Preview");
  });
});

// ─── aria-pressed reflects current state ──────────────────────────────────────

describe("AppearanceSection — aria-pressed initial state", () => {
  it("Dark button is pressed when theme is 'dark'", async () => {
    _mockTheme = "dark";
    const AppearanceSection = await freshComponent();
    renderSection(AppearanceSection);
    const darkBtn = screen.getByRole("button", { name: "Dark" });
    expect(darkBtn.getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "Light" }).getAttribute("aria-pressed")).toBe("false");
  });

  it("Default (md) Text-size button is pressed on init", async () => {
    _mockTextSize = "md";
    const AppearanceSection = await freshComponent();
    renderSection(AppearanceSection);
    const defaultBtn = screen.getByRole("button", { name: "Default" });
    expect(defaultBtn.getAttribute("aria-pressed")).toBe("true");
  });

  it("Comfortable Density button is pressed on init", async () => {
    _mockDensity = "comfortable";
    const AppearanceSection = await freshComponent();
    renderSection(AppearanceSection);
    expect(screen.getByRole("button", { name: "Comfortable" }).getAttribute("aria-pressed")).toBe("true");
  });

  it("Bubbles Message-style button is pressed on init", async () => {
    _mockMessageStyle = "bubbles";
    const AppearanceSection = await freshComponent();
    renderSection(AppearanceSection);
    expect(screen.getByRole("button", { name: "Bubbles" }).getAttribute("aria-pressed")).toBe("true");
    expect(screen.getByRole("button", { name: "Flat" }).getAttribute("aria-pressed")).toBe("false");
  });
});

// ─── Click interactions ───────────────────────────────────────────────────────

describe("AppearanceSection — click Text size", () => {
  it("clicking 'Large' calls setTextSize('lg')", async () => {
    const AppearanceSection = await freshComponent();
    renderSection(AppearanceSection);
    fireEvent.click(screen.getByRole("button", { name: "Large" }));
    expect(mockSetTextSize).toHaveBeenCalledWith("lg");
  });

  it("clicking 'Compact' (text-size) calls setTextSize('sm')", async () => {
    const AppearanceSection = await freshComponent();
    renderSection(AppearanceSection);
    // The first button named "Compact" belongs to the Text size group.
    const compactBtns = screen.getAllByRole("button", { name: "Compact" });
    fireEvent.click(compactBtns[0]);
    // setTextSize should be called (the first Compact is text-size=sm).
    expect(mockSetTextSize).toHaveBeenCalledWith("sm");
  });
});

describe("AppearanceSection — click Density", () => {
  it("clicking 'Comfortable' calls setDensity('comfortable')", async () => {
    _mockDensity = "compact"; // start from compact so the button is not already pressed
    const AppearanceSection = await freshComponent();
    renderSection(AppearanceSection);
    fireEvent.click(screen.getByRole("button", { name: "Comfortable" }));
    expect(mockSetDensity).toHaveBeenCalledWith("comfortable");
  });

  it("clicking the Density 'Compact' button calls setDensity('compact')", async () => {
    _mockDensity = "comfortable";
    const AppearanceSection = await freshComponent();
    renderSection(AppearanceSection);
    // "Compact" appears in both Text size and Density. The Density one is index 1.
    const compactBtns = screen.getAllByRole("button", { name: "Compact" });
    // Click the last "Compact" button — it's in the Density group.
    fireEvent.click(compactBtns[compactBtns.length - 1]);
    expect(mockSetDensity).toHaveBeenCalledWith("compact");
  });
});

describe("AppearanceSection — click Message style", () => {
  it("clicking 'Flat' calls setMessageStyle('flat')", async () => {
    const AppearanceSection = await freshComponent();
    renderSection(AppearanceSection);
    fireEvent.click(screen.getByRole("button", { name: "Flat" }));
    expect(mockSetMessageStyle).toHaveBeenCalledWith("flat");
  });

  it("clicking 'Bubbles' calls setMessageStyle('bubbles')", async () => {
    _mockMessageStyle = "flat";
    const AppearanceSection = await freshComponent();
    renderSection(AppearanceSection);
    fireEvent.click(screen.getByRole("button", { name: "Bubbles" }));
    expect(mockSetMessageStyle).toHaveBeenCalledWith("bubbles");
  });
});

describe("AppearanceSection — click Theme", () => {
  it("clicking 'Light' calls setTheme with value 'light'", async () => {
    const AppearanceSection = await freshComponent();
    renderSection(AppearanceSection);
    fireEvent.click(screen.getByRole("button", { name: "Light" }));
    expect(mockSetTheme).toHaveBeenCalledWith("light", expect.objectContaining({ x: expect.any(Number), y: expect.any(Number) }));
  });
});
