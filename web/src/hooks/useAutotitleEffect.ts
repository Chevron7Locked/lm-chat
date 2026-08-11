/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useAutotitleEffect — auto-generate a chat title after the first assistant
 * turn completes (2026-06-07).
 *
 * Extracted from pages/Chat.tsx so the swallow + no-retry contract can be
 * unit-tested independently of Chat's full mount surface.
 *
 * Contract (pins AC25-AC26):
 *   - Fires once per chatId per session (titleAttemptedRef guards retries).
 *   - Rejection from the mutation is swallowed — never bubbles to an
 *     unhandledrejection event or React error boundary.
 *   - After a failed attempt the chatId remains in titleAttemptedRef so
 *     subsequent SSE-complete transitions do NOT retry in-session.
 */
import { useEffect } from "react";
import type { RefObject } from "react";
import type { UseMutationResult } from "@tanstack/react-query";
import type { GenerateTitleResult } from "./useChats";
import type { ApiError } from "@/lib/api";

/** Minimal SSE status shape required by this hook.
 * Mirrors useSSE's full StreamState["status"] union (incl. "stopped") so
 * Chat.tsx can pass its sseState straight through; the hook only acts on
 * "complete". */
export interface AutotitleSSEState {
  status: "idle" | "streaming" | "complete" | "error" | "stopped";
}

/** Minimal chat shape required by this hook. */
export interface AutotitleChat {
  title: string;
}

/** Minimal messages-data shape required by this hook.
 *
 * ``content`` is read so the hook can skip autotitle on empty-reply
 * turns (e.g. an XML-tool-call dialect that escaped recovery and left
 * the assistant message with only reasoning). Optional + nullable so
 * call sites can pass a narrower MessageRecord and so we don't break
 * existing test shims that omitted content. */
export interface AutotitleMessagesData {
  messages: { role: string; content?: string | null }[];
}

/** Minimal mutation shape required by this hook. */
export type AutotitleMutation = Pick<
  UseMutationResult<GenerateTitleResult, ApiError, number>,
  "mutateAsync"
>;

/** Store callbacks required by this hook. */
export interface AutotitleStoreCallbacks {
  beginGenerating: (chatId: number) => void;
  endGenerating: (chatId: number) => void;
}

export interface UseAutotitleEffectArgs {
  chatId: number | null;
  sseState: AutotitleSSEState;
  currentChat: AutotitleChat | undefined;
  messagesData: AutotitleMessagesData | undefined;
  mutation: AutotitleMutation;
  store: AutotitleStoreCallbacks;
  /** Ref tracking which chatIds have already had title generation attempted
   * this session.  Owned by the caller so it survives re-mounts of this hook. */
  titleAttemptedRef: RefObject<Set<number>>;
}

const AUTO_TITLE_DEFAULT_VALUES = new Set(["", "New Chat", "Incognito Chat"]);

/**
 * Runs the auto-title side-effect in response to SSE-complete transitions.
 *
 * No return value — pure side-effect hook.
 */
export function useAutotitleEffect({
  chatId,
  sseState,
  currentChat,
  messagesData,
  mutation,
  store,
  titleAttemptedRef,
}: UseAutotitleEffectArgs): void {
  useEffect(() => {
    if (sseState.status !== "complete") return;
    if (chatId === null) return;
    const msgs = messagesData?.messages ?? [];
    const assistantCount = msgs.filter((m) => m.role === "assistant").length;
    if (assistantCount < 1) return;
    // 2026-06-11: a turn that ended with ONLY reasoning content (model
    // failed to emit a final reply — e.g. the polaris-9b
    // XML tool-call dialect that the BE recovery didn't catch) titled
    // the chat with the reasoning prefix, e.g.
    // "_(reasoning surfaced because the model produced no final answer)_
    // The user is asking about…". Skip autotitle when the most recent
    // assistant message has empty/whitespace-only content — titles
    // should reflect the reply, not the model's thinking-out-loud.
    const latestAssistant = [...msgs]
      .reverse()
      .find((m) => m.role === "assistant");
    if (latestAssistant && (latestAssistant.content ?? "").trim() === "") {
      return;
    }
    const currentTitle = currentChat?.title ?? "";
    const isDefault = AUTO_TITLE_DEFAULT_VALUES.has(currentTitle);
    if (!isDefault) return;
    if (titleAttemptedRef.current.has(chatId)) return;
    titleAttemptedRef.current.add(chatId);

    const targetChatId = chatId;
    store.beginGenerating(targetChatId);
    mutation
      .mutateAsync(targetChatId)
      .catch(() => {
        // Best-effort: swallow upstream/network errors silently so the
        // user is never bothered by a failed background nicety.
      })
      .finally(() => {
        store.endGenerating(targetChatId);
      });
  }, [
    sseState.status,
    chatId,
    messagesData,
    currentChat,
    mutation,
    store,
    titleAttemptedRef,
  ]);
}
