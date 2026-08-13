/* SPDX-License-Identifier: Apache-2.0 */
/**
 * ChatMessage — "Fork from here" button behaviour.
 *
 * The backend already supports forking a chat at any message boundary
 * (POST /api/chats/{id}/fork with at_message_id — see ChatService.fork).
 * This locks the per-message affordance that lets the user branch the
 * main thread at a specific assistant turn, alongside the existing
 * Edit / Regenerate / Resend / Copy / Delete action-row buttons.
 *
 * Locked behaviours:
 *   - Fork button renders on assistant-role persisted messages when
 *     onForkFromHere is supplied.
 *   - Fork button is absent on user-role messages.
 *   - Fork button is absent while streamingActive is true.
 *   - Fork button is absent on the in-flight streaming assistant row
 *     (message.streaming === true), even if streamingActive is false.
 *   - Fork button does NOT render when onForkFromHere is not supplied.
 *   - Clicking Fork calls onForkFromHere with the assistant message id.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ChatMessage } from "@/components/ChatMessage";

describe("ChatMessage Fork button", () => {
  it("renders a Fork button on assistant-role persisted messages", () => {
    const handler = vi.fn();
    render(
      <ChatMessage
        message={{ id: 42, role: "assistant", content: "branch me" }}
        streamingActive={false}
        onForkFromHere={handler}
      />,
    );
    expect(screen.getByTestId("chat-message-fork-btn-42")).toBeTruthy();
    expect(screen.getByLabelText("Fork from here")).toBeTruthy();
  });

  it("does NOT render a Fork button on user-role messages", () => {
    const handler = vi.fn();
    render(
      <ChatMessage
        message={{ id: 55, role: "user", content: "prompt" }}
        streamingActive={false}
        onForkFromHere={handler}
      />,
    );
    expect(screen.queryByTestId("chat-message-fork-btn-55")).toBeNull();
  });

  it("hides the Fork button while streamingActive is true", () => {
    const handler = vi.fn();
    render(
      <ChatMessage
        message={{ id: 7, role: "assistant", content: "waiting" }}
        streamingActive
        onForkFromHere={handler}
      />,
    );
    expect(screen.queryByTestId("chat-message-fork-btn-7")).toBeNull();
  });

  it("hides the Fork button on the in-flight streaming row", () => {
    const handler = vi.fn();
    render(
      <ChatMessage
        message={{
          id: "streaming",
          role: "assistant",
          content: "typing…",
          streaming: true,
        }}
        streamingActive
        onForkFromHere={handler}
      />,
    );
    expect(
      screen.queryByTestId("chat-message-fork-btn-streaming"),
    ).toBeNull();
  });

  it("does NOT render a Fork button when onForkFromHere is not supplied", () => {
    render(
      <ChatMessage
        message={{ id: 8, role: "assistant", content: "no handler" }}
        streamingActive={false}
      />,
    );
    expect(screen.queryByTestId("chat-message-fork-btn-8")).toBeNull();
  });

  it("calls onForkFromHere with the assistant message id when clicked", () => {
    const handler = vi.fn();
    render(
      <ChatMessage
        message={{ id: 99, role: "assistant", content: "click me" }}
        streamingActive={false}
        onForkFromHere={handler}
      />,
    );
    fireEvent.click(screen.getByTestId("chat-message-fork-btn-99"));
    expect(handler).toHaveBeenCalledOnce();
    expect(handler).toHaveBeenCalledWith(99);
  });

  it("renders Fork alongside Regenerate on an assistant message", () => {
    const regenHandler = vi.fn();
    const forkHandler = vi.fn();
    render(
      <ChatMessage
        message={{ id: 11, role: "assistant", content: "regeneratable and forkable" }}
        streamingActive={false}
        onRegenerate={regenHandler}
        onForkFromHere={forkHandler}
      />,
    );
    expect(screen.getByTestId("chat-message-regenerate-btn-11")).toBeTruthy();
    expect(screen.getByTestId("chat-message-fork-btn-11")).toBeTruthy();
  });
});
