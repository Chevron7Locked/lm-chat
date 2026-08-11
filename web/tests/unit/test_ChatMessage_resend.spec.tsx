/* SPDX-License-Identifier: Apache-2.0 */
/**
 * ChatMessage — Resend button behaviour.
 *
 * Locked behaviours:
 *   - Resend button renders on user-role persisted messages when onResend is supplied.
 *   - Resend button is absent on assistant-role messages.
 *   - Resend button is absent while streamingActive is true.
 *   - Clicking Resend triggers onResend with the user message id.
 *   - Resend button does NOT render when onResend is not supplied.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ChatMessage } from "@/components/ChatMessage";

describe("ChatMessage Resend button", () => {
  it("renders a Resend button on user-role persisted messages", () => {
    const handler = vi.fn();
    render(
      <ChatMessage
        message={{ id: 42, role: "user", content: "send me again" }}
        streamingActive={false}
        onResend={handler}
      />,
    );
    expect(screen.getByTestId("chat-message-resend-btn-42")).toBeTruthy();
    expect(
      screen.getByLabelText("Resend message"),
    ).toBeTruthy();
  });

  it("does NOT render a Resend button on assistant-role messages", () => {
    const handler = vi.fn();
    render(
      <ChatMessage
        message={{ id: 55, role: "assistant", content: "response" }}
        streamingActive={false}
        onResend={handler}
      />,
    );
    expect(screen.queryByTestId("chat-message-resend-btn-55")).toBeNull();
  });

  it("hides the Resend button while streamingActive is true", () => {
    const handler = vi.fn();
    render(
      <ChatMessage
        message={{ id: 7, role: "user", content: "waiting" }}
        streamingActive
        onResend={handler}
      />,
    );
    expect(screen.queryByTestId("chat-message-resend-btn-7")).toBeNull();
  });

  it("does NOT render a Resend button when onResend is not supplied", () => {
    render(
      <ChatMessage
        message={{ id: 8, role: "user", content: "no handler" }}
        streamingActive={false}
      />,
    );
    expect(screen.queryByTestId("chat-message-resend-btn-8")).toBeNull();
  });

  it("calls onResend with the user message id when clicked", () => {
    const handler = vi.fn();
    render(
      <ChatMessage
        message={{ id: 99, role: "user", content: "click me" }}
        streamingActive={false}
        onResend={handler}
      />,
    );
    fireEvent.click(screen.getByTestId("chat-message-resend-btn-99"));
    expect(handler).toHaveBeenCalledOnce();
    expect(handler).toHaveBeenCalledWith(99);
  });

  it("renders both Edit and Resend buttons on a user message", () => {
    const editHandler = vi.fn(async () => {});
    const resendHandler = vi.fn();
    render(
      <ChatMessage
        message={{ id: 11, role: "user", content: "editable and resendable" }}
        streamingActive={false}
        onEditUserMessage={editHandler}
        onResend={resendHandler}
      />,
    );
    expect(screen.getByTestId("chat-message-edit-btn-11")).toBeTruthy();
    expect(screen.getByTestId("chat-message-resend-btn-11")).toBeTruthy();
  });
});
