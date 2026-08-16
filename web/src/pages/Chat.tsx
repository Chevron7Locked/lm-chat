/* SPDX-License-Identifier: Apache-2.0 */
import "@/styles/chat.css";
/**
 * Chat — the main three-column chat surface.
 *
 * Layout:
 *   Left:   Sidebar (collapsible, ChatList)
 *   Center: Message list + Composer
 *   Right:  Optional slide-in panel (Settings | Memory)
 *
 * Top bar: chat title, model selector, fork/pin/delete actions.
 *
 * Streaming:
 *   useSSE handles the fetch-based SSE connection. On message submission
 *   the in-flight assistant response is rendered as an optimistic message
 *   that updates in-place from contentDeltas.
 *
 *   Multi-tab: BroadcastChannel coordination is wired inside useSSE. If
 *   another tab starts a stream on this chat_id, the Composer shows a
 *   "streaming in another tab" notice.
 *
 * Response-ID reconciliation:
 *   After each stream, the responseId is stored locally (keyed by chat_id)
 *   and passed as previous_response_id on the next message.
 */
import {
  useEffect,
  useMemo,
  useRef,
  useCallback,
  useState,
} from "react";
import { useMobileDrawer } from "@/hooks/useMobileDrawer";
import { useAutotitleEffect } from "@/hooks/useAutotitleEffect";
import { useSSEWarningToasts } from "@/hooks/useSSEWarningToasts";
import { useStoppedStreamReconciliation } from "@/hooks/useStoppedStreamReconciliation";
import { useReasoningOverridePersistence } from "@/hooks/useReasoningOverridePersistence";
import { useMtpSuspectedDedupe } from "@/hooks/useMtpSuspectedDedupe";
import { useChatCommands } from "@/hooks/useChatCommands";
import { useMessageActions } from "@/hooks/useMessageActions";
import { useAbCompareActions } from "@/hooks/useAbCompareActions";
import { useParams, useNavigate, Navigate } from "react-router-dom";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { BRAND_NAME } from "@/components/BrandMark";
import { LmStudioAuthBanner } from "@/components/LmStudioAuthBanner";
import { useChatScopedState } from "@/hooks/useChatScopedState";
import { useQueryClient } from "@tanstack/react-query";
import { useAuthStore } from "@/stores/authStore";
import { useSSE } from "@/hooks/useSSE";
import type { ChatStreamPayload, StreamStats } from "@/hooks/useSSE";
import { useSubSession } from "@/hooks/useSubSession";
import type { SubSessionState } from "@/hooks/useSubSession";
import {
  useChatsDirect,
  useMessages,
  useUpdateChat,
  useDeleteChat,
  useClearChatMessages,
  useForkChat,
  useCompactChat,
  useAppendMessage,
  useCreateChat,
  useDeleteMessage,
  useEditMessage,
  useRegenerateMessage,
  useGenerateTitle,
  chatKeys,
} from "@/hooks/useChats";
import { useTitleGenerationStore } from "@/stores/titleGenerationStore";
import { usePinInsight } from "@/hooks/useMemory";
import { useEmbeddingStatus } from "@/hooks/useEmbeddingStatus";
import { useModelList } from "@/hooks/useModelList";
import { useLmStudioConfig } from "@/hooks/useLmStudioConfig";
import { useChatModelOptions } from "@/hooks/useChatModelOptions";
import { extractFollowups } from "@/lib/followups";
import { getPreset, DEFAULT_PRESET_ID, RAW_PRESET_ID } from "@/lib/presets";
import { deriveMessageList } from "@/lib/deriveMessageList";
import { useViewport } from "@/hooks/useViewport";
import { useKeyboardInset } from "@/hooks/useKeyboardInset";
import { useABStream } from "@/hooks/useABStream";
import { Composer, resolveChatIntegrationsField } from "@/components/Composer";
import { PromoteToProjectModal } from "@/components/PromoteToProjectModal";
import { PinNavStrip } from "@/components/PinNavStrip";
import { PinnedMessagesPanel } from "@/components/PinnedMessagesPanel";
import { useToast } from "@/stores/toastStore";
import { useCompactions } from "@/hooks/useCompactions";
import { useChatPreset, useHydrateChatPresets, useChatPresetStore } from "@/hooks/useChatPreset";
import { useKeyboardShortcuts } from "@/hooks/useKeyboardShortcuts";
import { useFocusMode } from "@/hooks/useFocusMode";
import { InterruptedRow } from "@/components/InterruptedRow";
import { SlashPalette } from "@/components/SlashPalette";
import { BUILTIN_COMMANDS } from "@/components/SlashMenu";
import type { SlashCommand } from "@/components/SlashMenu";
import { KeyboardHelp } from "@/components/KeyboardHelp";
import { usePresence } from "@/hooks/usePresence";
import { useMouseParallax } from "@/hooks/useMouseParallax";
import type { ComposerPresenceCbs } from "@/components/Composer";
import { storeResponseId, loadResponseId, clearResponseId } from "@/lib/responseId";
import type { PanelView } from "@/components/chat/shared";
import { AUTO_MODEL_VALUE } from "@/components/chat/shared";
import { TopBar } from "@/components/chat/TopBar";
import { MobileDock } from "@/components/chat/MobileDock";
import { MobileSidebarShell } from "@/components/chat/MobileSidebarShell";
import { FocusModeExit } from "@/components/chat/FocusModeExit";
import { RightPanel } from "@/components/chat/RightPanel";
import { ConfirmDialog } from "@/components/chat/ConfirmDialog";
import { EmptyState } from "@/components/chat/EmptyState";
import { SubSessionPanel } from "@/components/chat/SubSessionPanel";
import { ABComparePane } from "@/components/chat/ABComparePane";
import { RegenConfirmDialog } from "@/components/chat/RegenConfirmDialog";
import { MessageListBody } from "@/components/chat/MessageListBody";

// ─── Component ──────────────────────────────────────────────────────────────

