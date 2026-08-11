/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useMessageActions — message-level regenerate/resend/edit/retry handlers.
 *
 * Extracted from pages/Chat.tsx — behavior-preserving; every callback body
 * is verbatim from Chat.tsx, only wrapped in this hook. `regenConfirm` /
 * `setRegenConfirm` now live here (mirroring `confirmClear` in
 * useChatCommands) since the only consumers are these handlers and the
 * RegenConfirmDialog JSX in Chat.tsx, which reads the returned pair
 * directly — same shape as before.
 *
 * handleRegenerateClick is the shared primitive: handleResendClick and
 * handleEditUserMessage both delegate to it (clearing the response-id
 * chain first), and handleRegenerateConfirm is the modal's "confirm"
 * follow-up when the initial call comes back 412.
 * handleRetryInterruptedStream is a separate turn-retry primitive that was
 * co-located with this cluster in Chat.tsx and moves with it.
 */
import { useCallback, useState } from "react";
import type { Dispatch, RefObject, SetStateAction } from "react";
import type {
  RegenerateConfirmDetail,
  useEditMessage,
  useRegenerateMessage,
} from "@/hooks/useChats";
import type { ChatStreamPayload } from "@/hooks/useSSE";
import type { PushOptions } from "@/stores/toastStore";
import { resolveChatIntegrationsField } from "@/components/Composer";
import {
  clearOrphanedSSEKeys,
  loadOrphanedResponseId,
} from "@/components/InterruptedRow";
import { clearResponseId } from "@/lib/responseId";

export interface UseMessageActionsArgs {
  chatId: number | null;
  regenerateMessage: ReturnType<typeof useRegenerateMessage>;
  editMessage: ReturnType<typeof useEditMessage>;
  push: (opts: PushOptions) => string;
  submitTurn: (
    turnChatId: number,
    inputText: string,
    opts?: { integrations?: string[] },
  ) => void;
  resolveTurnModel: () => string;
  startStream: (chatId: number, payload: ChatStreamPayload) => Promise<void>;
  setHasOrphanedStream: Dispatch<SetStateAction<boolean>>;
  // Chain-invalidation flag: set to true by handleEditUserMessage so that
  // the stream-complete effect (still in Chat.tsx) skips writing a stale
  // response_id if the user edited between stream-end and that effect
  // running. Shared with Chat.tsx's own handleSubmit / stream-complete
  // effect, so the ref object itself must stay owned by Chat.tsx.
  chainInvalidatedRef: RefObject<boolean>;
  // Auto-stick-to-bottom ref, shared broadly across Chat.tsx (scroll
  // effect, handleSubmit, submitTurn, etc.) — stays owned by Chat.tsx.
  autoStickRef: RefObject<boolean>;
}

export interface UseMessageActionsResult {
  regenConfirm: RegenerateConfirmDetail | null;
  setRegenConfirm: Dispatch<SetStateAction<RegenerateConfirmDetail | null>>;
  handleRegenerateClick: (messageId: number) => void;
  handleResendClick: (messageId: number) => void;
  handleEditUserMessage: (
    messageId: number,
    newContent: string,
  ) => Promise<void>;
  handleRegenerateConfirm: () => Promise<void>;
  handleRetryInterruptedStream: () => void;
}

