/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useSubSession — slash-command sub-agent sessions.
 *
 * Extracted from `Chat.tsx` (behavior-preserving). Sub-session mode is
 * activated by slash commands
 * (/research, /code, etc.). When active, messages go to the clean-context
 * sub-session endpoint instead of the main stream. The main chat history
 * is preserved but hidden.
 *
 * `startSubSession` returns the
 * freshly-created `SubSessionState` synchronously, and `maybeRouteSubmit`
 * takes it as an explicit optional argument. This replaces a prior
 * ref-mirror bandaid (`subSessionRef`, kept in sync via an effect, read
 * ref-first in `maybeRouteSubmit`) that defended against a stale-closure
 * race: the Composer's inline-form `/research <query>` submit used to fire
 * `onSubmit` via `setTimeout(0)` on the same microtask `startSubSession`
 * ran on, which could beat React's commit of `setSubSession` — without the
 * ref, `handleSubmit` would read a stale closure where `subSession` was
 * still null and fall through to the regular chat path. Passing the
 * just-created session explicitly removes the race at its source: the
 * caller no longer needs to wait for a re-render (or a ref) to see it.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useSubSessionSSE } from "@/hooks/useSubSessionSSE";
import type {
  SubSessionMessage,
  UseSubSessionSSE,
} from "@/hooks/useSubSessionSSE";
import { useSubSessionStore } from "@/stores/subSessionStore";
import { usePresetModels } from "@/hooks/usePresetModels";
import { getPreset } from "@/lib/presets";
import {
  buildSubSessionSystemPrompt,
  formatSubSessionDate,
  injectSubSessionSummary,
} from "@/lib/subSession";
import { resolveChatIntegrationsField } from "@/components/Composer";
import type { ChatStreamPayload } from "@/hooks/useSSE";
import type { PushOptions } from "@/stores/toastStore";
import type { ChatSummary } from "@/hooks/useChats";

// Sub-session mode — activated by slash commands (/research, /code, etc.).
// When active, messages go to the clean-context sub-session endpoint instead
// of the main stream. The main chat history is preserved but hidden.
export interface SubSessionState {
  presetId: string;
  presetLabel: string;
  /** Raw preset.system_prompt template; rebuilt with current integrations
   *  at every handleSubmit. Stored separately so the rendered systemPrompt
   *  isn't stale when MCP chips are toggled mid-session. */
  presetTemplate: string;
  systemPrompt: string;
  messages: SubSessionMessage[];
  finalizing: boolean;
  finalContent: string | null;
}

export interface UseSubSessionArgs {
  chatId: number | null;
  currentChat: ChatSummary | undefined;
  selectedModel: string | undefined;
  savedDefaultModel: string | undefined;
  /** Single model-resolution ladder for turn-dispatch paths (per-chat
   *  override → chat's persisted model → global saved default → ""); owned
   *  by Chat.tsx since other handlers outside this cluster use it too. */
  resolveTurnModel: () => string;
  push: (opts: PushOptions) => string;
  refetchMessages: () => Promise<unknown>;
}

export interface UseSubSessionResult {
  subSession: SubSessionState | null;
  /** Returns the freshly-created SubSessionState (or null if the presetId
   *  is unknown or there's no active chat) so callers that can't wait for
   *  a re-render — the Composer inline-form path — can pass it straight
   *  into `maybeRouteSubmit` instead. */
  startSubSession: (presetId: string) => SubSessionState | null;
  handleSubSessionFinalize: () => void;
  handleSubSessionInject: () => void;
  cancelSubSession: () => void;
  subSessionSSE: UseSubSessionSSE;
  /** Routes a Composer submit into the sub-session stream when a
   *  sub-session is active. Returns true if it handled the submit (caller
   *  should return early), false if the caller should fall through to the
   *  regular chat/AB/stream dispatch.
   *
   *  `explicitSubSession`, when passed (even as null), is used AS-IS in
   *  place of the `subSession` state — the Composer inline-form path
   *  passes `startSubSession`'s just-created return value here so the
   *  route decision doesn't depend on a re-render having landed yet.
   *  Omit it (leave undefined) to read the current `subSession` state,
   *  which is what every other call site does. */
  maybeRouteSubmit: (
    cid: number,
    payload: ChatStreamPayload,
    userText: string,
    explicitSubSession?: SubSessionState | null,
  ) => boolean;
}

