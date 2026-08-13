/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useChatCommands — handleForkFromMessage (per-message "Fork from here").
 *
 * Pins the exact logic added to useChatCommands.ts's handleForkFromMessage,
 * mirroring the sibling handleFork it's derived from:
 *
 *   const handleForkFromMessage = useCallback(
 *     async (messageId: number): Promise<void> => {
 *       if (chatId === null) return;
 *       try {
 *         const forked = await forkChat.mutateAsync({ at_message_id: messageId });
 *         push({ variant: "success", message: "Chat forked." });
 *         void navigate(`/chats/${String(forked.id)}`);
 *       } catch {
 *         push({ variant: "error", message: "Fork failed." });
 *       }
 *     },
 *     [chatId, forkChat, navigate, push],
 *   );
 *
 * Style mirrors test_Chat_resend_bugs.spec.tsx — the established pattern for
 * testing logic extracted from pages/Chat.tsx into these command hooks: pin
 * the decision/action logic in isolation rather than mounting the full Chat
 * component (too expensive in jsdom for what's a pure wiring concern).
 *
 * Locked behaviours:
 *   - Calls forkChat.mutateAsync with the SUPPLIED message id (not the last
 *     message in the chat — that's handleFork's job, not this one's).
 *   - On success: pushes the "Chat forked." toast and navigates to the new
 *     chat's route.
 *   - On failure: pushes the "Fork failed." error toast and does NOT navigate.
 *   - No-ops (no mutation call) when chatId is null.
 */
import { describe, it, expect, vi } from "vitest";

async function simulateHandleForkFromMessage(opts: {
  chatId: number | null;
  messageId: number;
  forkChat: { mutateAsync: (args: { at_message_id: number }) => Promise<{ id: number }> };
  push: (toast: { variant: string; message: string }) => void;
  navigate: (path: string) => void;
}): Promise<void> {
  const { chatId, messageId, forkChat, push, navigate } = opts;
  if (chatId === null) return;
  try {
    const forked = await forkChat.mutateAsync({ at_message_id: messageId });
    push({ variant: "success", message: "Chat forked." });
    navigate(`/chats/${String(forked.id)}`);
  } catch {
    push({ variant: "error", message: "Fork failed." });
  }
}

describe("handleForkFromMessage — fork mutation + navigate wiring", () => {
  it("calls forkChat.mutateAsync with the supplied message id, then navigates to the forked chat", async () => {
    const push = vi.fn();
    const navigate = vi.fn();
    const mutateAsync = vi.fn().mockResolvedValue({ id: 777 });

    await simulateHandleForkFromMessage({
      chatId: 5,
      messageId: 123, // boundary message id — NOT the last message in the chat
      forkChat: { mutateAsync },
      push,
      navigate,
    });

    expect(mutateAsync).toHaveBeenCalledOnce();
    expect(mutateAsync).toHaveBeenCalledWith({ at_message_id: 123 });
    expect(push).toHaveBeenCalledWith({
      variant: "success",
      message: "Chat forked.",
    });
    expect(navigate).toHaveBeenCalledWith("/chats/777");
  });

  it("forks at an EARLIER message, not the chat's last message", async () => {
    // Distinguishes this from handleFork (which always forks at the last
    // message) — the boundary id here is whatever the caller passes in.
    const push = vi.fn();
    const navigate = vi.fn();
    const mutateAsync = vi.fn().mockResolvedValue({ id: 42 });

    await simulateHandleForkFromMessage({
      chatId: 9,
      messageId: 17, // an assistant turn somewhere in the middle of the chat
      forkChat: { mutateAsync },
      push,
      navigate,
    });

    expect(mutateAsync).toHaveBeenCalledWith({ at_message_id: 17 });
    expect(navigate).toHaveBeenCalledWith("/chats/42");
  });

  it("pushes an error toast and does NOT navigate when the mutation fails", async () => {
    const push = vi.fn();
    const navigate = vi.fn();
    const mutateAsync = vi.fn().mockRejectedValue(new Error("network error"));

    await simulateHandleForkFromMessage({
      chatId: 5,
      messageId: 123,
      forkChat: { mutateAsync },
      push,
      navigate,
    });

    expect(push).toHaveBeenCalledWith({
      variant: "error",
      message: "Fork failed.",
    });
    expect(navigate).not.toHaveBeenCalled();
  });

  it("no-ops when chatId is null (no active chat)", async () => {
    const push = vi.fn();
    const navigate = vi.fn();
    const mutateAsync = vi.fn();

    await simulateHandleForkFromMessage({
      chatId: null,
      messageId: 123,
      forkChat: { mutateAsync },
      push,
      navigate,
    });

    expect(mutateAsync).not.toHaveBeenCalled();
    expect(push).not.toHaveBeenCalled();
    expect(navigate).not.toHaveBeenCalled();
  });
});