export function useMessageActions({
  chatId,
  regenerateMessage,
  editMessage,
  push,
  submitTurn,
  resolveTurnModel,
  startStream,
  setHasOrphanedStream,
  chainInvalidatedRef,
  autoStickRef,
}: UseMessageActionsArgs): UseMessageActionsResult {
  // Regenerate confirm modal state.
  const [regenConfirm, setRegenConfirm] =
    useState<RegenerateConfirmDetail | null>(null);

  // Regenerate flow — call without confirm → if 412, show modal;
  // on user confirmation, call with confirm=true → kick streaming.
  const handleRegenerateClick = useCallback(
    (messageId: number): void => {
      if (chatId === null) return;
      const currentChatId = chatId;
      regenerateMessage.mutate(
        { messageId, confirm: false },
        {
          onSuccess: (result) => {
            // Auto-confirm path: last user message — backend returns 200
            // with the user content; no 412 modal needed. A confirm=false
            // call only reaches onSuccess (instead of the 412 in onError)
            // when the backend auto-confirmed, so `deleted` is no longer
            // the signal here — the boundary user message is now deleted
            // and replayed too, so `deleted` is >= 1 even in this branch.
            if (
              result.prior_user_content !== null &&
              result.prior_user_content !== ""
            ) {
              submitTurn(currentChatId, result.prior_user_content);
            } else {
              // Silent-failure guard: the regenerate/resend endpoint
              // auto-confirmed (HTTP 200) but returned no prior prompt to
              // replay (e.g. a malformed chat with no preceding user message)
              // — surface it instead of a click that does nothing.
              push({
                variant: "error",
                message: "Nothing to regenerate from — no prior prompt.",
              });
            }
          },
          onError: (err) => {
            if (err.status === 412) {
              const detail = err.detailObject;
              if (detail?.code === "confirm_required") {
                setRegenConfirm(detail as unknown as RegenerateConfirmDetail);
                return;
              }
            }
            push({ variant: "error", message: "Regenerate failed." });
          },
        },
      );
    },
    [chatId, regenerateMessage, push, submitTurn],
  );

  // Resend a USER message: clear any stale response_id (the chain
  // restarts from the new turn's rid) then delegate to handleRegenerateClick.
  // Do NOT set chainInvalidatedRef here — resend does not change content so
  // the new response_id from the replayed stream should be stored normally.
  // The backend now accepts user-role message ids on the regenerate endpoint
  // and returns the user message's own content as prior_user_content so the
  // onSuccess handler in handleRegenerateClick can resubmit it as a fresh turn.
  const handleResendClick = useCallback(
    (messageId: number): void => {
      if (chatId === null) return;
      clearResponseId(chatId);
      handleRegenerateClick(messageId);
    },
    [chatId, handleRegenerateClick],
  );

  // Edit-regenerate: handle an inline edit on a user message.
  //
  // The edit endpoint (PATCH /api/messages/{id}) only patches the message
  // content IN PLACE — it does NOT truncate the now-stale assistant reply. So
  // after persisting the edit we must regenerate the reply, and we must do it
  // through the regenerate delete-then-replay path (mirroring
  // handleRegenerateConfirm): streaming a fresh turn directly would leave the
  // old reply in place AND duplicate the user message. handleRegenerateClick
  // hits the regenerate endpoint, which deletes the just-edited user message +
  // everything after it and resubmits its (now-edited) content as a fresh turn
  // via submitTurn — so no duplicate user row is created. For the last user
  // message the backend auto-confirms; for an earlier one it surfaces the
  // regenerate confirm modal ("N following messages will be deleted").
  //
  // Two-pronged rid clear to handle the race between this callback and the
  // stream-complete useEffect (which may fire before or after the edit):
  //   1. Set chainInvalidatedRef so the useEffect skips storeResponseId if it
  //      hasn't fired yet.
  //   2. Call clearResponseId immediately so any already-stored rid is removed
  //      — the edited content starts a fresh chain.
  const handleEditUserMessage = useCallback(
    async (messageId: number, newContent: string): Promise<void> => {
      await editMessage.mutateAsync({ messageId, content: newContent });
      chainInvalidatedRef.current = true;
      if (chatId !== null) clearResponseId(chatId);
      // Regenerate FROM the just-edited user message (delete-then-replay).
      handleRegenerateClick(messageId);
    },
    [chatId, editMessage, handleRegenerateClick, chainInvalidatedRef],
  );

  const handleRegenerateConfirm = useCallback(async (): Promise<void> => {
    if (regenConfirm === null) return;
    const { message_id: messageId, chat_id: confirmedChatId } = regenConfirm;
    // Guard: user switched chats while the confirm modal was open.
    if (confirmedChatId !== chatId) {
      setRegenConfirm(null);
      push({ variant: "error", message: "Chat changed — please try again." });
      return;
    }
    const model = resolveTurnModel();
    if (model === "") {
      push({ variant: "error", message: "Pick a model before regenerating." });
      return;
    }
    try {
      // Backend deletes the whole turn (prior user prompt + the
      // assistant reply being regenerated + anything after) and hands
      // back the prompt text. We resubmit that text as a fresh turn —
      // LM Studio's /api/v1/chat rejects an empty input array, so we
      // can't just lean on previous_response_id (its anchor is gone
      // anyway). See the service method docstring for the full
      // rationale.
      const result = await regenerateMessage.mutateAsync({
        messageId,
        confirm: true,
      });
      setRegenConfirm(null);
      if (
        result.prior_user_content === null ||
        result.prior_user_content === ""
      ) {
        push({
          variant: "error",
          message: "Nothing to regenerate from — no prior prompt in this chat.",
        });
        return;
      }
      submitTurn(confirmedChatId, result.prior_user_content);
    } catch {
      push({ variant: "error", message: "Regenerate failed." });
    }
  }, [
    chatId,
    regenConfirm,
    regenerateMessage,
    submitTurn,
    push,
    // Model ladder (via resolveTurnModel) drives the pre-delete guard: fail
    // before the regenerate endpoint deletes the turn when no model resolves.
    resolveTurnModel,
  ]);

  const handleRetryInterruptedStream = useCallback((): void => {
    if (chatId === null) return;
    const prevRid = loadOrphanedResponseId(chatId);
    clearOrphanedSSEKeys(chatId);
    setHasOrphanedStream(false);
    if (prevRid !== null) {
      const model = resolveTurnModel();
      if (model === "") return;
      // Resume the interrupted stream with the stored response_id.
      const retryIntegrations = resolveChatIntegrationsField(chatId);
      const resumePayload: ChatStreamPayload = {
        input: [],
        model,
        previous_response_id: prevRid,
        ...(retryIntegrations !== undefined && { integrations: retryIntegrations }),
      };
      // Re-arms auto-stick so this turn snaps to the bottom.
      autoStickRef.current = true;
      void startStream(chatId, resumePayload);
    }
  }, [
    chatId,
    startStream,
    resolveTurnModel,
    setHasOrphanedStream,
    autoStickRef,
  ]);

  return {
    regenConfirm,
    setRegenConfirm,
    handleRegenerateClick,
    handleResendClick,
    handleEditUserMessage,
    handleRegenerateConfirm,
    handleRetryInterruptedStream,
  };
}
