/* SPDX-License-Identifier: Apache-2.0 */
import "@/styles/composer.css";
/**
 * Composer — multi-line message input with slash-command autocomplete.
 *
 * Submit: Enter (Cmd/Ctrl+Enter also sends). Newline: Shift+Enter.
 * Stop: Shown while streaming; sends an abort signal.
 * Slash commands: triggered when input starts with "/".
 * File attach: always available (text files fold into the message text);
 *   image uploads additionally require the active model's vision capability.
 * STT: Mic button (Cmd/Ctrl+Shift+M) — Web Speech API, browser-side only.
 */
import {
  useRef,
  useState,
  useCallback,
  useEffect,
  useMemo,
  type CSSProperties,
  type KeyboardEvent,
  type ChangeEvent,
} from "react";
// Typing data-attribute for atmospheric ::before flare.
// Tracks whether the user is actively typing (300ms debounce).
const TYPING_DEBOUNCE_MS = 300;
import { Paperclip, Square, SendHorizontal, TriangleAlert, X } from "lucide-react";
import {
  SlashMenu,
  parseSlashCommand,
  BUILTIN_COMMANDS,
  filterCommands,
} from "@/components/SlashMenu";
import type { SlashCommand } from "@/components/SlashMenu";
import { useToast } from "@/stores/toastStore";
import type { ChatStreamPayload } from "@/hooks/useSSE";
// Type-only — verbatimModuleSyntax erases this at compile time, so it does
// not create a runtime circular import with useSubSession.ts's (value)
// import of resolveChatIntegrationsField from this module.
import type { SubSessionState } from "@/hooks/useSubSession";
import { MicButton } from "@/components/MicButton";
import { usePrompts } from "@/hooks/usePrompts";
import { useIntegrationsList } from "@/hooks/useIntegrationsList";
import { useLmStudioConfig } from "@/hooks/useLmStudioConfig";
import { partitionAttachments } from "@/lib/attachments";
import { useChatScopedState } from "@/hooks/useChatScopedState";
import { useChatPreset } from "@/hooks/useChatPreset";
import { PRESET_BY_SLASH_CMD } from "@/lib/presets";
import { usePlatform } from "@/hooks/usePlatform";
import { useViewport } from "@/hooks/useViewport";
import { InProjectChip } from "@/components/InProjectChip";
import { RagModeBadge } from "@/components/RagModeBadge";
import { useModels } from "@/hooks/useModels";
import { useComposerStt } from "@/hooks/useComposerStt";
import { useComposerAttachments } from "@/hooks/useComposerAttachments";
import { ComparePicker } from "@/components/ComparePicker";

// ─── Presence callbacks (optional — wired from Chat.tsx via usePresence) ─────

export interface ComposerPresenceCbs {
  onComposerFocused?: () => void;
  onComposerBlurred?: () => void;
  onComposerTyping?: () => void;
}

// ─── Props ───────────────────────────────────────────────────────────────────

interface ComposerProps {
  chatId: number;
  /** True while SSE stream is active. */
  streaming: boolean;
  /** Called when the user submits a message.
   * `presetLabel` is the label of the active preset at submit time
   * (e.g. "Coder", "Research") so Chat.tsx can render the subchat divider.
   * `explicitSubSession`: the inline-form preset path (dispatchSlashCommand)
   * passes onPresetActivate's just-created return value straight through
   * here so Chat.tsx's sub-session router doesn't have to wait for a
   * re-render to see it — see onPresetActivate below. */
  onSubmit: (
    chatId: number,
    payload: ChatStreamPayload,
    userText: string,
    presetLabel?: string,
    explicitSubSession?: SubSessionState | null,
  ) => void;
  /** Called when the user clicks Stop. */
  onStop: () => void;
  /** Called when /clear is requested (delegate to parent for confirm dialog). */
  onClear: () => void;
  /** Called when /fork is requested. */
  onFork: () => void;
  /** Called when /compact is requested. */
  onCompact: () => void;
  /** Called when /memory <text> is submitted. */
  onMemoryPin: (text: string) => void;
  /** Called when a preset slash command activates — lets Chat.tsx start the
   *  sub-session. Returns the freshly-created SubSessionState (or null)
   *  synchronously so the inline-form path (`/research <query>` in one
   *  keystroke) can forward it to onSubmit instead of relying on a
   *  re-render having landed. */
  onPresetActivate?: ((presetId: string) => SubSessionState | null) | undefined;
  /** Called when /compare confirms — Chat.tsx patches ab_compare settings on. */
  onABCompareStart?: ((modelA: string, modelB: string) => void) | undefined;
  /** Current model id (optional override). */
  modelId?: string | undefined;
  /** Presence machine callbacks (optional). */
  presence?: ComposerPresenceCbs | undefined;
  /** Context blocks staged for the next send (e.g. sub-session summaries).
   *  Rendered as chips above the textarea; consumed on submit. */
  contextBlocks?:
    | { id: string; label: string; content: string; source: string }[]
    | undefined;
  /** Called when the user clicks ✕ on a context chip to dismiss it. */
  onRemoveContextBlock?: ((id: string) => void) | undefined;
  /** Per-chat custom instructions (chat.settings.system_prompt). Appended
   *  below the preset's system prompt before sending. Optional; when
   *  absent, the preset's prompt is used as-is. */
  customInstructions?: string;
  /**
   * Estimated token count for
   * the conversation history (system prompt + all prior messages). Computed
   * by the parent from serverMessages using the same chars÷4 heuristic.
   * Added to the composer draft's estimate so the context meter reflects
   * actual context usage, not just the current draft.
   */
  historyTokens?: number;
}

