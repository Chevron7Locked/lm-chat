/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useChatCommands — chat-lifecycle command handlers: per-message delete,
 * /clear (+ its confirm-modal state), fork, compact, memory-pin,
 * delete-chat, pin-toggle, and the empty-state "start your first chat"
 * action.
 *
 * Extracted from pages/Chat.tsx — behavior-preserving; every callback body
 * is verbatim from Chat.tsx, only wrapped in this hook. `handleCompact`'s
 * `clearResponseId(chatId)` call clears the cached response-id so the NEXT
 * turn replays with the compaction summary instead of resuming the
 * pre-compaction LM Studio chain; it is imported directly from
 * `@/lib/responseId`, matching the original call site.
 *
 * Several of these handlers are also dispatched from code that stays in
 * Chat.tsx (handlePaletteSelect's slash-command switch; TopBar/Composer/
 * EmptyState render props) — those call sites are unaffected: they just
 * consume the handlers returned here instead of local closures.
 *
 * `handleDeleteChat` and `handlePinToggle` were plain `async function`
 * declarations (not useCallback) in Chat.tsx — preserved as such here, so
 * they're still recreated every render exactly as before.
 */
import { useCallback, useState } from "react";
import type { QueryClient, UseQueryResult } from "@tanstack/react-query";
import type { NavigateFunction } from "react-router-dom";
import type { PushOptions } from "@/stores/toastStore";
import type {
  ChatSummary,
  MessageListResponse,
  useClearChatMessages,
  useCompactChat,
  useCreateChat,
  useDeleteChat,
  useDeleteMessage,
  useForkChat,
  useUpdateChat,
} from "@/hooks/useChats";
import { compactionKeys } from "@/hooks/useCompactions";
import type { usePinInsight } from "@/hooks/useMemory";
import type { ApiError } from "@/lib/api";
import { clearResponseId } from "@/lib/responseId";

export interface UseChatCommandsArgs {
  chatId: number | null;
  currentChat: ChatSummary | undefined;
  messagesData: MessageListResponse | undefined;
  refetchMessages: UseQueryResult<MessageListResponse, ApiError>["refetch"];
  qc: QueryClient;
  push: (opts: PushOptions) => string;
  navigate: NavigateFunction;
  deleteMessage: ReturnType<typeof useDeleteMessage>;
  clearChatMessages: ReturnType<typeof useClearChatMessages>;
  forkChat: ReturnType<typeof useForkChat>;
  compactChat: ReturnType<typeof useCompactChat>;
  pinInsight: ReturnType<typeof usePinInsight>;
  deleteChat: ReturnType<typeof useDeleteChat>;
  updateChat: ReturnType<typeof useUpdateChat>;
  createChat: ReturnType<typeof useCreateChat>;
}

export interface UseChatCommandsResult {
  handleDeleteMessage: (messageId: number) => void;
  confirmClear: boolean;
  setConfirmClear: (next: boolean | ((prev: boolean) => boolean)) => void;
  handleClear: () => void;
  handleClearConfirm: () => Promise<void>;
  handleFork: () => Promise<void>;
  handleCompact: () => Promise<void>;
  handleMemoryPin: (text: string) => Promise<void>;
  handleDeleteChat: () => Promise<void>;
  handlePinToggle: () => Promise<void>;
  handleStartFirstChat: () => Promise<void>;
}

