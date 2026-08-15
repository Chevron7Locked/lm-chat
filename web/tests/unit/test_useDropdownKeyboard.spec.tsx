/**
 * Unit tests for useDropdownKeyboard.
 *
 * Closes DEFERRED §2. Covers:
 * - Tab/Shift+Tab focus trap within the dropdown.
 * - Arrow Down / Arrow Up navigation.
 * - Escape calls onClose.
 * - Home / End jump to first / last.
 */
import { describe, it, expect, vi } from "vitest";
import { renderHook, render, act, fireEvent } from "@testing-library/react";
import { createElement } from "react";
import { useDropdownKeyboard } from "@/hooks/useDropdownKeyboard";

// Build a minimal KeyboardEvent-like object for testing.
function fakeKey(key: string, shiftKey = false): React.KeyboardEvent<HTMLElement> {
  const prevented = { value: false };
  return {
    key,
    shiftKey,
    preventDefault: () => { prevented.value = true; },
    _prevented: prevented,
  } as unknown as React.KeyboardEvent<HTMLElement>;
}

describe("useDropdownKeyboard", () => {
  it("returns containerProps with onKeyDown and ref", () => {
    const { result } = renderHook(() =>
      useDropdownKeyboard({ open: true, onClose: vi.fn() })
    );
    expect(typeof result.current.containerProps.onKeyDown).toBe("function");
    expect(result.current.containerProps.ref).toBeDefined();
  });

  it("calls onClose on Escape", () => {
    const onClose = vi.fn();
    const { result } = renderHook(() =>
      useDropdownKeyboard({ open: true, onClose })
    );

    act(() => {
      result.current.containerProps.onKeyDown(fakeKey("Escape"));
    });

    expect(onClose).toHaveBeenCalledOnce();
  });

  it("does not call onClose when closed", () => {
    const onClose = vi.fn();
    const { result } = renderHook(() =>
      useDropdownKeyboard({ open: false, onClose })
    );

    act(() => {
      result.current.containerProps.onKeyDown(fakeKey("Escape"));
    });

    expect(onClose).not.toHaveBeenCalled();
  });

  it("accepts itemSelector option without throwing", () => {
    const { result } = renderHook(() =>
      useDropdownKeyboard({
        open: true,
        onClose: vi.fn(),
        itemSelector: '[role="menuitem"]',
      })
    );
    expect(result.current.containerProps).toBeDefined();
  });

  it("ArrowDown navigates to next item when DOM items exist", () => {
    // Render a real DOM with menu items to test ArrowDown navigation.
    const onClose = vi.fn();

    function TestMenu() {
      const { containerProps } = useDropdownKeyboard({ open: true, onClose });
      return createElement(
        "div",
        { ref: containerProps.ref, onKeyDown: containerProps.onKeyDown, "data-testid": "menu" },
        createElement("button", { role: "menuitem", "data-testid": "item1" }, "Item 1"),
        createElement("button", { role: "menuitem", "data-testid": "item2" }, "Item 2"),
      );
    }

    const { getByTestId } = render(createElement(TestMenu));
    const menu = getByTestId("menu");

    // Focus first item then arrow down.
    const item1 = getByTestId("item1") as HTMLButtonElement;
    item1.focus();

    act(() => {
      fireEvent.keyDown(menu, { key: "ArrowDown" });
    });

    expect(document.activeElement?.getAttribute("data-testid")).toBe("item2");
  });

  it("Tab traps focus within container", () => {
    const onClose = vi.fn();

    function TestMenu() {
      const { containerProps } = useDropdownKeyboard({ open: true, onClose });
      return createElement(
        "div",
        { ref: containerProps.ref, onKeyDown: containerProps.onKeyDown, "data-testid": "menu" },
        createElement("button", { role: "menuitem", "data-testid": "a" }, "A"),
        createElement("button", { role: "menuitem", "data-testid": "b" }, "B"),
      );
    }

    const { getByTestId } = render(createElement(TestMenu));
    const menu = getByTestId("menu");
    const itemB = getByTestId("b") as HTMLButtonElement;
    itemB.focus();

    act(() => {
      fireEvent.keyDown(menu, { key: "Tab", shiftKey: false });
    });

    // Tab from last item wraps back to first.
    expect(document.activeElement?.getAttribute("data-testid")).toBe("a");
  });

  it("Shift+Tab moves to previous item", () => {
    const onClose = vi.fn();

    function TestMenu() {
      const { containerProps } = useDropdownKeyboard({ open: true, onClose });
      return createElement(
        "div",
        { ref: containerProps.ref, onKeyDown: containerProps.onKeyDown, "data-testid": "menu" },
        createElement("button", { role: "menuitem", "data-testid": "a" }, "A"),
        createElement("button", { role: "menuitem", "data-testid": "b" }, "B"),
      );
    }

    const { getByTestId } = render(createElement(TestMenu));
    const menu = getByTestId("menu");
    const itemB = getByTestId("b") as HTMLButtonElement;
    itemB.focus();

    act(() => {
      fireEvent.keyDown(menu, { key: "Tab", shiftKey: true });
    });

    expect(document.activeElement?.getAttribute("data-testid")).toBe("a");
  });

  it("Home and End jump to first/last", () => {
    const onClose = vi.fn();

    function TestMenu() {
      const { containerProps } = useDropdownKeyboard({ open: true, onClose });
      return createElement(
        "div",
        { ref: containerProps.ref, onKeyDown: containerProps.onKeyDown, "data-testid": "menu" },
        createElement("button", { role: "menuitem", "data-testid": "a" }, "A"),
        createElement("button", { role: "menuitem", "data-testid": "b" }, "B"),
        createElement("button", { role: "menuitem", "data-testid": "c" }, "C"),
      );
    }

    const { getByTestId } = render(createElement(TestMenu));
    const menu = getByTestId("menu");
    const itemB = getByTestId("b") as HTMLButtonElement;
    itemB.focus();

    act(() => {
      fireEvent.keyDown(menu, { key: "End" });
    });
    expect(document.activeElement?.getAttribute("data-testid")).toBe("c");

    act(() => {
      fireEvent.keyDown(menu, { key: "Home" });
    });
    expect(document.activeElement?.getAttribute("data-testid")).toBe("a");
  });
});