// ─── Per-chat integrations persistence ───────────────────────────────────────

/**
 * Legacy localStorage key shape — pinned via
 * `localStorageKeyOverride` so existing user selections survive the move
 * to `useChatScopedState`. Do NOT change this shape.
 */
function integrationsStorageKey(chatId: number | null): string {
  return `lmchat:composer:integrations:${String(chatId)}`;
}

function isStringArray(v: unknown): v is string[] {
  return Array.isArray(v) && v.every((x) => typeof x === "string");
}

/**
 * Whether localStorage holds ANY entry for this chat's integrations —
 * including an explicit empty array, which is a real user selection
 * ("tools off for this chat") and must suppress the defaults seed.
 */
function hasStoredIntegrations(chatId: number | null): boolean {
  if (typeof window === "undefined") return false;
  try {
    return (
      window.localStorage.getItem(integrationsStorageKey(chatId)) !== null
    );
  } catch {
    return false;
  }
}

/**
 * Returns the stored integrations selection for a chat when it exists
 * (including an explicit empty array, which means "tools off"), or
 * `undefined` when the chat has never had an explicit selection stored.
 *
 * Callers that build stream/finalize payloads (regenerate, retry,
 * followup chips, sub-session finalize) spread this with:
 *   `...(field !== undefined && { integrations: field })`
 * so that an explicit `[]` reaches the BE (honouring "tools off") while
 * an absent entry lets the BE apply admin defaults (the pre-d82c651
 * behaviour for untouched chats).
 */
export function resolveChatIntegrationsField(
  chatId: number | null,
): string[] | undefined {
  if (!hasStoredIntegrations(chatId)) return undefined;
  if (typeof window === "undefined") return undefined;
  try {
    const raw = window.localStorage.getItem(integrationsStorageKey(chatId));
    if (raw === null) return undefined;
    const parsed: unknown = JSON.parse(raw);
    if (Array.isArray(parsed) && parsed.every((x) => typeof x === "string")) {
      return parsed;
    }
    return undefined;
  } catch {
    return undefined;
  }
}

const NO_SELECTED_INTEGRATIONS: string[] = [];

// ─── Component ──────────────────────────────────────────────────────────────

