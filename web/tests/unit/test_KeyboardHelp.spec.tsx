/**
 * Unit tests for KeyboardHelp modal (P13d, closes audit K-10).
 *
 * Coverage:
 *  - hidden when open=false
 *  - rendered with dialog role + accessible name when open
 *  - lists every documented shortcut chord + every BUILTIN_COMMANDS slash command
 *  - closes via Esc, backdrop click, and the explicit close button
 *  - clicking inside the card does NOT close the modal
 */
import { describe, it, expect, vi, afterEach } from "vitest";
import { render, screen, fireEvent, cleanup } from "@testing-library/react";
import { KeyboardHelp } from "@/components/KeyboardHelp";
import { BUILTIN_COMMANDS } from "@/components/SlashMenu";

afterEach(() => {
  cleanup();
});

describe("KeyboardHelp", () => {
  it("renders nothing when closed", () => {
    const { container } = render(<KeyboardHelp open={false} onClose={() => undefined} />);
    expect(container.firstChild).toBeNull();
  });

  it("renders a labelled dialog when open", () => {
    render(<KeyboardHelp open onClose={() => undefined} />);
    const dialog = screen.getByRole("dialog", { name: /keyboard shortcuts/i });
    expect(dialog).toBeTruthy();
    // Title is visible in the header.
    expect(screen.getByText(/^Keyboard shortcuts$/)).toBeTruthy();
  });

  it("lists each documented chord", () => {
    render(<KeyboardHelp open onClose={() => undefined} />);
    // Chords are rendered by formatShortcut() using the runtime platform.
    // In jsdom (test env), navigator.platform is not Mac → isMac=false →
    // labels use Ctrl+Key format (no symbols, joined with "+").
    // On a real Mac browser the same chords render as ⌘N, ⌘⇧S etc.
    // We assert the non-Mac format here since tests run in jsdom.
    // Some chords (Esc) appear twice — once in the list, once in the footer
    // hint — so use getAllByText for those and getByText for unique ones.
    expect(screen.getByText("Ctrl+N")).toBeTruthy();
    expect(screen.getByText("Ctrl+Shift+S")).toBeTruthy();
    expect(screen.getByText("Ctrl+,")).toBeTruthy();
    expect(screen.getByText("Ctrl+K")).toBeTruthy();
    expect(screen.getByText("Ctrl+/")).toBeTruthy();
    expect(screen.getByText("Ctrl+Enter")).toBeTruthy();
    expect(screen.getByText("?")).toBeTruthy();
    // "Esc" appears in the shortcut row AND the footer hint.
    expect(screen.getAllByText("Esc").length).toBeGreaterThanOrEqual(1);
  });

  it("lists the focus-mode toggle shortcut", () => {
    render(<KeyboardHelp open onClose={() => undefined} />);
    expect(screen.getByText("Toggle focus mode")).toBeTruthy();
    // {mod:true,key:"."} renders as "Ctrl+." in jsdom (non-Mac).
    expect(screen.getByText("Ctrl+.")).toBeTruthy();
  });

  it("lists every built-in slash command", () => {
    render(<KeyboardHelp open onClose={() => undefined} />);
    for (const cmd of BUILTIN_COMMANDS) {
      // Each command name appears prefixed with a slash in a <kbd>.
      expect(screen.getByText(`/${cmd.name}`)).toBeTruthy();
    }
  });

  it("calls onClose when the ✕ button is clicked", () => {
    const onClose = vi.fn();
    render(<KeyboardHelp open onClose={onClose} />);

    fireEvent.click(screen.getByTestId("keyboard-help-close"));
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("calls onClose when the backdrop is clicked", () => {
    const onClose = vi.fn();
    render(<KeyboardHelp open onClose={onClose} />);

    const dialog = screen.getByRole("dialog", { name: /keyboard shortcuts/i });
    // Click on the dialog backdrop itself (not a descendant).
    fireEvent.click(dialog);
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("does NOT call onClose when the card interior is clicked", () => {
    const onClose = vi.fn();
    render(<KeyboardHelp open onClose={onClose} />);

    fireEvent.click(screen.getByTestId("keyboard-help-card"));
    expect(onClose).not.toHaveBeenCalled();
  });

  it("calls onClose when Esc is pressed while open", () => {
    const onClose = vi.fn();
    render(<KeyboardHelp open onClose={onClose} />);

    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).toHaveBeenCalledOnce();
  });

  it("does NOT call onClose when Esc is pressed while closed", () => {
    const onClose = vi.fn();
    render(<KeyboardHelp open={false} onClose={onClose} />);

    fireEvent.keyDown(window, { key: "Escape" });
    expect(onClose).not.toHaveBeenCalled();
  });
});
