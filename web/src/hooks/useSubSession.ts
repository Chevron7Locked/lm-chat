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
  fetchSubSessionDetail,
  fetchSubSessions,
  formatSubSessionDate,
  injectSubSessionSummary,
  isSubSessionLive,
} from "@/lib/subSession";
import type { SubSessionDetailDto, SubSessionSummaryDto } from "@/lib/subSession";
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
  /**
   * `sub_sessions.id` this panel corresponds to server-side, or `null`
   * when unknown. A freshly-started session never learns its id — the SSE
   * stream doesn't echo it back — but a session restored on chat load (P3)
   * or reopened from history (P4, `reopenSubSession`) populates this from
   * `GET .../sub-sessions/{id}`. Once set, `maybeRouteSubmit` forwards it
   * as the `sub_session_id` continuation param so every subsequent turn
   * APPENDS onto the same durable row instead of starting a new one (P4
   * §2 — the backend's `_append_turn_to_sub_session` path). See
   * `closeSubSessionPanel` vs `cancelSubSession` for why closing a panel
   * must never abort an already-gracefully-finishing stream (that's the
   * "keep the record" guarantee — it's about not racing the server's own
   * teardown, not about retaining this field after the panel closes; the
   * record itself stays reachable via the history list regardless).
   */
  subSessionId: number | null;
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
  /** UI-only close — chat-switch and post-inject cleanup. Never aborts an
   *  in-flight stream; see the D10 comment on its implementation. */
  closeSubSessionPanel: () => void;
  /** Explicit user abort (the panel's Cancel button). Aborts any in-flight
   *  stream, which the backend salvages as `aborted`. */
  cancelSubSession: () => void;
  subSessionSSE: UseSubSessionSSE;
  /** P4: this chat's past sub-sessions (newest first), or `null` before
   *  the first `openHistory()` fetch lands. */
  subSessionHistory: SubSessionSummaryDto[] | null;
  /** True while an `openHistory()` fetch is in flight. */
  subSessionHistoryLoading: boolean;
  /** Whether the history browse view is currently showing. */
  isSubSessionHistoryOpen: boolean;
  /** Fetch this chat's sub-session list (fresh, every call) and show the
   *  history browse view. On-demand only — never fetched eagerly, so a
   *  chat with no sub-sessions never pays for it. */
  openSubSessionHistory: () => void;
  /** Hide the history browse view without touching the underlying panel
   *  (a live/reopened `subSession`, if any, is unaffected). */
  closeSubSessionHistory: () => void;
  /** Reopen a past sub-session (ANY status — final or aborted) by id:
   *  fetches its full transcript and shows it in the panel, replacing
   *  whatever was open. A subsequent turn appends onto the SAME row (see
   *  `SubSessionState.subSessionId`). */
  reopenSubSession: (subSessionId: number) => void;
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

/**
 * Convert a fetched `SubSessionDetail` transcript into a `SubSessionState`
 * the panel can render — same shape a live session builds up turn by turn.
 *
 * Returns `null` (skip the restore) when `preset_id` doesn't resolve to a
 * known preset — legacy rows written before this fix carry the
 * `_SUB_SESSION_PRESET_ID_UNSPECIFIED` placeholder, and a preset-less
 * session can't rebuild a `systemPrompt` for a possible follow-up
 * finalize. `systemPrompt` is reconstructed fresh (today's date, no
 * integrations) rather than round-tripped — the original isn't persisted,
 * and every live turn already rebuilds it the same way (see
 * `maybeRouteSubmit`), so this isn't a new source of drift.
 *
 * Drops any row that isn't `user`/`assistant` (the schema allows a future
 * `tool` role; nothing writes one today) and any row with no content AND
 * no reasoning_content (a draft salvaged before its first delta landed) —
 * otherwise the panel would show a blank assistant bubble.
 */