export function useSubSession(args: UseSubSessionArgs): UseSubSessionResult {
  const {
    chatId,
    currentChat,
    selectedModel,
    savedDefaultModel,
    resolveTurnModel,
    push,
    refetchMessages,
  } = args;

  const [subSession, setSubSession] = useState<SubSessionState | null>(null);
  const subSessionSSE = useSubSessionSSE();

  // The reset-sub-session
  // sequence (clear ref, clear state, reset SSE) was previously duplicated
  // verbatim at three call sites in Chat.tsx (the cross-chat wipe effect,
  // handleSubSessionInject, and the SubSessionPanel onCancel prop).
  // Consolidated here into one callback — same three statements, same
  // order, at all three sites — and returned so Chat.tsx's render wiring
  // can pass it straight through as onCancel.
  const cancelSubSession = useCallback((): void => {
    setSubSession(null);
    subSessionSSE.reset();
  }, [subSessionSSE]);

  // Preset-model mapping — used to route sub-sessions to the configured
  // model+provider for each preset instead of the top-bar model.
  const { data: presetModels } = usePresetModels();

  // Sub-sessions cannot be projected. Push the active chat id into the
  // global ``subSessionStore`` whenever a sub-session is open so the
  // Sidebar's move-to-project affordance can hide on this chat's row
  // (the visible UI surface is the ephemeral session; the move target
  // would be the persistent main chat, which is intentionally
  // not what they're interacting with).
  const setSubSessionActive = useSubSessionStore((s) => s.setActive);
  const clearSubSessionActive = useSubSessionStore((s) => s.clear);
  useEffect(() => {
    if (subSession !== null && chatId !== null) {
      setSubSessionActive(chatId);
    } else {
      clearSubSessionActive();
    }
    return () => {
      clearSubSessionActive();
    };
  }, [subSession, chatId, setSubSessionActive, clearSubSessionActive]);

  // un-keyed subSession state leaked across chat-switch — opening a fresh
  // chat 19 from chat 18 showed chat 18's "RESEARCH" panel. This piece of
  // state belongs to the chat the user was just in. Wipe on chatId change.
  // Intentionally ephemeral (no restore-on-revisit).
  //
  // This used to be one half
  // of a combined effect in Chat.tsx that also wiped followupSuggestions
  // off a single shared prevChatIdRef. Split so each concern owns its own
  // prevChatIdRef copy — both effects still fire on every chatId change,
  // so behavior is unchanged; see Chat.tsx for the followups half.
  const prevChatIdRef = useRef<number | null>(chatId);
  useEffect(() => {
    if (prevChatIdRef.current !== chatId) {
      prevChatIdRef.current = chatId;
      if (subSession !== null) {
        cancelSubSession();
      }
    }
  }, [chatId, cancelSubSession, subSession]);

  // Start a sub-session for a given preset. Returns the created state
  // synchronously (in addition to scheduling the setSubSession commit) so
  // the Composer inline-form path can pass it straight into
  // maybeRouteSubmit without waiting for a re-render.
  const startSubSession = useCallback(
    (presetId: string): SubSessionState | null => {
      const preset = getPreset(presetId);
      if (preset === null || chatId === null) return null;
      const next: SubSessionState = {
        presetId,
        presetLabel: preset.label,
        presetTemplate: preset.system_prompt,
        systemPrompt: buildSubSessionSystemPrompt(
          preset.system_prompt,
          formatSubSessionDate(),
        ),
        messages: [],
        finalizing: false,
        finalContent: null,
      };
      setSubSession(next);
      return next;
    },
    [chatId],
  );

  // Finalize: stream a summary from the sub-session.
  const handleSubSessionFinalize = useCallback((): void => {
    if (subSession === null || chatId === null) return;
    const resolvedModel = resolveTurnModel();
    if (!resolvedModel) {
      push({
        variant: "warning",
        message: "Select a model before finalizing.",
      });
      return;
    }
    setSubSession((prev) => (prev ? { ...prev, finalizing: true } : null));
    const finalizeIntegrations = resolveChatIntegrationsField(chatId);
    subSessionSSE.finalize({
      chatId,
      modelId: resolvedModel,
      systemPrompt: subSession.systemPrompt,
      messages: subSession.messages,
      ...(finalizeIntegrations !== undefined && { integrations: finalizeIntegrations }),
      onComplete: (fc) => {
        setSubSession((prev) =>
          prev ? { ...prev, finalizing: false, finalContent: fc } : null,
        );
      },
    });
  }, [subSession, chatId, resolveTurnModel, subSessionSSE, push]);

  // Inject the finalized summary into the main chat and exit sub-session.
  const handleSubSessionInject = useCallback((): void => {
    const content = subSession?.finalContent;
    if (!content || chatId === null) return;
    void (async () => {
      const resolvedModel =
        selectedModel ?? currentChat?.model_id ?? savedDefaultModel ?? null;
      const { ok } = await injectSubSessionSummary(
        chatId,
        content,
        resolvedModel,
      );
      if (!ok) {
        push({
          variant: "error",
          message: "Couldn't send summary to main chat — try again.",
        });
        return; // keep sub-session open so user can retry
      }
      cancelSubSession();
      void refetchMessages();
    })();
  }, [
    subSession,
    chatId,
    selectedModel,
    currentChat?.model_id,
    savedDefaultModel,
    cancelSubSession,
    refetchMessages,
    push,
  ]);

  // Sub-session submit routing, absorbed from handleSubmit's sub-session
  // branch. Returns true if it handled the submit (caller returns early).
  const maybeRouteSubmit = useCallback(
    (
      cid: number,
      payload: ChatStreamPayload,
      userText: string,
      explicitSubSession?: SubSessionState | null,
    ): boolean => {
      // Sub-session: route to clean-context endpoint, bypass main stream.
      // Use the explicit override when the caller passed one (even null —
      // that's a deliberate "no session" from a startSubSession that just
      // failed) — that's how the Composer inline-form path routes a
      // just-started session correctly without waiting for the
      // setSubSession commit to land. Every other call site omits the
      // argument and falls back to the current subSession state.
      const activeSubSession =
        explicitSubSession !== undefined ? explicitSubSession : subSession;
      if (activeSubSession !== null) {
        const userMsg: SubSessionMessage = { role: "user", content: userText };
        const updated = [...activeSubSession.messages, userMsg];
        setSubSession((prev) => (prev ? { ...prev, messages: updated } : null));
        // Resolve model+provider for this preset.
        // If a preset-model mapping is configured for this presetId, use it.
        // Otherwise fall back to the top-bar selectedModel with the chat's
        // current provider (so cloud main-chats still route correctly).
        const presetEntry = presetModels?.[activeSubSession.presetId];
        const resolvedModel =
          presetEntry?.model_id ??
          selectedModel ??
          currentChat?.model_id ??
          savedDefaultModel ??
          "";
        const resolvedProvider =
          presetEntry?.provider ??
          currentChat?.settings?.provider ??
          "lmstudio";
        // Rebuild the system prompt with the CURRENT integrations so the
        // tool-availability block reflects whatever MCP chips are on right
        // now — not the empty list baked at sub-session open. Without this, the
        // model reads "No live tools are wired into this sub-session" even
        // when integrations are forwarded.
        const turnIntegrations = payload.integrations ?? [];
        const freshSystemPrompt = buildSubSessionSystemPrompt(
          activeSubSession.presetTemplate,
          formatSubSessionDate(),
          turnIntegrations,
        );
        subSessionSSE.stream({
          chatId: cid,
          modelId: resolvedModel,
          provider: resolvedProvider,
          systemPrompt: freshSystemPrompt,
          messages: updated,
          ...(turnIntegrations.length > 0
            ? { integrations: turnIntegrations }
            : {}),
          onComplete: (fc) => {
            setSubSession((prev) =>
              prev
                ? {
                    ...prev,
                    messages: [...updated, { role: "assistant", content: fc }],
                  }
                : null,
            );
          },
        });
        return true;
      }
      return false;
    },
    [
      subSession,
      subSessionSSE,
      presetModels,
      selectedModel,
      currentChat,
      savedDefaultModel,
    ],
  );

  return {
    subSession,
    startSubSession,
    handleSubSessionFinalize,
    handleSubSessionInject,
    cancelSubSession,
    subSessionSSE,
    maybeRouteSubmit,
  };
}