export default function Chat() {
  const { chatId: chatIdParam } = useParams<{ chatId?: string }>();
  const navigate = useNavigate();
  const { user, isInitializing } = useAuthStore();

  // Guard: only accept numeric segments — "/chats/new" or any non-numeric
  // path would produce NaN and trigger 422s on every API call.
  // Defensive against direct nav to non-numeric /chats/<x> URLs; no internal
  // route generates these.
  const chatId =
    chatIdParam !== undefined && /^\d+$/.test(chatIdParam)
      ? parseInt(chatIdParam, 10)
      : null;

  const { isMobile } = useViewport();
  // iOS soft-keyboard inset — sets --keyboard-inset-fallback on <html>
  // so composer.css padding-bottom stays above the on-screen keyboard.
  useKeyboardInset();
  // panelView is declared here (ahead of useMobileDrawer) because the
  // hook's close-drawer-when-panel-opens effect reads it — Chat.tsx still
  // owns this state since it's used well beyond the drawer (RightPanel,
  // TopBar, keyboard shortcuts below).
  const [panelView, setPanelView] = useState<PanelView>(null);
  const {
    sidebarCollapsed,
    setSidebarCollapsed,
    mobileDrawerOpen,
    drawerClosing,
    endDrawerClose,
  } = useMobileDrawer({ isMobile, chatId, panelView });
  // confirmClear state removed — /clear feature not yet
  // implemented; the command now shows a toast directly.
  // Cmd/Ctrl+/ opens the SlashPalette popover.
  const [slashPaletteOpen, setSlashPaletteOpen] = useState(false);
  // `?` opens the global KeyboardHelp modal.
  const [keyboardHelpOpen, setKeyboardHelpOpen] = useState(false);
  // Pinned-messages panel anchored to the TopBar Pins button.
  const [pinnedPanelOpen, setPinnedPanelOpen] = useState(false);
  // "Turn this chat into a Project" — opened from the ⋯ overflow menu.
  const [promoteModalOpen, setPromoteModalOpen] = useState(false);
  // Cmd/Ctrl+Shift+E increments this counter; ChatHeaderMenu's
  // useEffect reacts to the change by opening its dropdown.
  const [exportMenuSignal, setExportMenuSignal] = useState(0);
  // Reversible full-screen focus mode — session-only. Hides the sidebar +
  // top chrome and floats the composer (driven by the `is-focus-mode` class
  // on the shell). `motionEnabled` gates the transition class so
  // reduced-motion users get an instant enter/exit.
  const { focusMode, setFocusMode, toggleFocusMode, motionEnabled, revealLocked } =
    useFocusMode();

  // Orphan/interrupted stream detection.
  // On mount (or when chatId changes), check if localStorage has an orphaned
  // msg_id key for this chat without a matching completed message in
  // messagesData. Cleared when the user retries or explicitly dismisses.
  const [hasOrphanedStream, setHasOrphanedStream] = useState(false);

  // Model override per chat — memory-tier chat-scoped state. Switching
  // chats re-hydrates to that chat's own
  // override (or undefined, which falls through to currentChat.model_id /
  // savedDefaultModel below), so chat A's dropdown pick no longer bleeds
  // into chat B — and since onModelChange only fires on an actual dropdown
  // change, navigation alone can never silently PATCH chat B's model_id.
  const [selectedModel, setSelectedModel] = useChatScopedState<
    string | undefined
  >(chatId, "selectedModel", "memory", undefined);

  // Saved LM Studio default — fallback used when neither selectedModel
  // (per-chat override) nor currentChat.model_id (persisted on the chat)
  // is set. Without this, the model selector + every send-payload code
  // path defaulted to "" on a brand-new chat — the Settings → Default
  // Model save persisted but had no effect on Chat. Empty string means
  // the user hasn't saved a default yet; treat as undefined.
  const { data: lmStudioConfig } = useLmStudioConfig();
  const savedDefaultModel =
    lmStudioConfig?.default_model !== undefined &&
    lmStudioConfig.default_model !== ""
      ? lmStudioConfig.default_model
      : undefined;

  const messagesEndRef = useRef<HTMLDivElement>(null);
  // Scroll container for the messages list. We auto-stick to the bottom ONLY
  // while the user is already near the bottom; the moment they scroll up to
  // read, `autoStickRef` flips false and the per-delta auto-scroll stops
  // yanking them back down. Scrolling back to the bottom re-enables it.
  const messagesAreaRef = useRef<HTMLDivElement>(null);
  const autoStickRef = useRef(true);
  // px from the bottom within which we consider the user "at the bottom" and
  // keep following the stream. Generous enough to survive a final delta's
  // height growth without unsticking.
  const STICK_THRESHOLD_PX = 120;
  const handleMessagesScroll = useCallback(() => {
    const el = messagesAreaRef.current;
    if (!el) return;
    const distanceFromBottom = el.scrollHeight - el.scrollTop - el.clientHeight;
    autoStickRef.current = distanceFromBottom <= STICK_THRESHOLD_PX;
  }, []);
  // Switching chats re-arms auto-stick so the freshly-loaded chat lands at the
  // bottom (latest message) even if the user was scrolled up in the prior one.
  useEffect(() => {
    autoStickRef.current = true;
  }, [chatId]);

  const { data: chatsData } = useChatsDirect();
  const { data: messagesData, refetch: refetchMessages } = useMessages(chatId);
  const { data: compactionsData } = useCompactions(chatId);
  const compactions = compactionsData ?? [];

  const updateChat = useUpdateChat(chatId ?? 0);
  const deleteChat = useDeleteChat();
  const clearChatMessages = useClearChatMessages();
  const forkChat = useForkChat(chatId ?? 0);
  const compactChat = useCompactChat(chatId ?? 0);
  const appendMessage = useAppendMessage(chatId ?? 0);
  // Edit user messages + regenerate assistant messages.
  const editMessage = useEditMessage(chatId ?? 0);
  const regenerateMessage = useRegenerateMessage(chatId ?? 0);
  const deleteMessage = useDeleteMessage(chatId ?? 0);
  // Auto-title generation after the 2nd assistant turn.
  const generateTitleMutation = useGenerateTitle();
  const beginGenerating = useTitleGenerationStore((s) => s.begin);
  const endGenerating = useTitleGenerationStore((s) => s.end);
  // Tracks per-chat "already attempted" so we don't refire on every stream
  // complete after the second turn.  Survives within the component instance;
  // a page reload resets it, which is fine: the backend is idempotent.
  const titleAttemptedRef = useRef<Set<number>>(new Set());
  // Regenerate confirm modal state now lives in useMessageActions (see the
  // handler cluster below) — returned as `regenConfirm`/`setRegenConfirm`.
  // Cmd/Ctrl+N creates a new chat and navigates to it.
  const createChat = useCreateChat();
  const pinInsight = usePinInsight();
  const qc = useQueryClient();
  const { push } = useToast();

  // Top-level model status for composer no-model hint.
  const { loadedModels: topLevelLoadedModels, status: topLevelLmStatus } =
    useModelList();
  // Derive resolved RAG-enabled state for the toggle/badge.
  // The server smart-defaults RAG to ON when an embedder is loaded and the
  // chat has no explicit rag_enabled setting. The FE must reflect this so
  // "what you see" matches "what the server does". We poll
  // useEmbeddingStatus (already shared with Settings; 25s cadence) so the
  // toggle state self-corrects whenever the embedder loads/unloads.
  const { data: embeddingStatusData } = useEmbeddingStatus();
  const noModelLoaded =
    topLevelLmStatus === "no_models" ||
    (topLevelLmStatus === "connected" && topLevelLoadedModels.length === 0);

  // Hydrate + persist per-chat reasoning overrides — see
  // useReasoningOverridePersistence. Reads useChatSettingsStore
  // internally.
  useReasoningOverridePersistence({ chatId, chatsData, updateChat });

  // Hydrate the per-chat active-preset store from backend on first load.
  // Reads each chat's settings.active_preset and seeds the Zustand override so
  // the Composer badge survives a page reload.
  useHydrateChatPresets(chatsData);

  // Derive the persona label for the current chat so assistant turns can show
  // which persona produced them. Reads the Zustand store directly (same source
  // the rail picker and Composer badge read from) — no extra network call.
  // Defaults to DEFAULT_PRESET_ID (General) when no override has been set.
  // Returns undefined for RAW_PRESET_ID ("none") and legacy "" so no chip
  // renders on raw-mode turns.
  const currentChatPresetId = useChatPresetStore((s) =>
    chatId !== null ? (s.overrides[chatId] ?? DEFAULT_PRESET_ID) : DEFAULT_PRESET_ID,
  );
  const currentPersonaLabel: string | undefined = (() => {
    // Raw mode / legacy empty = no system prompt → no persona chip.
    if (currentChatPresetId === RAW_PRESET_ID || currentChatPresetId === "") {
      return undefined;
    }
    const resolved = getPreset(currentChatPresetId);
    // Known preset → its label. Unknown id → undefined (no chip).
    return resolved !== null ? resolved.label : undefined;
  })();
  // C3 — was the chat's CURRENT preset applied by model adoption rather
  // than chosen by the user?
  const currentPersonaAdopted: boolean = useChatPresetStore((s) =>
    chatId !== null && s.sources[chatId] === "model",
  );
  // adoptModelPreset applies a `mode_adopt` SSE verdict — see the effect
  // below, right after the OOB followups effect it mirrors.
  const { adoptModelPreset } = useChatPreset(chatId);

  const { state: sseState, start: startStream, stop: stopStream, reset: resetStream } = useSSE(chatId);

  // Optimistic user message. Rendered the instant the user hits send so their
  // message appears immediately — instead of a bare streaming caret until the
  // post-completion refetch lands, which previously made the user message and
  // the reply pop in together ("blinking cursor, then both at once"). `baseline`
  // is the server message count at submit time; once a refetch grows the list
  // past it, the persisted message has arrived and we drop the optimistic copy.
  const [pendingUser, setPendingUser] = useState<{
    text: string;
    baseline: number;
  } | null>(null);

  // A/B compare stream — used when ab_compare.enabled is true on this chat.
  const {
    state: abState,
    start: startABStream,
    stop: stopABStream,
  } = useABStream();

  // Current chat info.
  const currentChat = chatsData?.find((c) => c.id === chatId);
  // While `currentChat` is loading, pass null so the title falls back
  // to the brand alone — avoids flashing "Chat 13" for one frame
  // before the real title resolves.
  useDocumentTitle(currentChat?.title ?? null);

  // Stale pinned-model auto-switch: a chat can be
  // pinned to a model whose catalog key no longer exists in LM Studio (the
  // operator renamed/replaced the model). The <select> can't render a value
  // that isn't one of its options, so it visually falls back to the first
  // loaded model while React state (and the send payload) still holds the
  // dead key — "sees model A, but errors on model B". Detect the gap once
  // per chat and auto-switch to a valid model + surface a non-blocking
  // toast so the chat just works again. Only acts on an EXPLICIT pin
  // (non-empty currentChat.model_id) that is absent from the catalog — an
  // empty model_id is an implicit default the backend already resolves and
  // must never be persisted here (see the documented implicit-default
  // persistence bug this must not reintroduce).
  const { options: chatModelOptions, isLoading: chatModelOptionsLoading } =
    useChatModelOptions();
  const staleModelSwitchedForChatIdRef = useRef<number | null>(null);
  useEffect(() => {
    if (chatModelOptionsLoading) return;
    if (chatModelOptions.length === 0) return;
    if (currentChat === undefined) return;
    if (selectedModel !== undefined) return;
    const pinnedModelId = currentChat.model_id;
    if (pinnedModelId === null || pinnedModelId === "") {
      return;
    }
    // Belt-and-braces idempotency guard: prevents a double switch/toast from
    // React 18 StrictMode's double-invoke of effects (both invocations would
    // otherwise read the same stale `selectedModel` before the state update
    // propagates). Once switched, this chat is never re-evaluated again in
    // this component instance.
    if (staleModelSwitchedForChatIdRef.current === currentChat.id) return;

    const pinnedProvider = currentChat.settings?.provider ?? "lmstudio";
    const pinnedIsAvailable = chatModelOptions.some(
      (o) => o.provider === pinnedProvider && o.id === pinnedModelId,
    );
    if (pinnedIsAvailable) return;

    // Pin is gone. Prefer a LOADED fallback — the operator's chosen fix is
    // "auto-switch to a loaded model", not merely a catalog-present one.
    // Swapping one unloaded pin for another would still trip the backend's
    // explicit-unloaded gate and defeat the purpose. Order: saved default IF
    // it's loaded, else the first loaded option, else (degenerate case —
    // nothing loaded) the first option, preserving prior behavior.
    const savedDefaultOption =
      savedDefaultModel !== undefined
        ? chatModelOptions.find((o) => o.id === savedDefaultModel)
        : undefined;
    const fallback =
      (savedDefaultOption?.loaded === true ? savedDefaultOption : undefined) ??
      chatModelOptions.find((o) => o.loaded) ??
      chatModelOptions[0];
    if (fallback === undefined) return;

    staleModelSwitchedForChatIdRef.current = currentChat.id;
    setSelectedModel(fallback.id);
    if (chatId !== null) {
      updateChat.mutate({ model_id: fallback.id, provider: fallback.provider });
    }
    push({
      variant: "warning",
      message: `Pinned model "${pinnedModelId}" is no longer available — switched to "${fallback.id}".`,
    });
  }, [
    chatModelOptionsLoading,
    chatModelOptions,
    currentChat,
    selectedModel,
    savedDefaultModel,
    chatId,
    updateChat,
    push,
    setSelectedModel,
  ]);

  // Chat-lifecycle command handlers — see useChatCommands. Merges
  // handleDeleteMessage, confirmClear state, handleClear,
  // handleClearConfirm, handleFork, handleCompact, handleMemoryPin,
  // handleDeleteChat, handlePinToggle, handleStartFirstChat into one hook.
  // Adaptation: these were originally spread across the component
  // (handleDeleteMessage right after `push`; the rest much further down).
  // This single call sits here — the earliest point after `currentChat`
  // (needed by handlePinToggle) is in scope, satisfying every handler's
  // inputs at once. Behavior-preserving: bodies are unchanged, only wrapped.
  const {
    handleDeleteMessage,
    confirmClear,
    setConfirmClear,
    handleClear,
    handleClearConfirm,
    handleFork,
    handleForkFromMessage,
    handleCompact,
    handleMemoryPin,
    handleDeleteChat,
    handlePinToggle,
    handleStartFirstChat,
  } = useChatCommands({
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
  });

  // Resolved RAG-enabled state for toggles and badges.
  // `rag_enabled` is a tri-state: true (explicit on), false (explicit off),
  // null/undefined (not set — server smart-defaults to ON when an embedder
  // is loaded). The FE must show the same state the server will use.
  const explicitRagEnabled = currentChat?.settings?.rag_enabled;
  const embeddingLoaded =
    embeddingStatusData?.embedding_status === "ok";
  const resolvedRagEnabled: boolean = explicitRagEnabled ?? embeddingLoaded;

  // Whether A/B compare mode is active for the current chat.
  const abCompareEnabled = currentChat?.settings?.ab_compare?.enabled === true;

  // Detect orphaned/interrupted streams on chat load.
  useEffect(() => {
    if (chatId === null || isInitializing) {
      setHasOrphanedStream(false);
      return;
    }
    // Check if there's a stored msg_id for this chat.
    const orphanKey = `lmchat:sse:${String(chatId)}:msg_id`;
    let storedMsgId: string | null;
    try {
      storedMsgId = localStorage.getItem(orphanKey);
    } catch {
      storedMsgId = null;
    }
    if (storedMsgId === null) {
      setHasOrphanedStream(false);
      return;
    }
    // There's a stored msg_id. Check if the messages list already has a
    // completed assistant message with this id (stream completed normally).
    // If messagesData hasn't loaded yet, we defer; this effect re-runs on
    // messagesData change.
    const msgIdNum = Number(storedMsgId);
    const alreadyCompleted =
      messagesData?.messages.some(
        (m) => m.id === msgIdNum && m.role === "assistant",
      ) ?? false;
    setHasOrphanedStream(!alreadyCompleted);
  }, [chatId, messagesData, isInitializing]);

  // Persist response_id after stream completes and always refresh messages.
  // NOTE: sseState.responseId is optional — LM Studio native streams do not
  // always include a response_id in the chat.start event.  The refetch MUST
  // fire unconditionally on completion so the message list syncs to the DB.
  // Only call storeResponseId when the value is actually present.
  useEffect(() => {
    if (sseState.status !== "complete" || chatId === null) return;
    // sseState now comes from useSSE(chatId) — a chat-keyed store selector
    // (see StreamState.chatId doc) — so it is already scoped to THIS
    // chat; a different chat's background completion can no longer land
    // here. (Prior to the 2026-08-15 store refactor this effect also
    // compared sseState.chatId against chatId to guard against exactly
    // that — a shared single-instance sseState could otherwise
    // store/clear the WRONG chat's response_id and invalidate the WRONG
    // chat's message-list cache.)
    // Skip storing the rid if the chain was invalidated by an edit that
    // happened between stream-complete and this effect running.  Also clear
    // any stale rid that may still be in localStorage from an earlier turn.
    if (chainInvalidatedRef.current) {
      chainInvalidatedRef.current = false;
      clearResponseId(chatId);
    } else if (sseState.responseId !== null) {
      storeResponseId(chatId, sseState.responseId);
    }
    // Snapshot final stats for the completed streaming message.
    finalStatsRef.current = sseState.stats;
    // Extract follow-up suggestions from the completed content.
    const completedContent = sseState.contentDeltas.join("");
    const { followups } = extractFollowups(completedContent);
    setFollowupSuggestions(followups);
    // No-flash: record the just-completed message id BEFORE the refetch so
    // the incoming persisted row can suppress its entrance animation.
    if (typeof sseState.messageId === "number") {
      recentlyStreamedIdRef.current = sseState.messageId;
    }
    // Refresh messages to load the finalized message from the server.
    //
    // The regenerate/resend/edit path (submitTurn) sets `pendingUser` the
    // same as a normal send, but the length-based auto-clear effect below
    // (`count > pendingUser.baseline`) can't track it reliably: delete_from_
    // user_message_for_resend / delete_assistant_turn_for_regenerate delete
    // the boundary row(s) THEMSELVES before this turn replays, so the
    // message count can dip and only return to (or fall short of) its
    // starting value once the replayed turn lands — "count > baseline"
    // never fires when the delete removed as many rows as the replay adds
    // back (the common case: a plain Regenerate always deletes >= 2 rows
    // for exactly 2 replayed back), leaving pendingUser stuck and the
    // optimistic bubble duplicating the now-persisted row. Clearing it
    // explicitly once THIS refetch (the one that lands the replayed turn)
    // resolves sidesteps the count math entirely and is exact regardless of
    // how many rows the turn deleted.
    void refetchMessages().then(() => {
      setPendingUser(null);
    });
    void qc.invalidateQueries({ queryKey: chatKeys.messages(chatId) });
  }, [
    sseState.status,
    sseState.responseId,
    sseState.stats,
    sseState.contentDeltas,
    sseState.messageId,
    chatId,
    refetchMessages,
    qc,
  ]);

  // OOB followups — arrive via the `followups` SSE frame AFTER chat.end.
  // The `followups` array in sseState is set by useSSE's `followups` case
  // handler. We watch it here and update suggestions when a non-empty array
  // lands. The legacy extractFollowups path above (from completedContent)
  // remains as a harmless fallback — it returns [] since the main content
  // no longer carries the HTML comment directive.
  //
  // No race with that legacy effect: `followups` is deliberately NOT in its
  // dependency array, and useSSE's `followups` handler spreads state
  // (`...s`) so `contentDeltas` keeps its reference — the legacy effect does
  // NOT re-run when the frame lands, so it cannot clobber these chips. Do not
  // add `sseState.followups` to the effect above, or it will re-clear them.
  useEffect(() => {
    // Guard against test mocks that don't carry the followups field.
    const chips = sseState.followups;
    if (!Array.isArray(chips) || chips.length === 0) return;
    // sseState comes from useSSE(chatId) — already scoped to this chat
    // (see StreamState.chatId doc) — so these followups always belong to
    // it; no cross-chat comparison needed here anymore.
    setFollowupSuggestions(chips);
  }, [sseState.followups]);

  // C3 — model-decided role adoption (next turn). Arrives via the
  // `mode_adopt` SSE frame AFTER chat.end (see streaming_service
  // _infer_mode_oob / _format_mode_adopt_frame), gated server-side by
  // lm_chat_mode_adoption_enabled (off by default). `presetId` is `null`
  // when the OOB inference found no clear match this turn — the common
  // case, since the classifier is deliberately biased toward "no change"
  // to avoid churning the persona every turn — or the flag is off;
  // either way there's nothing to apply. `adoptModelPreset` is itself a
  // no-op whenever the user has explicitly picked the chat's current
  // preset (see useChatPresetStore.adoptModel) — a manual rail-picker
  // choice always wins over an inferred one.
  useEffect(() => {
    const verdict = sseState.modeAdopt;
    if (verdict?.presetId == null) return;
    // sseState comes from useSSE(chatId) — already scoped to this chat
    // (see StreamState.chatId doc) — so this verdict always belongs to
    // the chat currently on screen; no cross-chat comparison needed here
    // anymore. adoptModelPreset itself is bound to this chat's id (via
    // useChatPreset(chatId)), and its identity changing on a chatId
    // switch is what makes this effect re-fire (harmlessly re-applying
    // the same verdict) for the chat it belongs to.
    adoptModelPreset(verdict.presetId);
  }, [sseState.modeAdopt, adoptModelPreset]);

  useSSEWarningToasts(sseState.warnings, push);

  useStoppedStreamReconciliation({
    chatId,
    sseState,
    refetchMessages,
    resetStream,
    qc,
  });

  // Drop the optimistic user message once the refetch has grown the persisted
  // list past the submit-time baseline (the real user + assistant rows have
  // landed). Keeping it until then avoids both a duplicate and a flash.
  useEffect(() => {
    if (pendingUser === null) return;
    const count = messagesData?.messages.length ?? 0;
    if (count > pendingUser.baseline) setPendingUser(null);
  }, [messagesData, pendingUser]);

  // Switching chats discards any in-flight optimistic message.
  useEffect(() => {
    setPendingUser(null);
  }, [chatId]);

  // Auto-generate the chat title after the FIRST assistant turn completes
  // (deliberately an assistantCount < 1 gate, not < 2).
  // The full contract — title
  // still default, once per chat per session, rejection swallowed, no
  // in-session retry — lives in useAutotitleEffect and is pinned by
  // test_Chat_autotitle_contract.spec.tsx. The backend is idempotent
  // (returns the existing title if user-set), so a race against a manual
  // rename is safe.
  const autotitleStore = useMemo(
    () => ({ beginGenerating, endGenerating }),
    [beginGenerating, endGenerating],
  );
  useAutotitleEffect({
    chatId,
    sseState,
    currentChat,
    messagesData,
    mutation: generateTitleMutation,
    store: autotitleStore,
    titleAttemptedRef,
  });

  // Auto-scroll to bottom when new content arrives.
  // Previous version fired
  // scrollIntoView({behavior:"smooth"}) on EVERY SSE delta — fast
  // streams produced a janky stutter as the smooth-scroll animation
  // re-started every keystroke-worth of content. Two-part fix:
  //   - During an active stream, scroll INSTANTLY so the view tracks
  //     the cursor without animation conflicts.
  //   - When the stream completes (or the persisted message list grows
  //     by a non-stream cause), use SMOOTH so the final landing
  //     position reads as intentional.
  // Plus rAF-coalesce so multiple rapid-fire deltas in the same frame
  // produce ONE scroll call, not N.
  // sseState comes from useSSE(chatId) — already scoped to this chat (see
  // StreamState.chatId doc) — so a foreign chat's stream can't drive this
  // chat's auto-scroll anymore.
  const isStreaming = sseState.status === "streaming";
  const lastScrollFrame = useRef<number | null>(null);
  useEffect(() => {
    if (lastScrollFrame.current !== null) {
      cancelAnimationFrame(lastScrollFrame.current);
    }
    lastScrollFrame.current = requestAnimationFrame(() => {
      // Only follow the stream if the user is parked at the bottom. If they
      // scrolled up to read, autoStickRef is false and we leave them be.
      if (autoStickRef.current) {
        messagesEndRef.current?.scrollIntoView({
          behavior: isStreaming ? "instant" : "smooth",
        });
      }
      lastScrollFrame.current = null;
    });
    return () => {
      if (lastScrollFrame.current !== null) {
        cancelAnimationFrame(lastScrollFrame.current);
        lastScrollFrame.current = null;
      }
    };
  }, [sseState.contentDeltas.length, messagesData?.messages.length, isStreaming]);

  // Capture the last submitted payload so the
  // stream-error banner can offer a Retry CTA without forcing the user
  // to re-type their message.  Ref because the value only needs to be
  // read by the banner; storing it in state would re-render the chat
  // on every send.
  const lastSubmitRef = useRef<{
    chatId: number;
    payload: ChatStreamPayload;
  } | null>(null);

  // Capture final stats when stream completes so the just-finished
  // assistant message can display its chip during the post-stream refetch window.
  const finalStatsRef = useRef<StreamStats | null>(null);

  // No-flash on stream-complete: the persisted message re-mounts after the
  // streaming bubble swaps out. Because the animation is on the row element
  // the re-mount re-triggers it — producing a replay of the entrance
  // translation. Capture the id that just finished before the refetch so we
  // can suppress the animation on exactly that row.
  const recentlyStreamedIdRef = useRef<number | null>(null);

  // Chain-invalidation flag: set to true by
  // handleEditUserMessage so that storeResponseId (fired by a useEffect after
  // stream-complete) skips writing if the user edited between stream-end and
  // the effect render. Cleared to false after each send.
  const chainInvalidatedRef = useRef(false);

  // Subchat-frame divider: label of the preset active when last
  // message was submitted. Cleared on next plain-message submit (no preset).
  const [activePresetLabel, setActivePresetLabel] = useState<string | null>(
    null,
  );

  // Single model-resolution ladder for turn-dispatch paths:
  // per-chat override → chat's persisted model → global saved default → "".
  // Extracted so the verbatim ladder isn't copy-pasted across handlers.
  // Sites whose ladder is meaningfully DIFFERENT do NOT use this: the preset
  // model+provider composite in handleSubmit's sub-session path, the A/B
  // model_a/model_b picks, the `?? null` inject signal, and the reads that
  // must round-trip `undefined` (the onModelChange rollback capture + the
  // Composer `modelId` prop).
  //
  // Declared here, ahead of startSubSession and handleSubSessionFinalize,
  // so it's available before the useSubSession() call below, which takes
  // it as an argument.
  const resolveTurnModel = useCallback(
    (): string =>
      selectedModel ?? currentChat?.model_id ?? savedDefaultModel ?? "",
    [selectedModel, currentChat?.model_id, savedDefaultModel],
  );

  // Sub-session mode (slash commands /research, /code, etc.) — extracted to
  // useSubSession. See that hook for the
  // SubSessionState shape and startSubSession's synchronous return value,
  // which handleSubmit below forwards to maybeRouteSubmit as
  // `explicitSubSession` for the inline-form path.
  const {
    subSession,
    startSubSession,
    handleSubSessionFinalize,
    handleSubSessionInject,
    cancelSubSession,
    subSessionSSE,
    maybeRouteSubmit,
    subSessionHistory,
    subSessionHistoryLoading,
    isSubSessionHistoryOpen,
    openSubSessionHistory,
    closeSubSessionHistory,
    reopenSubSession,
  } = useSubSession({
    chatId,
    currentChat,
    selectedModel,
    savedDefaultModel,
    resolveTurnModel,
    push,
    refetchMessages,
  });

  // "Turn this chat into a Project" — same three conditions the
  // sidebar's move-to-project affordance gates on: not already in a
  // project, not incognito, and no open sub-session (ephemeral surface;
  // the move target would be the persistent main chat, not what the
  // user is looking at).
  const canPromoteToProject =
    chatId !== null &&
    currentChat !== undefined &&
    currentChat.project_id == null &&
    currentChat.incognito !== true &&
    subSession === null;

  // Follow-up suggestion chips from the last completed assistant turn.
  const [followupSuggestions, setFollowupSuggestions] = useState<string[]>([]);

  // un-keyed followupSuggestions state leaked across chat-switch — opening
  // a fresh chat 19 from chat 18 showed chat 18's Paris follow-up chips.
  // This piece of state belongs to the chat the user was just in. Wipe on
  // chatId change. The useChatScopedState hook isn't the right shape
  // here — intentionally ephemeral (no restore-on-revisit).
  //
  // Still needed after the 2026-08-15 streamStore refactor: sseState now
  // comes from useSSE(chatId) and IS chat-scoped (a chat's own slot only
  // ever holds its own frames — see StreamState.chatId doc), but
  // followupSuggestions is a SEPARATE local useState populated FROM
  // sseState.followups by the OOB effect above, which only ever APPENDS
  // (there is no else-branch clearing it when the source array is empty)
  // — nothing else resets it on a chat switch, so without this wipe it
  // would keep showing the previous chat's chips indefinitely. Proven by
  // tests/unit/test_Chat_chatswitch_ephemeral_wipe.spec.tsx.
  //
  // This effect used to also
  // wipe subSession off this same prevChatIdRef (single combined effect).
  // That half now lives in useSubSession with its own prevChatIdRef copy;
  // both effects still fire on every chatId change, so behavior here is
  // unchanged.
  const prevChatIdRef = useRef<number | null>(chatId);
  useEffect(() => {
    if (prevChatIdRef.current !== chatId) {
      prevChatIdRef.current = chatId;
      setFollowupSuggestions([]);
    }
  }, [chatId]);

  // MTP-suspected dedupe — see useMtpSuspectedDedupe. The ref's
  // declaration + the record effect are combined there
  // (see the hook file for the StrictMode rationale); this call necessarily
  // sits here (after sseState/chatId) rather than near the original ref
  // declaration higher up in this component.
  const mtpSuspectedShownRef = useMtpSuspectedDedupe(sseState, chatId);

  // Handle submit from Composer.
  const handleSubmit = useCallback(
    (
      cid: number,
      payload: ChatStreamPayload,
      userText: string,
      presetLabel?: string,
      explicitSubSession?: SubSessionState | null,
    ): void => {
      // Sending a new message re-arms auto-stick so this turn snaps to the
      // bottom even if the user had scrolled up to read the previous answer.
      autoStickRef.current = true;
      // Sub-session: route to clean-context endpoint, bypass main stream,
      // if one is active. `explicitSubSession`
      // is only non-undefined for the Composer inline-form path, which
      // passes the session onPresetActivate (startSubSession) just created —
      // see useSubSession's maybeRouteSubmit for why that beats reading
      // `subSession` state here.
      if (maybeRouteSubmit(cid, payload, userText, explicitSubSession)) return;

      const abSettings = currentChat?.settings?.ab_compare;
      if (abCompareEnabled && abSettings !== undefined) {
        void startABStream({
          chatId: cid,
          message: userText,
          modelA: abSettings.model_a ?? selectedModel ?? "",
          modelB: abSettings.model_b ?? selectedModel ?? "",
        });
        return;
      }
      // Normal streaming mode.
      // Reset the chain-invalidation flag: the new turn starts fresh.
      chainInvalidatedRef.current = false;
      setPendingUser({
        text: userText,
        baseline: (messagesData?.messages ?? []).length,
      });
      const prevRid = loadResponseId(cid);
      const enrichedPayload: ChatStreamPayload = {
        ...payload,
        ...(prevRid !== null ? { previous_response_id: prevRid } : {}),
      };
      lastSubmitRef.current = { chatId: cid, payload: enrichedPayload };
      finalStatsRef.current = null;
      setActivePresetLabel(presetLabel ?? null);
      // Keep the prior turn's followup chips mounted across the stream
      // (they render dimmed + non-interactive via the `streaming` flag
      // on FollowupChips). Clearing them here used to collapse the
      // chip row at submit-time, then re-grow it when the new turn's
      // followups arrived — the exact layout-shift to avoid. The
      // complete-handler above replaces this array with the new turn's
      // followups when streaming ends, so the chip row swaps in place
      // without a height change.
      void startStream(cid, enrichedPayload);
    },
    [
      startStream,
      startABStream,
      abCompareEnabled,
      currentChat,
      selectedModel,
      messagesData,
      maybeRouteSubmit,
    ],
  );

  // Re-fire the last submitted payload — used by the stream-
  // error banner's Retry CTA.  No-ops if nothing has been sent yet.
  //
  // startStream's first action is to abort any in-flight AbortController
  // for that chat (streamStore.ts's start() action), so an explicit
  // stopStream() before this is
  // redundant AND introduces a race: stopStream posts an "aborted"
  // BroadcastChannel message and flips state to "idle"; startStream
  // synchronously aborts again and flips to "streaming". The interleaving
  // can produce a stale "aborted" channel notification AFTER the new
  // stream begins, confusing cross-tab subscribers. Skipping the stop and
  // letting startStream handle the cleanup eliminates the race.
  const handleStreamRetry = useCallback((): void => {
    const last = lastSubmitRef.current;
    if (last === null) return;
    // Re-arms auto-stick so this turn snaps to the bottom.
    autoStickRef.current = true;
    void startStream(last.chatId, last.payload);
  }, [startStream]);

  // A/B compare actions — start a comparison run + commit a pane's response
  // into chat history. See useAbCompareActions.ts (bodies moved verbatim).
  const { handleABCompareStart, handleAbSelect } = useAbCompareActions({
    chatId,
    updateChat,
    appendMessage,
    refetchMessages,
    push,
  });

  // ── Single turn-dispatch primitive ───────────────────────────────────────
  // Every text-driven turn that streams directly (regenerate, resend, and the
  // edit-then-regenerate flow via handleRegenerateClick) funnels through here,
  // so the model-resolution ladder, per-chat integration resolution, the
  // auto-stick re-arm, and the startStream call live in ONE place instead of
  // being copy-pasted per handler.
  //
  // NOTE on provider: `provider` is intentionally NOT part of the stream
  // payload. The backend resolves it from the STORED chat setting
  // (chat.settings.provider — streaming_service reads _provider_name from it)
  // and CanonicalChatRequest has no `provider` field, so a payload `provider`
  // key would be silently dropped. The model id on the wire is the bare id;
  // the top-bar composite "<provider>::<id>" is decoded at selection time and
  // the provider persisted to chat.settings separately (onModelChange).
  //
  // NOTE on scope: handleSubmit does NOT route through here — it carries a
  // richer Composer-built payload (images, system_prompt, temperature) plus
  // pendingUser / previous_response_id / lastSubmitRef bookkeeping and
  // sub-session / A-B routing. Follow-up chips deliberately go through
  // handleSubmit (not submitTurn) so they keep the previous_response_id chain
  // and the optimistic user bubble. handleRetryInterruptedStream keeps its own
  // resume payload (empty input + previous_response_id).
  const submitTurn = useCallback(
    (
      turnChatId: number,
      inputText: string,
      opts?: { integrations?: string[] },
    ): void => {
      const model = resolveTurnModel();
      if (model === "") {
        push({ variant: "error", message: "Pick a model before sending." });
        return;
      }
      const integrations =
        opts?.integrations ?? resolveChatIntegrationsField(turnChatId);
      const payload: ChatStreamPayload = {
        input: [{ type: "text", content: inputText }],
        model,
        ...(integrations !== undefined && { integrations }),
      };
      // Optimistic user bubble — mirrors handleSubmit's pendingUser (consumed
      // by deriveMessageList's optimisticUserMessages). Without this,
      // regenerate/resend/edit callers of submitTurn went straight to
      // startStream with no optimistic row: the caller just deleted the
      // boundary user message server-side (delete_from_user_message_for_resend
      // / delete_assistant_turn_for_regenerate both delete the boundary row
      // itself, not just what follows it), the messages refetch dropped it
      // from serverMessages, and nothing replaced it until the replayed
      // turn's new row was refetched at stream-complete — the resent/
      // regenerated message vanished for the full duration of generation.
      // Baseline is the current (pre-turn) count, same as handleSubmit —
      // the stream-complete effect's explicit `setPendingUser(null)` after
      // its refetch (not this baseline) is what actually clears the bubble
      // for this path; see that effect's comment for why a delete-aware
      // baseline can't be made to work with the length-comparison check.
      setPendingUser({
        text: inputText,
        baseline: (messagesData?.messages ?? []).length,
      });
      // Re-arms auto-stick so this turn snaps to the bottom.
      autoStickRef.current = true;
      void startStream(turnChatId, payload);
    },
    [resolveTurnModel, startStream, push, messagesData],
  );

  // Message-action handlers — regenerate/resend/edit/retry, plus the
  // regenerate-confirm modal state. See useMessageActions.ts (bodies moved
  // verbatim). Declared after submitTurn since handleRegenerateClick and
  // handleRegenerateConfirm both depend on it.
  const {
    regenConfirm,
    setRegenConfirm,
    handleRegenerateClick,
    handleResendClick,
    handleEditUserMessage,
    handleRegenerateConfirm,
    handleRetryInterruptedStream,
  } = useMessageActions({
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
  });

  // Dispatch palette-selected commands through the same handlers Composer
  // uses for inline slash commands.  Keeps a single source of truth for
  // command behaviour.
  const handlePaletteSelect = useCallback((cmd: SlashCommand): void => {
    // Preset-mode commands launch a transient sub-agent session.
    // They do NOT write active_preset — that is set only via the rail picker.
    if (cmd.presetId !== undefined && chatId !== null) {
      startSubSession(cmd.presetId);
      return;
    }
    switch (cmd.name) {
      case "help":
        push({
          variant: "info",
          message: BUILTIN_COMMANDS.map((c) => `/${c.name}: ${c.description}`).join("\n"),
          duration: 8_000,
        });
        break;
      case "clear":
        handleClear();
        break;
      case "memory":
        push({ variant: "info", message: "Use /memory <text> in the composer to pin an insight." });
        break;
      case "compact":
        void handleCompact();
        break;
      case "fork":
        void handleFork();
        break;
      case "prompt":
        push({ variant: "info", message: "Use /prompt <name> in the composer to insert a saved prompt." });
        break;
      case "panel":
        push({ variant: "info", message: "/panel is coming — multi-model convergence in one view." });
        break;
      default:
        push({ variant: "warning", message: `Unknown command: /${cmd.name}` });
        break;
    }
  }, [push, handleCompact, handleFork, handleClear, chatId, startSubSession]);

  // Cmd/Ctrl+N — create a new chat then navigate to it.
  const handleNewChatShortcut = useCallback((): void => {
    createChat.mutate(
      { title: "New Chat" },
      {
        onSuccess: (chat) => {
          void navigate(`/chats/${String(chat.id)}`);
        },
        onError: () => {
          push({
            variant: "error",
            message: "Couldn't create a new chat — try again.",
          });
        },
      },
    );
  }, [createChat, navigate, push]);

  // Keyboard shortcuts.
  useKeyboardShortcuts({
    onFocusSearch: () => {
      const input = document.querySelector<HTMLInputElement>(
        "input[aria-label='Filter chats']",
      );
      input?.focus();
    },
    onCommandPalette: () => {
      // Cmd/Ctrl+/ opens the SlashPalette popover.  Toggles closed
      // if it's already open (chord re-press dismisses).
      setSlashPaletteOpen((v) => !v);
    },
    onEscape: () => {
      // Esc closes whichever overlay is topmost — help first, then palette,
      // then panel, then finally exits focus mode.  The Esc priority chain
      // mirrors v0.5.x; focus mode sits last so an open overlay closes before
      // the whole focus surface tears down.
      if (keyboardHelpOpen) {
        setKeyboardHelpOpen(false);
        return;
      }
      if (slashPaletteOpen) {
        setSlashPaletteOpen(false);
        return;
      }
      if (panelView !== null) {
        setPanelView(null);
        return;
      }
      if (focusMode) {
        setFocusMode(false);
        return;
      }
      setPanelView(null);
    },
    onToggleThinking: () => {
      // Global thinking toggle deferred; per-message toggle is in ThinkingBlock.
    },
    // Cmd/Ctrl+N — new chat.
    onNewChat: handleNewChatShortcut,
    // Cmd/Ctrl+Shift+S — toggle sidebar.
    onToggleSidebar: () => {
      setSidebarCollapsed((v) => !v);
    },
    // Cmd/Ctrl+, — open Settings.
    onOpenSettings: () => {
      void navigate("/settings");
    },
    // `?` — open keyboard help modal.
    onShowHelp: () => {
      setKeyboardHelpOpen(true);
    },
    // Cmd/Ctrl+Shift+E — open chat export menu.
    onExportChat: () => {
      if (chatId === null) return;
      setExportMenuSignal((n) => n + 1);
    },
    // Cmd/Ctrl+. — toggle focus mode.
    onToggleFocusMode: toggleFocusMode,
  });

  // A/B compare model labels — resolved once here to avoid optional-chain
  // exhaustion warnings inside JSX (ESLint can't see the abCompareEnabled guard).
  const abModelALabel = currentChat?.settings?.ab_compare?.model_a ?? "Model A";
  const abModelBLabel = currentChat?.settings?.ab_compare?.model_b ?? "Model B";

  const {
    activeServerMessages,
    optimisticUserMessages,
    streamActive,
    streamingMessages,
    allMessages,
    allMessagesForPins,
  } = deriveMessageList({
    serverMessagesRaw: messagesData?.messages ?? [],
    finalStats: finalStatsRef.current,
    pendingUser,
    sseState,
    chatId,
  });

  // Presence-state machine — drives <html data-presence> attribute
  // which body::before in globals.css uses to shift bloom intensity + pace.
  const reasoningCount = sseState.reasoningDeltas.length;
  const contentCount = sseState.contentDeltas.length;
  // True only while still in the reasoning-only phase (no content yet).
  const hasReasoningContent = reasoningCount > 0 && contentCount === 0;
  const chatIsActive =
    chatId !== null &&
    ((messagesData?.messages.length ?? 0) > 0 || streamActive);
  const { notifyComposerFocused, notifyComposerBlurred, notifyTyping } =
    usePresence({
      // sseState comes from useSSE(chatId) — already scoped to this chat —
      // so a foreign chat's activity can't drive this chat's ambient
      // presence state anymore.
      streaming: sseState.status === "streaming",
      hasReasoningContent,
      chatActive: chatIsActive,
    });

  // Mouse parallax — near-imperceptible bloom shift tracking cursor.
  useMouseParallax();

  // Estimate history token count from activeServerMessages so Composer's
  // context meter reflects full
  // context usage, not just the current draft. Same chars÷4 heuristic as
  // the Composer uses for the draft. System prompt included via
  // customInstructions (currentChat?.settings?.system_prompt).
  const historyTokens = activeServerMessages.reduce((sum, m) => {
    const contentLen = typeof m.content === "string" ? m.content.length : 0;
    const reasoningLen = typeof m.reasoning_content === "string" ? m.reasoning_content.length : 0;
    return sum + Math.ceil((contentLen + reasoningLen) / 4);
  }, Math.ceil((currentChat?.settings?.system_prompt?.length ?? 0) / 4))
    + compactions.reduce((s, c) => s + c.summary_token_count, 0);

  const presenceCbs: ComposerPresenceCbs = {
    onComposerFocused: notifyComposerFocused,
    onComposerBlurred: notifyComposerBlurred,
    onComposerTyping: notifyTyping,
  };

  // While mount-time /me hydration is in flight, render a loading placeholder
  // instead of immediately redirecting to /login (which would cause a flash of
  // unauthenticated state for users with a valid session cookie).
  // Prevents a flash of unauthenticated state during /me hydration.
  if (isInitializing) {
    return (
      <div className="lmchat-loading-wrap">
        <p className="lmchat-loading-text">Loading…</p>
      </div>
    );
  }

  // Redirect to login if not authenticated. Preserve the current URL
  // as ?returnTo= so the login page can bounce the user back after
  // authenticating — bookmarked deep links + tab restores stop losing
  // their destination.
  if (user === null) {
    const here = window.location.pathname + window.location.search;
    const dest =
      here && here !== "/"
        ? `/login?returnTo=${encodeURIComponent(here)}`
        : "/login";
    return <Navigate to={dest} replace />;
  }

  return (
    <div
      className={`lmchat-app-shell lmchat-chat-shell${
        focusMode ? " is-focus-mode" : ""
      }${motionEnabled ? " lmchat-focus-animated" : ""}${
        revealLocked ? " lmchat-focus-reveal-locked" : ""
      }`}
    >
      <MobileSidebarShell
        isMobile={isMobile}
        mobileDrawerOpen={mobileDrawerOpen}
        drawerClosing={drawerClosing}
        sidebarCollapsed={sidebarCollapsed}
        setSidebarCollapsed={setSidebarCollapsed}
        onShowKeyboardHelp={() => {
          setKeyboardHelpOpen(true);
        }}
        endDrawerClose={endDrawerClose}
      />

      {/* Center column */}
      <main id="main-content" tabIndex={-1} className="lmchat-main-column">
        {/* Focus mode: a thin top-edge sentinel. Hovering it slides the hidden
            top chrome back down so the model picker stays reachable (see
            chat.css .lmchat-focus-hover-zone). Inert outside focus mode. */}
        <div className="lmchat-focus-hover-zone" aria-hidden />
        {/* R1b: surface the LM Studio 401 / key-pruned state on the chat view
            (Chat lives outside AppShell, so its banner never reached here). */}
        <LmStudioAuthBanner />
        {/* Top bar */}
        <TopBar
          title={
            currentChat?.title ??
            (chatId !== null ? `Chat ${String(chatId)}` : BRAND_NAME)
          }
          hasDefaultModel={savedDefaultModel !== undefined}
          modelId={(() => {
            // No explicit per-chat override = no memory-tier dropdown pick
            // (selectedModel) AND no persisted chats.model_id (null or "").
            const picked = selectedModel ?? currentChat?.model_id ?? "";
            if (picked !== "") {
              const prov = currentChat?.settings?.provider ?? "lmstudio";
              return `${prov}::${picked}`;
            }
            // Show "Auto" for the no-override case — it stands in for the
            // user's default, which the send path (Composer `modelId` prop +
            // resolveTurnModel) still resolves to savedDefaultModel at dispatch
            // time. The DISPLAY deliberately does NOT surface the default's
            // name. When there's no default configured at all, fall to "" so
            // the picker prompts "Select a model…" (Auto would resolve to
            // nothing and the composer blocks the send).
            return savedDefaultModel !== undefined ? AUTO_MODEL_VALUE : "";
          })()}
          onModelChange={(compositeId: string) => {
            // "Auto" reset: drop the per-chat override so the picker + send
            // path fall back to the user's default model. Clear the memory-tier
            // pick AND the persisted chats.model_id (via clear=model_id — the
            // flat model_id="" param is ignored server-side by design).
            if (compositeId === AUTO_MODEL_VALUE) {
              const prevSelected = selectedModel;
              setSelectedModel(undefined);
              const hasPersisted =
                currentChat?.model_id != null && currentChat.model_id !== "";
              if (chatId !== null && hasPersisted) {
                updateChat.mutate(
                  { clear: "model_id" },
                  {
                    onError: (err) => {
                      // Rollback optimistic clear.
                      setSelectedModel(prevSelected);
                      const detail =
                        (err as { detail?: unknown }).detail ??
                        (err instanceof Error ? err.message : String(err));
                      const suffix =
                        typeof detail === "string" && detail.length > 0
                          ? ` — ${detail}`
                          : "";
                      push({
                        variant: "error",
                        message: `Model couldn't be reset${suffix}`,
                      });
                    },
                  },
                );
              }
              return;
            }
            // Decode composite "<provider>::<model_id>" — split on FIRST :: only.
            const sepIdx = compositeId.indexOf("::");
            const provider = sepIdx >= 0 ? compositeId.slice(0, sepIdx) : "lmstudio";
            const modelId = sepIdx >= 0 ? compositeId.slice(sepIdx + 2) : compositeId;

            const prev = selectedModel ?? currentChat?.model_id ?? savedDefaultModel;
            setSelectedModel(modelId);
            if (chatId !== null && compositeId !== "") {
              updateChat.mutate({ model_id: modelId, provider }, {
                onError: (err) => {
                  // Rollback optimistic update.
                  setSelectedModel(prev);
                  const detail =
                    (err as { detail?: unknown }).detail ??
                    (err instanceof Error ? err.message : String(err));
                  const suffix = typeof detail === "string" && detail.length > 0
                    ? ` — ${detail}`
                    : "";
                  push({ variant: "error", message: `Model couldn't be saved${suffix}` });
                },
              });
            }
          }}
          onMobileMenuClick={
            // Hide the hamburger when the drawer is
            // already open — the backdrop's "Close sidebar" overlay
            // serves as the single close affordance, so we don't show
            // two competing controls.
            isMobile && !mobileDrawerOpen
              ? () => {
                  setSidebarCollapsed(false);
                }
              : undefined
          }
          pinned={currentChat?.pinned ?? false}
          onPinToggle={() => {
            void handlePinToggle();
          }}
          onDelete={() => {
            void handleDeleteChat();
          }}
          onFork={() => {
            void handleFork();
          }}
          onSettingsOpen={() => {
            setPanelView((v) => (v === "settings" ? null : "settings"));
          }}
          onMemoryOpen={() => {
            setPanelView((v) => (v === "memory" ? null : "memory"));
          }}
          onDocumentsOpen={() => {
            setPanelView((v) => (v === "documents" ? null : "documents"));
          }}
          onPinsOpen={() => {
            setPinnedPanelOpen((v) => !v);
          }}
          pinsOpen={pinnedPanelOpen}
          panelView={panelView}
          chatId={chatId}
          ragEnabled={resolvedRagEnabled}
          onRagToggle={
            chatId !== null
              ? () => {
                  void updateChat.mutateAsync({ rag_enabled: !resolvedRagEnabled });
                }
              : undefined
          }
          incognito={currentChat?.incognito === true}
          subAgentLabel={subSession?.presetLabel ?? null}
          onPromoteToProject={
            canPromoteToProject
              ? () => {
                  setPromoteModalOpen(true);
                }
              : undefined
          }
          exportChat={
            currentChat !== undefined
              ? {
                  id: currentChat.id,
                  title: currentChat.title,
                  // ChatSummary doesn't expose created_at; fall back to the
                  // chat updated_at (the export preamble only uses this as
                  // a courtesy timestamp).
                  created_at: currentChat.updated_at,
                }
              : null
          }
          exportMessages={messagesData?.messages ?? []}
          exportMenuSignal={exportMenuSignal}
          focusMode={focusMode}
          onToggleFocusMode={toggleFocusMode}
          onSubSessionHistoryOpen={
            chatId !== null ? openSubSessionHistory : undefined
          }
        />

        {/* Pin-nav strip — only when this chat has pins. */}
        {chatId !== null && (
          <PinNavStrip chatId={chatId} messages={allMessagesForPins} />
        )}

        {/* Pinned-messages panel — floating overlay anchored
            to the TopBar Pins button.  Self-positions absolutely. */}
        {chatId !== null && (
          <PinnedMessagesPanel
            chatId={chatId}
            open={pinnedPanelOpen}
            onClose={() => {
              setPinnedPanelOpen(false);
            }}
            messages={allMessagesForPins}
          />
        )}

        {/* Messages */}
        <div
          className="lmchat-messages-area"
          ref={messagesAreaRef}
          onScroll={handleMessagesScroll}
        >
          {chatId === null ? (
            <EmptyState
              onNewChat={() => {
                void handleStartFirstChat();
              }}
            />
          ) : abCompareEnabled ? (
            /* A/B compare mode: render two-pane view instead of message list */
            <ABComparePane
              abState={abState}
              modelALabel={abModelALabel}
              modelBLabel={abModelBLabel}
              onSelect={handleAbSelect}
              onExit={() => {
                void updateChat
                  .mutateAsync({ ab_compare_enabled: false })
                  .catch(() => {
                    push({
                      variant: "error",
                      message: "Couldn't exit compare mode — try again.",
                    });
                  });
              }}
            />
          ) : subSession !== null || isSubSessionHistoryOpen ? (
            /* Sub-session mode: clean-context conversation with the preset
               agent, OR (P4) browsing this chat's past sub-sessions when
               none is currently open. */
            <SubSessionPanel
              subSession={subSession}
              sseState={subSessionSSE.state}
              onFinalize={handleSubSessionFinalize}
              onInject={handleSubSessionInject}
              onCancel={cancelSubSession}
              history={subSessionHistory}
              historyLoading={subSessionHistoryLoading}
              isHistoryOpen={isSubSessionHistoryOpen}
              onOpenHistory={openSubSessionHistory}
              onCloseHistory={closeSubSessionHistory}
              onReopen={reopenSubSession}
            />
          ) : (
            <MessageListBody
              allMessages={allMessages}
              sseState={sseState}
              currentChat={currentChat}
              activeServerMessages={activeServerMessages}
              compactions={compactions}
              chatId={chatId}
              handleEditUserMessage={handleEditUserMessage}
              handleRegenerateClick={handleRegenerateClick}
              handleResendClick={handleResendClick}
              handleForkFromMessage={handleForkFromMessage}
              handleDeleteMessage={handleDeleteMessage}
              onLaunchMode={(presetId) => {
                // Same launch path the Composer's inline "/research" slash
                // command uses (see dispatchSlashCommand in Composer.tsx).
                // Composer isn't a forwardRef component today, so there's
                // no imperative handle to focus its textarea from here —
                // handlePaletteSelect (the Cmd+K palette's preset path,
                // above) has the same gap. Left as-is rather than adding a
                // forwardRef just for this.
                startSubSession(presetId);
              }}
              currentPersonaLabel={currentPersonaLabel}
              currentPersonaAdopted={currentPersonaAdopted}
              recentlyStreamedIdRef={recentlyStreamedIdRef}
              activePresetLabel={activePresetLabel}
              pendingUser={pendingUser}
              optimisticUserMessages={optimisticUserMessages}
              streamingMessages={streamingMessages}
              followupSuggestions={followupSuggestions}
              resolveTurnModel={resolveTurnModel}
              push={push}
              handleSubmit={handleSubmit}
              mtpSuspectedShownRef={mtpSuspectedShownRef}
              lastSubmitRef={lastSubmitRef}
              handleStreamRetry={handleStreamRetry}
              stopStream={stopStream}
              navigate={navigate}
              messagesEndRef={messagesEndRef}
            />
          )}
        </div>

        {/* Interrupted stream row */}
        {chatId !== null &&
          hasOrphanedStream &&
          // sseState comes from useSSE(chatId) — already scoped to this
          // chat (see StreamState.chatId doc) — so a different chat's
          // stream can't suppress this banner anymore.
          sseState.status !== "streaming" && (
            <InterruptedRow onRetry={handleRetryInterruptedStream} />
          )}

        {/* Mobile-only persistent panel dock — primary navigation surface for
            Memory / Docs / RAG / Settings, always visible above the composer
            so it's never buried behind an overflow menu. */}
        {isMobile && chatId !== null && (
          <MobileDock
            onMemoryOpen={() => {
              setPanelView((v) => (v === "memory" ? null : "memory"));
            }}
            onDocumentsOpen={() => {
              setPanelView((v) => (v === "documents" ? null : "documents"));
            }}
            onRagToggle={() => {
              void updateChat.mutateAsync({ rag_enabled: !resolvedRagEnabled });
            }}
            onSettingsOpen={() => {
              setPanelView((v) => (v === "settings" ? null : "settings"));
            }}
            panelView={panelView}
            ragEnabled={resolvedRagEnabled}
          />
        )}

        {/* Composer */}
        {chatId !== null && (
          <Composer
            chatId={chatId}
            streaming={
              subSession !== null
                ? subSessionSSE.state.status === "streaming"
                : abCompareEnabled
                  ? abState.status === "streaming"
                  // sseState comes from useSSE(chatId) — already scoped
                  // to this chat — so a foreign chat's stream can't show
                  // this chat's Composer as streaming anymore.
                  : sseState.status === "streaming"
            }
            onSubmit={handleSubmit}
            onStop={
              subSession !== null
                ? subSessionSSE.abort
                : abCompareEnabled
                  ? stopABStream
                  : stopStream
            }
            onClear={handleClear}
            onFork={() => {
              void handleFork();
            }}
            onCompact={() => {
              void handleCompact();
            }}
            onMemoryPin={(text) => {
              void handleMemoryPin(text);
            }}
            onPresetActivate={startSubSession}
            onABCompareStart={handleABCompareStart}
            modelId={
              selectedModel ?? currentChat?.model_id ?? savedDefaultModel
            }
            presence={presenceCbs}
            customInstructions={currentChat?.settings?.system_prompt ?? ""}
            historyTokens={historyTokens}
          />
        )}
        {/* No-model composer hint — quiet line below composer
            when LM Studio has no loaded model. Textarea stays enabled so
            the user can compose without being blocked. */}
        {chatId !== null && noModelLoaded && (
          <p className="lmchat-no-model-hint">
            Load a model in LM Studio to send.
          </p>
        )}
      </main>

      {/* Right panel */}
      {panelView !== null && (
        <RightPanel
          view={panelView}
          chatId={chatId}
          onClose={() => {
            setPanelView(null);
          }}
        />
      )}

      {/* Cmd/Ctrl+/ slash command palette */}
      <SlashPalette
        open={slashPaletteOpen}
        onClose={() => {
          setSlashPaletteOpen(false);
        }}
        onSelect={handlePaletteSelect}
      />

      {/* Keyboard shortcuts help modal — opened by `?`
          or the Sidebar footer ? button. */}
      <KeyboardHelp
        open={keyboardHelpOpen}
        onClose={() => {
          setKeyboardHelpOpen(false);
        }}
      />

      {/* "Turn this chat into a Project" — opened from the ⋯ overflow menu
          (onPromoteToProject is only set when canPromoteToProject holds). */}
      {chatId !== null && (
        <PromoteToProjectModal
          open={promoteModalOpen}
          onClose={() => {
            setPromoteModalOpen(false);
          }}
          chatId={chatId}
          chatTitle={currentChat?.title ?? ""}
          focusedDocumentId={currentChat?.settings?.focused_document_id ?? null}
        />
      )}

      {/* /clear confirm dialog removed — the feature is not
          yet implemented. The /clear command now shows a toast directly. */}

      {/* Regenerate / resend confirm modal.  The same confirm gate
          backs both "Regenerate" (assistant message) and "Resend" (user
          message) — role-aware copy so a resend never reads "Regenerating".
          subsequent_count is the number of later messages the action removes
          before replaying the turn. */}
      {regenConfirm !== null && (
        <RegenConfirmDialog
          regenConfirm={regenConfirm}
          targetRole={
            messagesData?.messages.find(
              (m) => m.id === regenConfirm.message_id,
            )?.role
          }
          onConfirm={() => {
            void handleRegenerateConfirm();
          }}
          onCancel={() => {
            setRegenConfirm(null);
          }}
        />
      )}
      {confirmClear && (
        <ConfirmDialog
          message="Clear this chat's history? All messages will be permanently deleted. The chat and its settings are kept."
          onConfirm={() => {
            void handleClearConfirm();
          }}
          onCancel={() => {
            setConfirmClear(false);
          }}
        />
      )}

      {/* Slim, always-reachable exit affordance — only visible in focus mode. */}
      <FocusModeExit
        active={focusMode}
        onExit={() => {
          setFocusMode(false);
        }}
      />
    </div>
  );
}