function buildRestoredSubSessionState(
  detail: SubSessionDetailDto,
): SubSessionState | null {
  const preset = getPreset(detail.preset_id);
  if (preset === null) return null;

  const messages: SubSessionMessage[] = [];
  for (const row of detail.messages) {
    if (row.role !== "user" && row.role !== "assistant") continue;
    if (row.content === "" && (row.reasoning_content ?? "") === "") continue;
    messages.push({ role: row.role, content: row.content, id: row.id });
  }

  return {
    presetId: preset.id,
    presetLabel: preset.label,
    presetTemplate: preset.system_prompt,
    systemPrompt: buildSubSessionSystemPrompt(
      preset.system_prompt,
      formatSubSessionDate(),
    ),
    messages,
    finalizing: false,
    finalContent: null,
    subSessionId: detail.id,
  };
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

  // P3 (durable sub-sessions, D10): cancel and close used to be the SAME
  // operation (`cancelSubSession`, below) fired from three call sites — the
  // cross-chat wipe effect, `handleSubSessionInject`'s post-promote
  // cleanup, and the panel's explicit Cancel button. That conflated two
  // different intents: at the server, a dropped connection and a real user
  // abort are indistinguishable (both salvage the draft as `aborted`), so
  // reusing `cancelSubSession`'s `subSessionSSE.reset()` (which aborts the
  // in-flight fetch's AbortController) at the chat-switch/post-inject sites
  // risked racing an already-gracefully-finishing stream into `aborted`
  // instead of `final`. Split:
  //   - `closeSubSessionPanel` — UI-only. Clears the local panel so it
  //     stops rendering, but does NOT touch `subSessionSSE` — a stream
  //     still in flight keeps running server-side to its own natural
  //     conclusion (final or aborted, decided by the backend's own
  //     disconnect/error/graceful-completion logic, same as a main-chat
  //     stream surviving a chat switch). Used by the chat-switch wipe and
  //     `handleSubSessionInject`'s post-promote cleanup.
  //   - `cancelSubSession` — a real user abort (the panel's Cancel
  //     button). Aborts the underlying fetch too, which the backend's
  //     disconnect watcher observes and salvages as `aborted`.
  const closeSubSessionPanel = useCallback((): void => {
    setSubSession(null);
  }, []);

  const cancelSubSession = useCallback((): void => {
    setSubSession(null);
    subSessionSSE.reset();
  }, [subSessionSSE]);

  // P4: per-chat sub-session HISTORY (list past sessions) + REOPEN (load a
  // past session's full transcript, any status, back into the panel).
  // `historyRunId` guards against a stale fetch from a prior openHistory()
  // call landing after the chat has switched — same shape as
  // `restoreRunIdRef` below.
  const [subSessionHistory, setSubSessionHistory] = useState<
    SubSessionSummaryDto[] | null
  >(null);
  const [subSessionHistoryLoading, setSubSessionHistoryLoading] =
    useState(false);
  const [isSubSessionHistoryOpen, setIsSubSessionHistoryOpen] =
    useState(false);
  const historyRunIdRef = useRef(0);

  const openSubSessionHistory = useCallback((): void => {
    if (chatId === null) return;
    setIsSubSessionHistoryOpen(true);
    setSubSessionHistoryLoading(true);
    const runId = historyRunIdRef.current + 1;
    historyRunIdRef.current = runId;
    const cid = chatId;
    void (async () => {
      const list = await fetchSubSessions(cid);
      if (historyRunIdRef.current !== runId) return; // chat switched again
      setSubSessionHistory(list ?? []);
      setSubSessionHistoryLoading(false);
    })();
  }, [chatId]);

  const closeSubSessionHistory = useCallback((): void => {
    setIsSubSessionHistoryOpen(false);
  }, []);

  const reopenSubSession = useCallback(
    (sid: number): void => {
      if (chatId === null) return;
      const cid = chatId;
      void (async () => {
        const detail = await fetchSubSessionDetail(cid, sid);
        if (detail === null) {
          push({
            variant: "error",
            message: "Couldn't load that sub-session — try again.",
          });
          return;
        }
        const restored = buildRestoredSubSessionState(detail);
        if (restored === null) {
          push({
            variant: "error",
            message: "That sub-session's mode is no longer available.",
          });
          return;
        }
        setSubSession(restored);
        setIsSubSessionHistoryOpen(false);
      })();
    },
    [chatId, push],
  );

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
  // state belongs to the chat the user was just in. Wipe (UI-only —
  // `closeSubSessionPanel`, not `cancelSubSession`; D10 above) on chatId
  // change. The panel itself doesn't restore across THIS wipe — the
  // restore-on-load effect below is a separate fetch keyed on the NEW
  // chatId, not a carry-over of the wiped state.
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
        closeSubSessionPanel();
      }
      // P4: the history browse view is per-chat scratch state too — close
      // it on chat-switch for the same reason (never carries a stale
      // chat's list into the new chat).
      if (isSubSessionHistoryOpen) {
        closeSubSessionHistory();
      }
    }
  }, [
    chatId,
    closeSubSessionPanel,
    subSession,
    isSubSessionHistoryOpen,
    closeSubSessionHistory,
  ]);

  // P3 restore-on-load: durable sub-sessions (migration 0045 + P2's
  // persist-through-the-draft-state-machine) survive a reload at the DB
  // layer already — this is what makes that visible. On every chatId
  // change (including the initial mount), fetch the chat's sub-sessions
  // newest-first and, ONLY if the newest one is genuinely still live,
  // restore its transcript into the panel.
  //
  // "Genuinely still live" (D9) is keyed off the newest
  // `sub_session_messages.state` being exactly `draft` — a
  // `pending_finalization` row is a completed turn awaiting the reaper's
  // final commit and is deliberately NOT auto-restored (mirrors
  // `isSubSessionLive` / `LIVE_MESSAGE_STATES` in lib/subSession.ts). It is
  // NOT keyed off `sub_sessions.status` alone, which can briefly still read
  // `active` after a graceful finish (the outer teardown's `final`
  // transition is a separate write that can lag the terminal SSE frame).
  // A finished/aborted session is deliberately NOT auto-restored here —
  // browsing/reopening past sessions is P4's history-list feature.
  //
  // `restoreRunIdRef` guards against a stale fetch from an earlier
  // chatId landing after the user has already switched again — the same
  // shape as `runGuardRef` in useSubSessionSSE, at the coarser
  // per-effect-run granularity this hook needs.
  const restoreRunIdRef = useRef(0);
  useEffect(() => {
    const runId = restoreRunIdRef.current + 1;
    restoreRunIdRef.current = runId;
    if (chatId === null) return;
    const cid = chatId;
    void (async () => {
      const list = await fetchSubSessions(cid);
      if (restoreRunIdRef.current !== runId) return; // chat switched again
      if (list === null) return;
      const [newest] = list;
      if (newest === undefined) return;
      const detail: SubSessionDetailDto | null = await fetchSubSessionDetail(
        cid,
        newest.id,
      );
      if (restoreRunIdRef.current !== runId) return; // chat switched again
      if (detail === null || !isSubSessionLive(detail)) return;
      const restored = buildRestoredSubSessionState(detail);
      if (restored === null) return;
      // Functional guard: if the user already opened their OWN sub-session
      // (e.g. typed `/research ...` while this fetch was in flight), don't
      // clobber it with the restored one. Reads the LATEST state at apply
      // time rather than whatever was closed over when the effect started.
      setSubSession((prev) => prev ?? restored);
    })();
  }, [chatId]);

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
        subSessionId: null,
      };
      setSubSession(next);
      // A fresh session takes over the panel — any open history browse
      // view would otherwise linger over it.
      setIsSubSessionHistoryOpen(false);
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
      presetId: subSession.presetId,
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

  // Failure-mode robustness: if the finalize (summary) stream ERRORS, clear
  // `finalizing` and surface the error — otherwise the panel hangs on
  // "Generating summary…" forever (dogfood-found: a sub-session/finalize 409 or
  // any transport error left it stuck; the onComplete path clears the flag but
  // there was no error path). The BE no longer 409s finalize against a
  // just-finished (pending_finalization) turn, but a finalize can still fail for
  // real reasons — a genuine concurrent stream, a network drop — and that must
  // never hang the UI. Fires once: clearing `finalizing` gates the re-fire.
  useEffect(() => {
    if (subSessionSSE.state.status === "error" && subSession?.finalizing) {
      setSubSession((prev) => (prev ? { ...prev, finalizing: false } : null));
      push({
        variant: "error",
        message:
          subSessionSSE.state.error?.message ??
          "Couldn't generate the summary — try again.",
      });
    }
  }, [
    subSessionSSE.state.status,
    subSessionSSE.state.error,
    subSession?.finalizing,
    push,
  ]);

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
      // D10: close, don't cancel — the finalize stream already finished
      // gracefully (finalContent came from its onComplete), so there's
      // nothing to abort; using cancelSubSession here would risk racing
      // an already-final row into aborted for no reason.
      closeSubSessionPanel();
      void refetchMessages();
    })();
  }, [
    subSession,
    chatId,
    selectedModel,
    currentChat?.model_id,
    savedDefaultModel,
    closeSubSessionPanel,
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
          presetId: activeSubSession.presetId,
          systemPrompt: freshSystemPrompt,
          messages: updated,
          ...(turnIntegrations.length > 0
            ? { integrations: turnIntegrations }
            : {}),
          // P4 continue: once a session is restored (P3) or reopened
          // (P4's reopenSubSession) its subSessionId is non-null — forward
          // it so the BE APPENDS this turn onto the SAME durable row
          // instead of starting a new one. A freshly-started session's id
          // stays null for its whole local lifetime (the create-new
          // response doesn't echo it back), so its first turns still each
          // create their own row — unchanged, pre-existing behavior.
          ...(activeSubSession.subSessionId !== null
            ? { subSessionId: activeSubSession.subSessionId }
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
    closeSubSessionPanel,
    cancelSubSession,
    subSessionSSE,
    maybeRouteSubmit,
    subSessionHistory,
    subSessionHistoryLoading,
    isSubSessionHistoryOpen,
    openSubSessionHistory,
    closeSubSessionHistory,
    reopenSubSession,
  };
}