export function Composer({
  chatId,
  streaming,
  onSubmit,
  onStop,
  onClear,
  onFork,
  onCompact,
  onMemoryPin,
  onPresetActivate,
  onABCompareStart,
  modelId,
  presence,
  contextBlocks,
  onRemoveContextBlock,
  customInstructions = "",
  historyTokens = 0,
}: ComposerProps) {
  const platform = usePlatform();
  // newlineShortcut variable used to embed the formatted keyboard hint
  // into the placeholder. That was removed (placeholders aren't the
  // place for instructions per WCAG 2.5). Keep the
  // platform handle for any future shortcut surfacing.
  void platform;
  const { isMobile } = useViewport();

  // Resolve capabilities for the current model so downstream logic can
  // gate on vision, tool_use, and context length.
  const { data: modelData, refetch: refetchModels } = useModels();
  const selectedModel = modelId !== undefined && modelId !== ""
    ? (modelData?.models.find((m) => m.id === modelId) ??
       modelData?.models.find((m) => m.loaded_instance_ids.includes(modelId)))
    : undefined;
  const modelCapabilities = selectedModel?.capabilities ?? null;
  // Text-file attachments never need vision — they're
  // decoded and folded into the message text (see attachments.ts), so the
  // attach control itself is never gated on vision. Only IMAGE uploads need
  // it. Unknown/not-yet-loaded capabilities are treated like non-vision:
  // text always works, images are blocked until vision is confirmed true.
  const isVision = modelCapabilities?.vision === true;
  // The context meter must show the LOADED
  // context (e.g. 98304 for a 9B loaded at 96k), NOT the model's
  // architectural max (e.g. 262144). Fall back to the max only when
  // nothing is loaded — at that point the meter is decorative anyway.
  const modelMaxContextLength =
    // eslint-disable-next-line @typescript-eslint/prefer-nullish-coalescing -- 0 is a genuine "not yet loaded" sentinel here (see the refetch effect below); `??` would treat it as a set value and skip the max_context_length fallback.
    (selectedModel?.loaded_context_length || selectedModel?.max_context_length) ??
    0;
  // When the selected model is loaded but `loaded_context_length`
  // is still 0 (stale cache from before the model was loaded into LM Studio),
  // immediately trigger a refetch so the fresh loaded context replaces the
  // architectural max within seconds. Without this a new chat shows the
  // arch-max (e.g. 1M) instead of the actual loaded window (e.g. 96k) until
  // the 25s refetchInterval fires on its own.
  useEffect(() => {
    if (
      selectedModel?.loaded === true &&
      selectedModel.loaded_context_length === 0
    ) {
      void refetchModels();
    }
    // Run only when the selected model identity or loaded-context changes —
    // NOT on every render `selectedModel` itself would (it's a fresh object
    // from .find() each time), so the guard above is written entirely in
    // terms of the same optional-chained fields listed below.
  }, [
    selectedModel?.id,
    selectedModel?.loaded,
    selectedModel?.loaded_context_length,
    refetchModels,
  ]);
  // On narrow viewports the textarea max
  // height eats too much of the chat column.  Cap at ~120px on mobile
  // so the message area stays visible.
  const textareaMaxPx = isMobile ? 120 : 200;
  const [text, setText] = useState("");
  const [showSlash, setShowSlash] = useState(false);
  const [slashActiveIdx, setSlashActiveIdx] = useState(0);
  // /compare picker state.
  const [showComparePicker, setShowComparePicker] = useState(false);
  const [compareModelA, setCompareModelA] = useState("");
  const [compareModelB, setCompareModelB] = useState("");

  // Per-chat integrations state, seeded from admin defaults.
  // Decision: per-chat (state persists for this chat session, resets on new
  // chat). Rationale: MCP integrations are a session-level concern — a user
  // typically wants the same set of tools active throughout a conversation.
  //
  // Persisted in localStorage keyed by chat id (previously fixed the
  // "refresh wiped tools" regression with bespoke
  // hydrate/persist/scrub logic). That logic is now folded into
  // `useChatScopedState("local")`; the
  // legacy key shape `lmchat:composer:integrations:<chatId>` is preserved
  // via `localStorageKeyOverride` so existing selections survive.
  //
  // Wire shape unchanged: the chosen subset still ships per-message as
  // ``CanonicalChatRequest.integrations``.
  const [selectedIntegrations, setSelectedIntegrations] = useChatScopedState<
    string[]
  >(chatId, "integrations", "local", NO_SELECTED_INTEGRATIONS, isStringArray, {
    localStorageKeyOverride: integrationsStorageKey,
  });
  const { data: availableIntegrations = [] } = useIntegrationsList();

  // Two MCP systems map to two dispatch paths, and only one is live at a
  // time: "lmstudio" (LM Studio's own mcp.json servers, run server-side —
  // only reachable when the selected model IS an LM Studio model AND the
  // global endpoint mode is "native") vs "store" (LMChat's MCP Store,
  // client-side agentic loop — used in "openai_compat" mode for an LM
  // Studio model, or for ANY cloud model, always). No model selected is
  // treated as LM Studio, since this is a local-first app. Filtering the
  // composer's picker down to the active system keeps every visible toggle
  // meaningful instead of showing entries that no-op in the current mode.
  const { data: lmStudioConfig } = useLmStudioConfig();
  const modelIsLmStudio =
    selectedModel === undefined || selectedModel.provider === "lmstudio";
  const endpointMode = lmStudioConfig?.lm_studio_endpoint_mode ?? "native";
  const activeSystem: "lmstudio" | "store" =
    modelIsLmStudio && endpointMode === "native" ? "lmstudio" : "store";
  // Memoized so `visibleIntegrations` /
  // `effectiveIntegrations` have a stable identity across renders that don't
  // change their inputs, AND so `handleSubmit` (a useCallback keyed on
  // `effectiveIntegrations`, see below) reliably picks up a NEW value — and
  // gets re-created — whenever `activeSystem` flips (model/endpoint switch)
  // even if `selectedIntegrations` itself hasn't changed. Without this,
  // handleSubmit's dep array missed the activeSystem→visibleIntegrations
  // link and could ship a stale system's tools after a model switch.
  const visibleIntegrations = useMemo(
    () =>
      availableIntegrations.filter(
        (entry) => (entry.source ?? "lmstudio") === activeSystem,
      ),
    [availableIntegrations, activeSystem],
  );
  // T1-10: endpoint drives tools. The wire must carry ONLY tools that can run in
  // the ACTIVE system for this model/endpoint. `selectedIntegrations` is
  // chat-scoped and can retain a cross-system entry (e.g. a Store tool picked in
  // compat mode, then the model switched to a native LM Studio one) — shipping it
  // would put an `mcp/<slug>` on the native wire that LM Studio can't expand: a
  // silent no-op. Filter the selection down to what's runnable before sending.
  // The stored selection itself is left intact, so switching back restores it.
  const effectiveIntegrations = useMemo(
    () =>
      selectedIntegrations.filter((value) =>
        visibleIntegrations.some((entry) => entry.value === value),
      ),
    [selectedIntegrations, visibleIntegrations],
  );
  // Tools that exist but are hidden because they belong to the OTHER system —
  // surfaced as an explicit hint so the endpoint→tools relationship isn't silent.
  const hiddenOtherSystemCount =
    availableIntegrations.length - visibleIntegrations.length;

  // The integrations disclosure open-state must be
  // STATEFUL. A derived `open={...}` expression fights native <details>
  // toggles — the summary click flips `open`, then the next render snaps it
  // back to the derived value, so the picker is unreachable when collapsed.
  //
  // The default is keyed ONLY on `isMobile` — deterministically, NOT on the
  // async-loaded tool count. Keying on `availableIntegrations.length` was a
  // trap: at first render the list is empty (0 ≤ 4 → open), but a cached/fast
  // load delivering >4 tools on the first render would default CLOSED and hide
  // the picker behind the summary again — the original bug, relocated. So:
  //   desktop → always-open inline grid (pills wrap to a tidy row);
  //   mobile  → collapsed-by-default "N of M active" summary.
  const integrationsDefaultOpen = !isMobile;
  const [toolsOpen, setToolsOpen] = useState(() => integrationsDefaultOpen);
  // Re-seed the default only when the viewport crosses the mobile breakpoint
  // (the sole input to the default now). An unrelated re-render is a no-op and
  // does NOT clobber a user's manual toggle.
  const prevIntegrationsDefaultOpen = useRef(integrationsDefaultOpen);
  useEffect(() => {
    if (prevIntegrationsDefaultOpen.current !== integrationsDefaultOpen) {
      prevIntegrationsDefaultOpen.current = integrationsDefaultOpen;
      setToolsOpen(integrationsDefaultOpen);
    }
  }, [integrationsDefaultOpen]);

  // Seed selectedIntegrations from admin defaults once the list
  // arrives. Domain-specific by design (NOT the hook's job): runs only when
  // the hydrated selection is empty AND localStorage holds no entry for this
  // chat — a persisted explicit empty IS a real selection (user turned tools
  // off for this chat) and is honoured. Once seeded, the hook persists the
  // defaults, so the entry exists and this effect self-disarms.
  useEffect(() => {
    if (selectedIntegrations.length > 0) return;
    if (visibleIntegrations.length === 0) return;
    if (hasStoredIntegrations(chatId)) return;
    const defaults = visibleIntegrations
      .filter((entry) => entry.enabled_by_default === true)
      .map((entry) => entry.value);
    if (defaults.length > 0) {
      setSelectedIntegrations(defaults);
    }
  }, [chatId, visibleIntegrations, selectedIntegrations, setSelectedIntegrations]);

  // Clean up typing timer on unmount.
  useEffect(() => {
    return () => {
      if (typingTimerRef.current !== null) {
        clearTimeout(typingTimerRef.current);
      }
    };
  }, []);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const barRef = useRef<HTMLDivElement>(null);
  const typingTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { push } = useToast();
  const { data: prompts } = usePrompts();

  // Attachment state (image/text file staging) — extracted; see
  // useComposerAttachments for the handler bodies (verbatim).
  const {
    attachedImages,
    setAttachedImages,
    attachInputRef,
    attachAccept,
    handleAttachFiles,
  } = useComposerAttachments(isVision, push);

  // Per-chat active preset (sticky-mode lock-in).
  // - A preset slash command (``/code`` etc.) sets ``activePreset``; the next
  //   non-slash user message ships with the preset's system_prompt + temp.
  // - A plain (non-slash) user message clears the preset on submit, matching
  //   v0.5.x ``app.js:4345-4380`` ``meta._activePreset`` semantics.
  const { preset } = useChatPreset(chatId);

  // STT (transcript appended to composer text) — extracted; see
  // useComposerStt for the callback bodies (verbatim).
  const { sttCapability, sttState, sttAnnouncement, handleSttToggle } =
    useComposerStt(setText, textareaRef, push);

  // Compute slash query — text after the leading "/" on first line only.
  const slashQuery = (() => {
    if (!text.startsWith("/")) return "";
    const firstLine = text.split("\n")[0] ?? "";
    return firstLine.slice(1);
  })();

  // Reset selection index whenever the query changes or the menu closes.
  useEffect(() => {
    setSlashActiveIdx(0);
  }, [slashQuery, showSlash]);

function handleChange(e: ChangeEvent<HTMLTextAreaElement>): void {
    const val = e.target.value;
    setText(val);
    setShowSlash(val.startsWith("/") && !(val.split("\n")[0] ?? "").slice(1).includes(" "));
    // Auto-grow: reset then set to scrollHeight.
    const ta = textareaRef.current;
    if (ta !== null) {
      ta.style.height = "auto";
      ta.style.height = `${String(Math.min(ta.scrollHeight, textareaMaxPx))}px`;
    }
    // Typing data-attribute for atmospheric ::before flare.
    // Notify presence machine.
    presence?.onComposerTyping?.();
    const bar = barRef.current;
    if (bar !== null) {
      bar.setAttribute("data-typing", "1");
      if (typingTimerRef.current !== null) clearTimeout(typingTimerRef.current);
      typingTimerRef.current = setTimeout(() => {
        bar.removeAttribute("data-typing");
        typingTimerRef.current = null;
      }, TYPING_DEBOUNCE_MS);
    }
  }

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>): void {
    // When the slash menu is open, Arrow keys navigate, Tab/Enter select,
    // Escape closes — all BEFORE the normal submit/newline logic.
    if (showSlash) {
      const matches = filterCommands(slashQuery);
      if (e.key === "ArrowDown") {
        e.preventDefault();
        setSlashActiveIdx((i) =>
          matches.length === 0 ? 0 : (i + 1) % matches.length,
        );
        return;
      }
      if (e.key === "ArrowUp") {
        e.preventDefault();
        setSlashActiveIdx((i) =>
          matches.length === 0 ? 0 : (i - 1 + matches.length) % matches.length,
        );
        return;
      }
      if (
        e.key === "Tab" ||
        (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing)
      ) {
        e.preventDefault();
        const cmd = matches[slashActiveIdx];
        if (cmd !== undefined) handleSlashSelect(cmd);
        else setShowSlash(false);
        return;
      }
      if (e.key === "Escape") {
        e.preventDefault();
        setShowSlash(false);
        return;
      }
    }

    // Enter sends; Shift+Enter inserts a newline. Cmd/Ctrl+Enter also sends
    // (power-user muscle memory). The isComposing guard prevents submitting
    // mid-IME-composition (CJK/accent input), where Enter commits the
    // candidate rather than the message.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSubmit();
      return;
    }
    // Cmd/Ctrl+Shift+M — toggle STT.
    if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key === "M") {
      e.preventDefault();
      handleSttToggle();
      return;
    }
  }

  function dispatchSlashCommand(name: string, args: string): void {
    // Preset-mode slash commands ("/code", "/research", …) launch a
    // transient sub-agent session (clean context, chainable).  They do NOT
    // write the chat's active_preset — the rail picker is the sole writer of
    // the persistent system prompt.
    const presetMatch = PRESET_BY_SLASH_CMD[name];
    if (presetMatch !== undefined) {
      // Notify Chat.tsx to open the sub-session panel for this preset. The
      // freshly-created SubSessionState comes back synchronously — kept so
      // the inline-form branch below can forward it straight into onSubmit
      // instead of waiting for the parent's setSubSession to commit.
      const startedSubSession = onPresetActivate?.(presetMatch.id) ?? null;

      const trimmedArgs = args.trim();
      if (trimmedArgs === "") {
        // No inline message — keep the composer empty, focus it, wait for
        // the next plain message.
        setTimeout(() => {
          textareaRef.current?.focus();
        }, 0);
        return;
      }

      // Inline-form: `/research what's quantum chromodynamics` should
      // activate research mode AND send the message in one keystroke. Build
      // the payload directly and call onSubmit synchronously; we skip the
      // composer's state round-trip to avoid a stale-closure read of `text`
      // inside handleSubmit. Passing `startedSubSession` explicitly means
      // Chat.tsx's sub-session router doesn't need to wait for a re-render
      // (or read a ref) to see the session that was just started above.
      if (modelId === undefined || modelId === "") {
        // Chat.tsx's sub-session branch will toast "Select a model…"
        // on its own; mirror the bail so the inline form behaves the
        // same as the two-step path.
        return;
      }
      const payload: ChatStreamPayload = {
        input: [{ type: "text", content: trimmedArgs }],
        model: modelId,
        ...((selectedIntegrations.length > 0 || hasStoredIntegrations(chatId)) && {
          integrations: effectiveIntegrations,
        }),
      };
      onSubmit(chatId, payload, trimmedArgs, presetMatch.label, startedSubSession);
      return;
    }

    switch (name) {
      case "help":
        push({
          variant: "info",
          message: BUILTIN_COMMANDS.map(
            (c) => `/${c.name}: ${c.description}`,
          ).join("\n"),
          duration: 8_000,
        });
        break;
      case "clear":
        onClear();
        break;
      case "memory":
        if (args.trim() === "") {
          push({ variant: "warning", message: "Usage: /memory <text to pin>" });
        } else {
          onMemoryPin(args.trim());
        }
        break;
      case "compact":
        onCompact();
        break;
      case "fork":
        onFork();
        break;
      case "compare": {
        // Guard: need at least 2 distinct models available.
        const allModels = modelData?.models ?? [];
        if (allModels.length < 2) {
          push({
            variant: "warning",
            message:
              "Load a second model in LM Studio to compare.",
          });
          break;
        }
        // Default Model A to the chat's current model; Model B to empty
        // (user picks).
        setCompareModelA(modelId ?? allModels[0]?.id ?? "");
        setCompareModelB("");
        setShowComparePicker(true);
        break;
      }
      case "panel":
        push({
          variant: "info",
          message:
            "/panel isn't in this build yet — multi-model convergence is coming.",
        });
        break;
      case "prompt": {
        // /prompt <name> — insert the matching prompt's content into the composer.
        if (args.trim() === "") {
          push({ variant: "warning", message: "Usage: /prompt <name>" });
        } else {
          const found = (prompts ?? []).find(
            (p) => p.name.toLowerCase() === args.trim().toLowerCase(),
          );
          if (found) {
            setText(found.content);
            setTimeout(() => {
              textareaRef.current?.focus();
            }, 0);
          } else {
            push({
              variant: "warning",
              message: `No prompt named "${args.trim()}" found.`,
            });
          }
        }
        break;
      }
      default:
        push({ variant: "warning", message: `Unknown command: /${name}` });
        break;
    }
  }

  // dispatchSlashCommand is a plain function (re-created every render, and
  // several of the props it closes over — onFork/onCompact/onMemoryPin from
  // Chat.tsx — are themselves inline arrows, so it can never be made
  // referentially stable via useCallback without changing Chat.tsx's prop
  // wiring too). handleSubmit doesn't need dispatchSlashCommand's identity
  // to be stable — it just needs to call the CURRENT one — so it's read
  // through a ref updated every render instead of listed as a dependency.
  const dispatchSlashCommandRef = useRef(dispatchSlashCommand);
  dispatchSlashCommandRef.current = dispatchSlashCommand;

  const handleSubmit = useCallback((): void => {
    const trimmed = text.trim();
    // When no model is selected, slash commands can't
    // dispatch — surface a toast but keep the typed text so the user
    // doesn't have to re-type the command after selecting a model.
    if (trimmed !== "" && !streaming && (modelId === undefined || modelId === "")) {
      const parsed = parseSlashCommand(trimmed);
      if (parsed !== null) {
        push({
          variant: "warning",
          message: `Select a model before using /${parsed.name}`,
        });
        // Do not clear the text — the user can pick a model and retry.
        return;
      }
    }

    // modelId undefined/empty means no model selected; block submission to
    // prevent a 422 from CanonicalChatRequest.model (required field).
    // This guard mirrors the canSubmit check so Cmd+Enter also respects it.
    if (trimmed === "" || streaming || modelId === undefined || modelId === "")
      return;

    // Slash command dispatch.
    const parsed = parseSlashCommand(trimmed);
    if (parsed !== null) {
      setText("");
      setShowSlash(false);
      dispatchSlashCommandRef.current(parsed.name, parsed.args);
      return;
    }

    setText("");
    setShowSlash(false);
    // Reset textarea height.
    if (textareaRef.current !== null) {
      textareaRef.current.style.height = "auto";
    }

    // When a preset is active (set deliberately via the rail picker),
    // inject its system_prompt + auto-temperature onto every CanonicalChatRequest.
    // The preset is persistent — it stays until the user changes it via the
    // rail picker ("Default / none").  No transient clear on send.
    const presetSnapshot = preset;
    // The follow-up-suggestions directive is injected server-side
    // (streaming_service, gated by LM_CHAT_FOLLOWUPS_ENABLED) so it never
    // leaks into the client payload. The composer only carries the preset's
    // system_prompt + auto-temperature when a preset is active.
    // Compose the system_prompt: preset's base + custom instructions for
    // this chat (the amendment, saved via ChatSettingsRail). If only one
    // layer is set, that's what gets sent. If both, they're concatenated
    // with a blank line and a header that makes the boundary obvious to
    // the model.
    const trimmedAmendment = customInstructions.trim();
    const baseSystem =
      presetSnapshot !== null ? presetSnapshot.system_prompt : "";
    const composedSystem =
      baseSystem !== "" && trimmedAmendment !== ""
        ? `${baseSystem}\n\n## Custom instructions for this chat\n\n${trimmedAmendment}`
        : baseSystem !== ""
          ? baseSystem
          : trimmedAmendment;

    // Attachments. Images go down the
    // {type:"image", data_url} path (useSSE.ts:95 accepts it). TEXT files
    // (e.g. .txt) MUST NOT — the backend validates the image data_url and
    // rejects a `data:text/plain;base64,…` as invalid image data. So a text
    // file's decoded contents fold into the message text instead.
    const { imageDataUrls, textSegments } = partitionAttachments(attachedImages);
    const textContent =
      textSegments.length > 0
        ? [trimmed, ...textSegments].filter((s) => s !== "").join("\n\n")
        : trimmed;
    const inputItems: ChatStreamPayload["input"] = [
      { type: "text", content: textContent },
      ...imageDataUrls.map((data_url) => ({
        type: "image" as const,
        data_url,
      })),
    ];

    const payload: ChatStreamPayload = {
      input: inputItems,
      model: modelId,
      // Include integrations when the user has made an explicit
      // choice for this chat (stored entry exists) OR has items selected.
      // Omit only when truly untouched (no localStorage entry, nothing selected)
      // so the backend can distinguish "user hasn't touched tools yet" (None →
      // apply admin defaults) from "user explicitly chose [] / [...]".
      ...((selectedIntegrations.length > 0 || hasStoredIntegrations(chatId)) && {
        integrations: effectiveIntegrations,
      }),
      // Preset-driven system_prompt + auto-temperature.
      // Per-chat custom instructions (chat.settings.system_prompt) are
      // appended above so the user's amendment reaches the model whether
      // or not a preset is active.
      ...(composedSystem !== "" && {
        system_prompt: composedSystem,
      }),
      ...(presetSnapshot?.temperature != null && {
        temperature: presetSnapshot.temperature,
      }),
    };

    // active_preset is persistent — it stays until the user changes it
    // via the rail picker.  No auto-clear here.
    // Clear attachments after submit.
    setAttachedImages([]);

    // Pass the preset label (if any) so Chat.tsx can render the
    // subchat-frame divider.
    onSubmit(chatId, payload, trimmed, presetSnapshot?.label);
  }, [
    text,
    streaming,
    chatId,
    modelId,
    onSubmit,
    preset,
    selectedIntegrations,
    effectiveIntegrations,
    attachedImages,
    setAttachedImages,
    customInstructions,
    push,
  ]);

  function handleSlashSelect(cmd: SlashCommand): void {
    setShowSlash(false);
    if (cmd.comingSoon === true) {
      setText("");
      textareaRef.current?.focus();
      return;
    }
    // Commands that require user-typed args: fill in the prefix so they can
    // continue typing. All others dispatch immediately on Tab/Enter.
    if (cmd.name === "memory" || cmd.name === "prompt") {
      setText(`/${cmd.name} `);
      textareaRef.current?.focus();
      return;
    }
    // Dispatch immediately — no need to press Enter a second time.
    dispatchSlashCommand(cmd.name, "");
    setText("");
    textareaRef.current?.focus();
  }

  // modelId undefined/empty means no model selected — block submission to
  // prevent a 422 from CanonicalChatRequest.model (required field).
  const canSubmit =
    text.trim() !== "" && !streaming && modelId !== undefined && modelId !== "";

  return (
    <div className="lmchat-composer-wrapper">
      {/* ARIA live region — announces transcript injection to screen readers.
          Shows "Listening for speech…" while active, then the injected
          transcript snippet (≤60 chars) when the state transitions to idle. */}
      <div aria-live="polite" aria-atomic="true" style={srOnlyStyle}>
        {sttState.listening ? "Listening for speech…" : sttAnnouncement}
      </div>

      {/* Meta row: in-project badge + RagModeBadge share ONE horizontal
          line with the integrations disclosure summary. flex-wrap:wrap
          lets the tools bar break to its own row when open. */}
      <div className="lmchat-composer-meta-row">
        {/* In-project badge. Renders nothing when the chat is un-projected
            so the existing visual UX is preserved verbatim for legacy
            chats. RagModeBadge sits beside it showing the resolver's
            INLINE/HYBRID/FOCUSED pick. */}
        <div
          style={{
            display: "contents",
          }}
        >
          <InProjectChip chatId={chatId} />
          <RagModeBadge chatId={chatId} />
        </div>

        {/* Integrations picker — collapsible on mobile (≤768px) and
            on desktop when there are more than 4 integrations (clutter threshold).
            Collapses to a single "N tools active" summary line.
            Also gate on capabilities.trained_for_tool_use. Non-tool-trained
            models don't show
            the Tools panel — it would be meaningless and confusing. When model
            capabilities are unknown (null) we err on the side of showing the panel
            (backward-compat). */}
        {visibleIntegrations.length > 0 &&
        (modelCapabilities === null ||
          modelCapabilities.trained_for_tool_use) ? (
          <details
            className="lmchat-integrations-disclosure"
            data-testid="integrations-disclosure"
            open={toolsOpen}
            onToggle={(e) => {
              setToolsOpen(e.currentTarget.open);
            }}
          >
          <summary className="lmchat-integrations-summary">
            {/* Numerator is effectiveIntegrations
                (what actually ships), not selectedIntegrations — a
                cross-system entry can inflate selectedIntegrations beyond
                the visible/runnable count. */}
            {selectedIntegrations.length > 0
              ? `${String(effectiveIntegrations.length)} of ${String(visibleIntegrations.length)} active`
              : `Tools (${String(visibleIntegrations.length)})`}
          </summary>
          <div
            className="lmchat-integrations-bar"
            aria-label="MCP integrations"
            role="group"
          >
            {visibleIntegrations.map((entry) => {
              const isOn = selectedIntegrations.includes(entry.value);
              return (
                <button
                  key={entry.value}
                  type="button"
                  role="checkbox"
                  aria-checked={isOn}
                  onClick={() => {
                    setSelectedIntegrations((prev) =>
                      isOn
                        ? prev.filter((v) => v !== entry.value)
                        : [...prev, entry.value],
                    );
                  }}
                  disabled={streaming}
                  className={`lmchat-integration-pill ${isOn ? "lmchat-integration-pill--on" : "lmchat-integration-pill--off"}`}
                  data-testid={`integration-pill-${entry.value}`}
                >
                  {entry.value.startsWith("mcp/")
                    ? entry.value.slice(4)
                    : entry.value}
                </button>
              );
            })}
          </div>
          </details>
        ) : // No integrations configured — the bar stays empty (no noise).
        null}
        {/* T1-10: endpoint drives tools. When tools exist but belong to the
            OTHER system (hidden for this model/endpoint), say so explicitly
            instead of silently omitting them — including the case where the
            visible bar above is empty because ALL tools live in the other mode. */}
        {hiddenOtherSystemCount > 0 &&
        (modelCapabilities === null ||
          modelCapabilities.trained_for_tool_use) ? (
          <p
            className="lmchat-integrations-other-hint"
            data-testid="integrations-other-system-hint"
          >
            {activeSystem === "lmstudio"
              ? `${String(hiddenOtherSystemCount)} Store tool${hiddenOtherSystemCount === 1 ? "" : "s"} — switch to OpenAI-compat mode to use`
              : `${String(hiddenOtherSystemCount)} LM Studio tool${hiddenOtherSystemCount === 1 ? "" : "s"} — switch to Native mode to use`}
          </p>
        ) : null}

        {/* Context window meter — lives in the meta row, pushed to the right
            edge of the input column (margin-left:auto) so it reads inline with
            the RAG-method badge and the input instead of floating off to the
            page edge. Shows approximate token usage vs the model's LOADED
            context length; warns (amber) at ≥80%. Token estimate uses chars÷4
            and includes historyTokens so it reflects total context usage. */}
        {modelMaxContextLength > 0 && (() => {
          const draftTokens = Math.ceil(text.length / 4) + attachedImages.length * 512;
          const estimatedTokens = historyTokens + draftTokens;
          const pct = estimatedTokens / modelMaxContextLength;
          const warn = pct >= 0.8;
          return (
            <div
              className="lmchat-ctx-meter"
              aria-label={`Context usage: ~${String(estimatedTokens)} of ${String(modelMaxContextLength)} tokens`}
              style={{
                fontSize: "var(--fs-micro)",
                color: warn ? "var(--color-warning)" : "var(--color-text-muted)",
                marginLeft: "auto",
              }}
              data-testid="context-meter"
              data-warn={warn ? "true" : "false"}
            >
              ~{estimatedTokens.toLocaleString()} / {modelMaxContextLength.toLocaleString()}
              {warn && (
                <span
                  style={{ marginLeft: "var(--space-glue)", display: "inline-flex", verticalAlign: "text-bottom" }}
                  role="img"
                  aria-label="approaching context limit"
                >
                  <TriangleAlert size={12} aria-hidden="true" />
                </span>
              )}
            </div>
          );
        })()}
      </div>

      {/* Context blocks — chips for staged sub-session summaries or other
          context injected before the next send. Each chip shows the label
          and an ✕ to dismiss. On submit they are consumed by Chat.tsx
          (prepended to system_prompt) and then cleared. */}
      {contextBlocks !== undefined && contextBlocks.length > 0 && (
        <div
          className="lmchat-context-chips"
          role="list"
          aria-label="Staged context"
        >
          {contextBlocks.map((block) => (
            <div key={block.id} className="lmchat-context-chip" role="listitem">
              <span className="lmchat-context-chip__label">{block.label}</span>
              <button
                type="button"
                className="lmchat-context-chip__remove"
                aria-label={`Remove context: ${block.label}`}
                onClick={() => {
                  onRemoveContextBlock?.(block.id);
                }}
              >
                <X size={12} aria-hidden="true" />
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Attached image preview chips. */}
      {attachedImages.length > 0 && (
        <div
          className="lmchat-attach-chips"
          role="list"
          aria-label="Attached files"
        >
          {attachedImages.map((img) => (
            <div
              key={img.id}
              role="listitem"
              style={{
                display: "flex",
                alignItems: "center",
                gap: "4px",
                background: "color-mix(in oklch, var(--color-surface) 85%, var(--color-accent))",
                border: "1px solid var(--color-border)",
                borderRadius: "var(--radius-sm, 4px)",
                padding: "2px 6px",
                fontSize: "var(--fs-caption)",
              }}
              data-testid="attach-chip"
            >
              <span style={{ maxWidth: "120px", overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {img.name}
              </span>
              <button
                type="button"
                aria-label={`Remove attachment: ${img.name}`}
                onClick={() => { setAttachedImages((prev) => prev.filter((i) => i.id !== img.id)); }}
                style={{ background: "none", border: "none", cursor: "pointer", padding: 0, lineHeight: 1, display: "inline-flex", alignItems: "center" }}
                data-testid="attach-chip-remove"
              >
                <X size={12} aria-hidden="true" />
              </button>
            </div>
          ))}
        </div>
      )}

      {/* Slash command menu — relative container scoped to the same max-width
          as the bar so the menu never exceeds the input column. */}
      <div className="lmchat-composer-inner">
        {showSlash && (
          <SlashMenu
            query={slashQuery}
            activeIdx={slashActiveIdx}
            onHighlight={setSlashActiveIdx}
            onSelect={handleSlashSelect}
            onClose={() => {
              setShowSlash(false);
            }}
          />
        )}

        {/* /compare picker — lightweight popover anchored above the bar.
            Two model selects + confirm/cancel. No full-screen modal. */}
        {showComparePicker && (
          <ComparePicker
            modelOptions={(modelData?.models ?? []).map((m) => ({
              id: m.id,
              label: m.name,
              loaded: m.loaded,
            }))}
            modelA={compareModelA}
            modelB={compareModelB}
            onChangeA={setCompareModelA}
            onChangeB={setCompareModelB}
            onCancel={() => { setShowComparePicker(false); }}
            onConfirm={(modelA, modelB) => {
              onABCompareStart?.(modelA, modelB);
              setShowComparePicker(false);
              setText("");
              textareaRef.current?.focus();
            }}
          />
        )}

        <div ref={barRef} className="lmchat-composer-bar">
          {/* Textarea */}
          <textarea
            ref={textareaRef}
            value={text}
            onChange={handleChange}
            onKeyDown={handleKeyDown}
            onFocus={() => {
              presence?.onComposerFocused?.();
            }}
            onBlur={() => {
              presence?.onComposerBlurred?.();
            }}
            disabled={streaming}
            // Placeholder no longer dumps 3 keyboard
            // hints (WCAG 2.5: placeholders aren't the place for
            // instructions). The Cmd+K palette + the `?` keyboard-help
            // surface carry the discoverability load instead.
            placeholder="Message…"
            rows={1}
            aria-label="Message"
            className="lmchat-composer-textarea"
            style={{ maxHeight: `${String(textareaMaxPx)}px` }}
          />

          <div className="lmchat-composer-actions">
            {/* Attach button. Always shown — text-file attachments never
                need vision (decoded + folded into the message text, see
                attachments.ts). Only IMAGE uploads require
                capabilities.vision === true; handleAttachFiles enforces that
                gate (the accept filter below is a UX hint only — it doesn't
                stop drag/paste or a model switch after staging). */}
            <>
              <input
                ref={attachInputRef}
                type="file"
                accept={attachAccept}
                multiple
                style={{ display: "none" }}
                aria-hidden="true"
                onChange={(e) => { handleAttachFiles(e.target.files); }}
                data-testid="attach-file-input"
              />
              <button
                type="button"
                onClick={() => { attachInputRef.current?.click(); }}
                aria-label="Attach file"
                title={isVision ? "Attach image or text file" : "Attach text file"}
                disabled={streaming}
                className="lmchat-composer-attach-btn"
                data-testid="attach-button"
              >
                <Paperclip size={16} aria-hidden />
              </button>
            </>

            {/* STT mic button */}
            <MicButton
              capability={sttCapability}
              state={sttState}
              onToggle={handleSttToggle}
              disabled={streaming}
            />

            {/* Stop / Send */}
            {streaming ? (
              <button
                type="button"
                onClick={onStop}
                aria-label="Stop generation"
                title="Stop generation"
                className="lmchat-composer-stop-btn"
              >
                <Square size={16} fill="currentColor" aria-hidden />
              </button>
            ) : (
              <button
                type="button"
                onClick={handleSubmit}
                disabled={!canSubmit}
                aria-label="Send message"
                title="Send message"
                className="lmchat-composer-send-btn"
              >
                <SendHorizontal size={16} aria-hidden /> Send
              </button>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ─── Styles ──────────────────────────────────────────────────────────────────
// Visual styles moved to web/src/styles/composer.css.
// Only JS-driven dynamic values remain as inline styles.

// Visually hidden but accessible to screen readers.
const srOnlyStyle: CSSProperties = {
  position: "absolute",
  width: "1px",
  height: "1px",
  padding: 0,
  margin: "-1px",
  overflow: "hidden",
  clip: "rect(0, 0, 0, 0)",
  whiteSpace: "nowrap",
  borderWidth: 0,
};