export function useChatCommands({
  chatId,
  currentChat,
  messagesData,
  refetchMessages,
  qc,
  push,
  navigate,
  deleteMessage,
  clearChatMessages,
  forkChat,
  compactChat,
  pinInsight,
  deleteChat,
  updateChat,
  createChat,
}: UseChatCommandsArgs): UseChatCommandsResult {
  // Per-message delete handler (action-bar Delete). Placed after `push`
  // is declared so the success/error toasts can use it.
  const handleDeleteMessage = useCallback(
    (messageId: number): void => {
      deleteMessage.mutate(messageId, {
        onSuccess: () => {
          push({ variant: "success", message: "Message deleted." });
        },
        onError: () => {
          push({ variant: "error", message: "Couldn't delete that message." });
        },
      });
    },
    [deleteMessage, push],
  );

  // /clear — confirm modal visibility for clearing this chat's history.
  const [confirmClear, setConfirmClear] = useState(false);

  // /clear — open the confirm modal; the destructive delete runs only
  // after the user confirms (mirrors the regenerate confirm flow).
  const handleClear = useCallback((): void => {
    if (chatId === null) return;
    setConfirmClear(true);
  }, [chatId]);

  const handleClearConfirm = useCallback(async (): Promise<void> => {
    if (chatId === null) {
      setConfirmClear(false);
      return;
    }
    try {
      const { cleared } = await clearChatMessages.mutateAsync(chatId);
      await refetchMessages();
      push({
        variant: "success",
        message:
          cleared === 0
            ? "Chat is already empty."
            : `Cleared ${String(cleared)} message${cleared === 1 ? "" : "s"}.`,
      });
    } catch {
      push({ variant: "error", message: "Couldn't clear chat history — try again." });
    } finally {
      setConfirmClear(false);
    }
  }, [chatId, clearChatMessages, refetchMessages, push]);

  const handleFork = useCallback(async (): Promise<void> => {
    if (chatId === null) return;
    // Fork at the last message in the current chat (fork-all-messages idiom).
    const msgs = messagesData?.messages ?? [];
    const lastMsg = msgs[msgs.length - 1];
    const lastMsgId = lastMsg !== undefined ? lastMsg.id : 0;
    try {
      const forked = await forkChat.mutateAsync({ at_message_id: lastMsgId });
      push({ variant: "success", message: "Chat forked." });
      void navigate(`/chats/${String(forked.id)}`);
    } catch {
      push({ variant: "error", message: "Fork failed." });
    }
  }, [chatId, messagesData, forkChat, navigate, push]);

  const handleCompact = useCallback(async (): Promise<void> => {
    if (chatId === null) return;
    try {
      // 4096-token target is a sensible default for most local models;
      // a future Settings panel can expose this per-chat.
      const result = await compactChat.mutateAsync({ target_tokens: 4096 });
      // Clear the cached response-id so the NEXT turn replays with the
      // summary instead of resuming the pre-compaction LM Studio chain.
      // Without this the whole feature is a no-op.
      clearResponseId(chatId);
      // Surface the new tab.
      void qc.invalidateQueries({ queryKey: compactionKeys.list(chatId) });
      // Honest toast from the real response.
      if (result.archived_count === 0) {
        push({ variant: "info", message: "Already compact — nothing to trim." });
      } else {
        push({
          variant: "success",
          message: `Compacted ${String(result.archived_count)} message${result.archived_count === 1 ? "" : "s"} (~${result.original_token_count.toLocaleString()} → ~${result.summary_token_count.toLocaleString()} tokens).`,
        });
      }
      void refetchMessages();
    } catch {
      push({ variant: "error", message: "Compact failed." });
    }
  }, [chatId, compactChat, refetchMessages, push, qc]);

  const handleMemoryPin = useCallback(
    async (text: string): Promise<void> => {
      try {
        await pinInsight.mutateAsync({ text });
        push({ variant: "success", message: "Insight pinned to memory." });
      } catch {
        push({
          variant: "error",
          message: "Couldn't pin that insight — try again.",
        });
      }
    },
    [pinInsight, push],
  );

  async function handleDeleteChat(): Promise<void> {
    if (chatId === null) return;
    try {
      await deleteChat.mutateAsync(chatId);
      push({ variant: "info", message: "Chat deleted." });
      void navigate("/");
    } catch {
      push({ variant: "error", message: "Delete failed." });
    }
  }

  async function handlePinToggle(): Promise<void> {
    if (chatId === null || currentChat === undefined) return;
    try {
      await updateChat.mutateAsync({ pinned: !currentChat.pinned });
    } catch {
      push({ variant: "error", message: "Couldn't update chat — try again." });
    }
  }

  // Empty-state "Start your first chat" action.
  const handleStartFirstChat = useCallback(async (): Promise<void> => {
    try {
      const created = await createChat.mutateAsync({ title: "New Chat" });
      void navigate(`/chats/${String(created.id)}`);
    } catch {
      push({
        variant: "error",
        message: "Couldn't create a new chat — try again.",
      });
    }
  }, [createChat, navigate, push]);

  return {
    handleDeleteMessage,
    confirmClear,
    setConfirmClear,
    handleClear,
    handleClearConfirm,
    handleFork,
    handleCompact,
    handleMemoryPin,
    handleDeleteChat,
    handlePinToggle,
    handleStartFirstChat,
  };
}
