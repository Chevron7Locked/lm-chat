/* SPDX-License-Identifier: Apache-2.0 */
/**
 * ChatMessage P13l.1 — Edit / Regenerate triggers.
 *
 * Locked behaviours:
 *   - Edit button is rendered for user-role persisted messages.
 *   - Regenerate button is rendered for assistant-role persisted messages.
 *   - Neither button is rendered while streamingActive is true.
 *   - Edit form: save calls onEditUserMessage; empty save guarded.
 *   - Regenerate button triggers onRegenerate with the message id.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ChatMessage } from "@/components/ChatMessage";

describe("ChatMessage edit / regenerate (P13l.1)", () => {
  it("renders an Edit button on user-role persisted messages", () => {
    const handler = vi.fn(async () => {});
    render(
      <ChatMessage
        message={{ id: 7, role: "user", content: "hi" }}
        streamingActive={false}
        onEditUserMessage={handler}
      />,
    );
    expect(screen.getByTestId("chat-message-edit-btn-7")).toBeTruthy();
  });

  it("renders a Regenerate button on assistant-role persisted messages", () => {
    const handler = vi.fn();
    render(
      <ChatMessage
        message={{ id: 9, role: "assistant", content: "ok" }}
        streamingActive={false}
        onRegenerate={handler}
      />,
    );
    expect(screen.getByTestId("chat-message-regenerate-btn-9")).toBeTruthy();
  });

  it("hides Edit / Regenerate while streamingActive is true", () => {
    const edit = vi.fn(async () => {});
    const regen = vi.fn();
    render(
      <>
        <ChatMessage
          message={{ id: 1, role: "user", content: "x" }}
          streamingActive
          onEditUserMessage={edit}
        />
        <ChatMessage
          message={{ id: 2, role: "assistant", content: "y" }}
          streamingActive
          onRegenerate={regen}
        />
      </>,
    );
    expect(screen.queryByTestId("chat-message-edit-btn-1")).toBeNull();
    expect(screen.queryByTestId("chat-message-regenerate-btn-2")).toBeNull();
  });

  it("hides Edit / Regenerate on the optimistic streaming row", () => {
    const regen = vi.fn();
    render(
      <ChatMessage
        message={{ id: "streaming", role: "assistant", content: "", streaming: true }}
        streamingActive
        onRegenerate={regen}
      />,
    );
    expect(screen.queryByTestId("chat-message-regenerate-btn-streaming")).toBeNull();
  });

  it("opens the inline edit form and saves on submit", async () => {
    const handler = vi.fn(async () => {});
    render(
      <ChatMessage
        message={{ id: 5, role: "user", content: "before" }}
        streamingActive={false}
        onEditUserMessage={handler}
      />,
    );
    fireEvent.click(screen.getByTestId("chat-message-edit-btn-5"));
    const ta = screen.getByTestId(
      "chat-message-edit-textarea-5",
    ) as HTMLTextAreaElement;
    expect(ta.value).toBe("before");
    fireEvent.change(ta, { target: { value: "after" } });
    fireEvent.click(screen.getByTestId("chat-message-edit-save-5"));
    await waitFor(() => {
      expect(handler).toHaveBeenCalledWith(5, "after");
    });
  });

  it("blocks an empty save with an inline error message", async () => {
    const handler = vi.fn(async () => {});
    render(
      <ChatMessage
        message={{ id: 6, role: "user", content: "something" }}
        streamingActive={false}
        onEditUserMessage={handler}
      />,
    );
    fireEvent.click(screen.getByTestId("chat-message-edit-btn-6"));
    fireEvent.change(screen.getByTestId("chat-message-edit-textarea-6"), {
      target: { value: "   " },
    });
    fireEvent.click(screen.getByTestId("chat-message-edit-save-6"));
    await waitFor(() => {
      expect(screen.getByRole("alert").textContent).toContain("empty");
    });
    expect(handler).not.toHaveBeenCalled();
  });

  it("cancels editing via the Cancel button", async () => {
    const handler = vi.fn(async () => {});
    render(
      <ChatMessage
        message={{ id: 8, role: "user", content: "kept" }}
        streamingActive={false}
        onEditUserMessage={handler}
      />,
    );
    fireEvent.click(screen.getByTestId("chat-message-edit-btn-8"));
    fireEvent.change(screen.getByTestId("chat-message-edit-textarea-8"), {
      target: { value: "discarded" },
    });
    fireEvent.click(screen.getByTestId("chat-message-edit-cancel-8"));
    await waitFor(() => {
      expect(screen.queryByTestId("chat-message-edit-textarea-8")).toBeNull();
    });
    expect(handler).not.toHaveBeenCalled();
  });

  it("dispatches onRegenerate with the assistant message id", () => {
    const regen = vi.fn();
    render(
      <ChatMessage
        message={{ id: 11, role: "assistant", content: "old" }}
        streamingActive={false}
        onRegenerate={regen}
      />,
    );
    fireEvent.click(screen.getByTestId("chat-message-regenerate-btn-11"));
    expect(regen).toHaveBeenCalledWith(11);
  });
});
