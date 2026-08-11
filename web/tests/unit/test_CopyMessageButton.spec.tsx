/* SPDX-License-Identifier: Apache-2.0 */
/**
 * B-copy-message — CopyMessageButton + ChatMessage integration.
 *
 * Spec ACs covered:
 *   - 1: renders on user / assistant; not on system / tool
 *   - 2: Copy is the LAST child of the action row (Edit → Regenerate → Copy)
 *   - 3: writeText receives `message.content` raw
 *   - 4: success → success toast + Check icon swap (aria-label flips)
 *   - 5: navigator.clipboard undefined → error toast, no writeText
 *   - 6: streaming row → no Copy
 *   - 7: empty content → no Copy
 *   - 8: whitespace-only content → DOES copy as-is
 *   - 9: optimistic ids supported (e.g. "streaming"-id-with-streaming-false)
 *   - failure: writeText rejects (NotAllowedError) → error toast, no icon swap
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, waitFor, act } from "@testing-library/react";
import { ChatMessage } from "@/components/ChatMessage";
import { useToastStore } from "@/stores/toastStore";

function lastToast(): { variant?: string; message?: string } | undefined {
  const toasts = useToastStore.getState().toasts;
  return toasts[toasts.length - 1];
}

function clearToasts(): void {
  useToastStore.getState().clear();
}

describe("CopyMessageButton — render gating (AC 1, 6, 7, 9)", () => {
  beforeEach(() => clearToasts());

  it("renders on user-role persisted messages", () => {
    render(
      <ChatMessage
        message={{ id: 7, role: "user", content: "hi" }}
        streamingActive={false}
      />,
    );
    expect(screen.getByTestId("chat-message-copy-btn-7")).toBeTruthy();
  });

  it("renders on assistant-role persisted messages", () => {
    render(
      <ChatMessage
        message={{ id: 9, role: "assistant", content: "ok" }}
        streamingActive={false}
      />,
    );
    expect(screen.getByTestId("chat-message-copy-btn-9")).toBeTruthy();
  });

  it("renders on optimistic string ids when streaming flag is false", () => {
    render(
      <ChatMessage
        message={{ id: "streaming", role: "assistant", content: "done", streaming: false }}
        streamingActive={false}
      />,
    );
    expect(screen.getByTestId("chat-message-copy-btn-streaming")).toBeTruthy();
  });

  it("does NOT render on system role", () => {
    render(
      <ChatMessage
        message={{ id: 1, role: "system", content: "note" }}
        streamingActive={false}
      />,
    );
    expect(screen.queryByTestId(/chat-message-copy-btn-/)).toBeNull();
  });

  it("does NOT render on tool role", () => {
    render(
      <ChatMessage
        message={{ id: 2, role: "tool", content: "tool out" }}
        streamingActive={false}
      />,
    );
    expect(screen.queryByTestId(/chat-message-copy-btn-/)).toBeNull();
  });

  it("does NOT render while message.streaming === true", () => {
    render(
      <ChatMessage
        message={{ id: 3, role: "assistant", content: "partial", streaming: true }}
        streamingActive={false}
      />,
    );
    expect(screen.queryByTestId(/chat-message-copy-btn-/)).toBeNull();
  });

  it("does NOT render when content is empty", () => {
    render(
      <ChatMessage
        message={{ id: 4, role: "user", content: "" }}
        streamingActive={false}
      />,
    );
    expect(screen.queryByTestId(/chat-message-copy-btn-/)).toBeNull();
  });
});

describe("CopyMessageButton — order in action row (AC 2)", () => {
  beforeEach(() => clearToasts());

  it("renders Copy as the LAST child of the action row on assistant turns", () => {
    render(
      <ChatMessage
        message={{ id: 9, role: "assistant", content: "ok" }}
        streamingActive={false}
        onRegenerate={vi.fn()}
      />,
    );
    const row = screen.getByTestId("chat-message-copy-btn-9").parentElement;
    expect(row).toBeTruthy();
    const buttons = Array.from(row!.querySelectorAll("button"));
    const last = buttons[buttons.length - 1];
    expect(last?.getAttribute("data-testid")).toBe("chat-message-copy-btn-9");
  });
});

describe("CopyMessageButton — click behavior (AC 3, 4, 5, 8)", () => {
  let originalClipboard: typeof navigator.clipboard | undefined;

  beforeEach(() => {
    clearToasts();
    originalClipboard = navigator.clipboard;
  });
  afterEach(() => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: originalClipboard,
    });
  });

  it("writes message.content raw on success + success toast", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });

    render(
      <ChatMessage
        message={{ id: 7, role: "user", content: "## Heading\n\n- Item 1\n- Item 2" }}
        streamingActive={false}
      />,
    );
    fireEvent.click(screen.getByTestId("chat-message-copy-btn-7"));
    await waitFor(() => expect(writeText).toHaveBeenCalledOnce());
    expect(writeText).toHaveBeenCalledWith("## Heading\n\n- Item 1\n- Item 2");
    await waitFor(() => {
      expect(lastToast()?.variant).toBe("success");
      expect(lastToast()?.message).toBe("Message copied.");
    });
  });

  it("flips aria-label to 'Copied!' after success", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(
      <ChatMessage
        message={{ id: 8, role: "assistant", content: "ok" }}
        streamingActive={false}
      />,
    );
    const btn = screen.getByTestId("chat-message-copy-btn-8");
    expect(btn.getAttribute("aria-label")).toBe("Copy message");
    fireEvent.click(btn);
    await waitFor(() => {
      expect(btn.getAttribute("aria-label")).toBe("Copied!");
    });
  });

  it("copies whitespace-only content as-is (no trim)", async () => {
    const writeText = vi.fn(async () => undefined);
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(
      <ChatMessage
        message={{ id: 10, role: "user", content: "   " }}
        streamingActive={false}
      />,
    );
    fireEvent.click(screen.getByTestId("chat-message-copy-btn-10"));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("   "));
  });

  it("undefined clipboard → error toast, no writeText invocation", async () => {
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: undefined,
    });
    render(
      <ChatMessage
        message={{ id: 11, role: "user", content: "x" }}
        streamingActive={false}
      />,
    );
    fireEvent.click(screen.getByTestId("chat-message-copy-btn-11"));
    await waitFor(() => {
      expect(lastToast()?.variant).toBe("error");
      expect(lastToast()?.message).toBe("Couldn't copy that — try again.");
    });
  });

  it("writeText rejection (NotAllowedError) → error toast, no icon swap", async () => {
    const writeText = vi.fn(async () => {
      throw new DOMException("denied", "NotAllowedError");
    });
    Object.defineProperty(navigator, "clipboard", {
      configurable: true,
      value: { writeText },
    });
    render(
      <ChatMessage
        message={{ id: 12, role: "user", content: "x" }}
        streamingActive={false}
      />,
    );
    const btn = screen.getByTestId("chat-message-copy-btn-12");
    fireEvent.click(btn);
    await waitFor(() => {
      expect(lastToast()?.variant).toBe("error");
      expect(lastToast()?.message).toBe("Couldn't copy that — try again.");
    });
    expect(btn.getAttribute("aria-label")).toBe("Copy message");
  });
});
