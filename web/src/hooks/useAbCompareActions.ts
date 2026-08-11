/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useAbCompareActions — A/B compare mode: starting a comparison run and
 * persisting the chosen pane's response into chat history.
 *
 * Extracted from pages/Chat.tsx — behavior-preserving; both callback bodies
 * are verbatim from Chat.tsx, only wrapped in this hook.
 */
import { useCallback } from "react";
import type { UseQueryResult } from "@tanstack/react-query";
import type {
  MessageListResponse,
  useAppendMessage,
  useUpdateChat,
} from "@/hooks/useChats";
import type { PushOptions } from "@/stores/toastStore";
import type { ApiError } from "@/lib/api";

export interface UseAbCompareActionsArgs {
  chatId: number | null;
  updateChat: ReturnType<typeof useUpdateChat>;
  appendMessage: ReturnType<typeof useAppendMessage>;
  refetchMessages: UseQueryResult<MessageListResponse, ApiError>["refetch"];
  push: (opts: PushOptions) => string;
}

export interface UseAbCompareActionsResult {
  handleABCompareStart: (modelA: string, modelB: string) => void;
  handleAbSelect: (paneLabel: "A" | "B", content: string) => void;
}

export function useAbCompareActions({
  chatId,
  updateChat,
  appendMessage,
  refetchMessages,
  push,
}: UseAbCompareActionsArgs): UseAbCompareActionsResult {
  // /compare slash command → enable A/B compare mode for this chat.
  // Patches ab_compare_enabled + model_a + model_b then invalidates so
  // ABCompareView renders on the next query cycle.
  const handleABCompareStart = useCallback(
    (modelA: string, modelB: string): void => {
      if (chatId === null) return;
      void updateChat
        .mutateAsync({
          ab_compare_enabled: true,
          ab_compare_model_a: modelA,
          ab_compare_model_b: modelB,
        })
        .catch(() => {
          push({
            variant: "error",
            message: "Couldn't start comparison — try again.",
          });
        });
    },
    [chatId, updateChat, push],
  );

  // A/B compare → commit pane response into chat history.
  // Persists the chosen pane's assistant text via POST /api/chats/:id/messages,
  // then toggles A/B compare off so the chat returns to single-stream mode.
  // The user can re-enable A/B from settings at any time.
  const handleAbSelect = useCallback(
    (paneLabel: "A" | "B", content: string): void => {
      if (chatId === null) return;
      if (content.trim() === "") {
        push({
          variant: "warning",
          message: `Model ${paneLabel} response is empty — nothing to save.`,
        });
        return;
      }
      appendMessage.mutate(
        { role: "assistant", content },
        {
          onSuccess: () => {
            push({
              variant: "success",
              message: `Model ${paneLabel} response saved to chat (${String(content.length)} chars)`,
            });
            // Refetch + flip A/B compare off so the UI returns to the single
            // message list with the new assistant turn visible.
            void refetchMessages();
            void updateChat
              .mutateAsync({ ab_compare_enabled: false })
              .catch(() => {
                // Non-fatal: the message is already saved.  If the toggle PATCH
                // fails the user can disable A/B manually from settings.
              });
          },
          onError: () => {
            push({
              variant: "error",
              message: `Couldn't save Model ${paneLabel} response — try again.`,
            });
          },
        },
      );
    },
    [chatId, appendMessage, refetchMessages, updateChat, push],
  );

  return { handleABCompareStart, handleAbSelect };
}
