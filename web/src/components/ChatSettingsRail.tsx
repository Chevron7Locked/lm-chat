/* SPDX-License-Identifier: Apache-2.0 */
/**
 * ChatSettingsRail — per-chat settings surface mounted in the Chat right rail.
 *
 * Contents:
 *  - Preset selector.
 *  - System prompt textarea with preset-match highlight.
 *  - Basic params: temperature.
 *  - Advanced sampler expander: top_p, top_k, min_p, repeat_penalty,
 *    max_tokens, repeat-loop cut threshold (K), reasoning effort.
 *  - Quality toggles: self-consistency, chain-of-verification, stateless flag.
 *
 * Persistence flows through the :func:`useUpdateChat` mutation
 * (PATCH /api/chats/:id, form-encoded).  Each field debounces / saves on
 * blur to avoid one-PATCH-per-keystroke.
 *
 * Per-chat scoped: the rail mounts only when ``chatId !== null``.  Unlike a
 * slideout that replaces the chat layout, this rail renders as a right-aside
 * without unmounting the message list.
 *
 * Behavior:
 *  - Optional Drawer wrap (``isOpen`` / ``onClose``) — when both are passed
 *    the rail body renders inside the shared Drawer primitive; otherwise it
 *    renders inline (legacy docked aside).
 *  - Empty-state copy when ``chatId === null`` so the surface is never an
 *    unexplained blank panel.
 *  - "Saved" pulse on successful PATCH.
 *  - The Advanced expander is now labelled "Advanced sampler" with a
 *    tooltip listing the fields it contains, and the Reasoning effort
 *    select carries a value tooltip.
 */
import { useEffect, useState, type ReactNode } from "react";
import { Check, Info } from "lucide-react";
import { useChatsDirect, useUpdateChat } from "@/hooks/useChats";
import type { ChatSettings as ChatSettingsT } from "@/hooks/useChats";
import { useChatPreset } from "@/hooks/useChatPreset";
import { useProject } from "@/hooks/useProjects";
import { useModels } from "@/hooks/useModels";
import { PRESET_LIST, PRESETS, DEFAULT_PRESET_ID, RAW_PRESET_ID } from "@/lib/presets";
import { Drawer } from "@/components/ui/Drawer";
import { useToast } from "@/stores/toastStore";
import { ReasoningToggle } from "@/components/ReasoningToggle";
import { useChatSettingsStore } from "@/stores/chatSettingsStore";
import type { ReasoningLevel } from "@/stores/chatSettingsStore";
import {
  REASONING_LEVELS,
  deriveChatReasoningOverride,
} from "@/hooks/useReasoningOverridePersistence";

// ─── Preset selector entries (wired to useChatPreset) ───────────────────────

// General is the sole default. A chat with no explicit
// preset choice resolves to "general" (real system prompt + date/baseline).
// The raw escape hatch ("None · raw model") is placed LAST and visually
// separated so it never reads as the default.  Selecting it sends no
// system_prompt.  The sentinel id "none" is handled by the Composer's
// existing null-preset → empty-system_prompt path (getPreset("none") = null).
const PRESET_OPTIONS: { id: string; label: string }[] = [
  ...PRESET_LIST.map((p) => ({
    id: p.id,
    label: p.id === "general" ? "General · friendly default" : p.label,
  })),
  // RAW escape hatch — always last, clearly not the default.
  { id: RAW_PRESET_ID, label: "None · raw model" },
];

const REASONING_TOOLTIP =
  "Per-chat override for the model's chain-of-thought budget. " +
  "off = no reasoning tokens; low/medium/high allocate progressively more " +
  "thinking before the final answer. Leave blank to defer to the global default.";

const ADVANCED_TOOLTIP =
  "Advanced sampler — top_p, top_k, min_p, repeat penalty, max tokens, " +
  "repeat-loop cut threshold, and reasoning effort. Leave a field blank " +
  "to defer to the model preset (or the global default for the loop cut).";

// Time the "Saved" indicator stays visible after a successful PATCH.
const SAVED_PULSE_MS = 2000;

// ─── Component ──────────────────────────────────────────────────────────────

export interface ChatSettingsRailProps {
  /**
   * PK of the chat whose settings are being edited.  ``null`` renders the
   * "no chat selected" empty state.
   */
  chatId: number | null;
  /**
   * Optional Drawer wrap.  Pass both ``isOpen`` and ``onClose`` to render
   * the rail as a sliding overlay; omit either to render inline (legacy
   * docked-aside posture used by Chat.tsx's RightPanel).
   */
  isOpen?: boolean;
  onClose?: () => void;
}

export function ChatSettingsRail({
  chatId,
  isOpen,
  onClose,
}: ChatSettingsRailProps) {
  const useDrawer = isOpen !== undefined && onClose !== undefined;
  const body =
    chatId === null ? <EmptyState /> : <ChatSettingsRailBody chatId={chatId} />;

  if (!useDrawer) {
    return (
      <div
        className="lmchat-settings-rail"
        aria-label="Chat settings"
        data-testid="chat-settings-rail"
      >
        {body}
      </div>
    );
  }

  return (
    <Drawer
      isOpen={isOpen}
      onClose={onClose}
      side="right"
      title="Chat settings"
      width={380}
      data-testid="chat-settings-drawer"
    >
      <div
        className="lmchat-settings-rail--drawer"
        aria-label="Chat settings"
        data-testid="chat-settings-rail"
      >
        {body}
      </div>
    </Drawer>
  );
}

// ─── Body (the actual settings form) ────────────────────────────────────────

function ChatSettingsRailBody({ chatId }: { chatId: number }) {
  const { data: chats } = useChatsDirect();
  const updateChat = useUpdateChat(chatId);
  const { push } = useToast();
  const chat = chats?.find((c) => c.id === chatId);
  const settings: ChatSettingsT = chat?.settings ?? {};
  const projectId = chat?.project_id ?? null;
  const { data: project } = useProject(projectId);

  // Resolve capabilities for the chat's current model so the reasoning
  // section can be gated on capabilities.reasoning !== null.
  const { data: modelData } = useModels();
  const currentModelId = chat?.model_id ?? null;
  const currentModel = currentModelId
    ? (modelData?.models.find((m) => m.id === currentModelId) ??
       // also check loaded_instance_ids for multi-instance matches
       modelData?.models.find((m) =>
         m.loaded_instance_ids.includes(currentModelId),
       ))
    : undefined;
  const modelCapabilities = currentModel?.capabilities ?? null;
  const projectSystemPrompt =
    project?.system_prompt && project.system_prompt.trim() !== ""
      ? project.system_prompt
      : null;

  // Per-chat preset state is sourced from useChatPreset so the
  // rail selector and the Composer badge share a single source of truth.
  // Selecting a preset here updates the Composer's lock-in state instantly
  // (and vice versa).
  const { activePreset, setPreset } = useChatPreset(chatId);

  // ─── Local form state ────────────────────────────────────────────────────
  // We mirror the persisted values into local state so the inputs stay
  // controlled across re-renders while the user types.  Each input saves on
  // blur (text/number) or change (toggles/select) to keep PATCH volume sane.
  const [systemPrompt, setSystemPrompt] = useState<string>(
    settings.system_prompt ?? "",
  );
  const [temperature, setTemperature] = useState<string>(
    stringOrEmpty(settings.temperature),
  );
  const [topP, setTopP] = useState<string>(stringOrEmpty(settings.top_p));
  const [topK, setTopK] = useState<string>(stringOrEmpty(settings.top_k));
  const [minP, setMinP] = useState<string>(stringOrEmpty(settings.min_p));
  const [repeatPenalty, setRepeatPenalty] = useState<string>(
    stringOrEmpty(settings.repeat_penalty),
  );
  const [maxTokens, setMaxTokens] = useState<string>(
    stringOrEmpty(settings.max_tokens),
  );
  const [repeatWarningCutK, setRepeatWarningCutK] = useState<string>(
    stringOrEmpty(settings.repeat_warning_cut_k),
  );
  // The canonical key is reasoning_effort; `settings.reasoning` is a legacy
  // alias. The canonical-key/legacy-alias parsing lives in ONE place
  // (deriveChatReasoningOverride, shared with ReasoningToggle) instead of
  // being duplicated inline here and below.
  const [reasoning, setReasoning] = useState<ReasoningLevel | "">(
    deriveChatReasoningOverride(settings),
  );

  // Toggle→select direction sync.
  // ReasoningToggle writes chatOverrides[chatId] in Zustand immediately
  // (via setChatReasoning). A handler in Chat.tsx then fires the PATCH
  // mutation so the value persists. However the rail's `reasoning` local
  // state only re-syncs via the settingsHash useEffect after the server
  // round-trip completes. This useEffect closes the gap: whenever the toggle
  // updates chatOverrides[chatId] we sync the local select value immediately,
  // so both controls show the same level without waiting for a round-trip.
  // Toggle and select are two controls for the same single canonical
  // reasoning_effort key.
  const chatOverrides = useChatSettingsStore((s) => s.chatOverrides);
  useEffect(() => {
    const override = chatOverrides[chatId];
    if (override === undefined) return; // no override set in this session — leave server value
    setReasoning(override);
  }, [chatId, chatOverrides]);

  const [advancedOpen, setAdvancedOpen] = useState<boolean>(false);

  // Re-sync local state when the chat row's settings change.
  // Keying only on ``chatId`` would miss updates when the preset store or
  // any external write (such as a preset slash command) mutated the settings
  // JSON outside the rail. We stringify the
  // settings object to derive a content hash so the deps array stays
  // primitive and React's referential-equality check fires only on real
  // content changes.
  const settingsHash = JSON.stringify(settings);
  useEffect(() => {
    // active_preset is sourced from useChatPreset — no local sync needed
    // here; the store stays the single source of truth.
    setSystemPrompt(settings.system_prompt ?? "");
    setTemperature(stringOrEmpty(settings.temperature));
    setTopP(stringOrEmpty(settings.top_p));
    setTopK(stringOrEmpty(settings.top_k));
    setMinP(stringOrEmpty(settings.min_p));
    setRepeatPenalty(stringOrEmpty(settings.repeat_penalty));
    setMaxTokens(stringOrEmpty(settings.max_tokens));
    setRepeatWarningCutK(stringOrEmpty(settings.repeat_warning_cut_k));
    setReasoning(deriveChatReasoningOverride(settings));
    // settingsHash is a content digest of `settings`; chatId guarantees we
    // also resync on chat-switch even if the new chat happens to have an
    // identical settings shape.
  }, [chatId, settingsHash]);

  // ─── Saved-pulse indicator ──────────────────────────────────────────────
  // The mutation's success state stays true until the next call.  We want
  // a transient "Saved" badge that auto-fades.  We watch the mutation's
  // submittedAt timestamp + isSuccess flag, then flip a local boolean.
  const [savedAt, setSavedAt] = useState<number | null>(null);
  const mutationSubmittedAt = updateChat.submittedAt;
  const mutationSuccess = updateChat.isSuccess;
  useEffect(() => {
    if (!mutationSuccess || mutationSubmittedAt === 0) return;
    setSavedAt(mutationSubmittedAt);
    const t = window.setTimeout(() => {
      setSavedAt(null);
    }, SAVED_PULSE_MS);
    return () => {
      window.clearTimeout(t);
    };
  }, [mutationSuccess, mutationSubmittedAt]);
  const savedVisible = savedAt !== null;

  // ─── Persist helpers ─────────────────────────────────────────────────────

  /** Build a standard onError handler for a named settings field. */
  const makeOnError = (fieldLabel: string) =>
    (err: unknown): void => {
      const detail =
        (err as { detail?: unknown }).detail ??
        (err instanceof Error ? err.message : String(err));
      const suffix = typeof detail === "string" && detail.length > 0
        ? ` — ${detail}`
        : "";
      push({
        variant: "error",
        message: `${fieldLabel} couldn't be saved${suffix}`,
      });
    };

  // ─── Persist handlers ────────────────────────────────────────────────────

  const persistNumber = (
    key:
      | "temperature"
      | "top_p"
      | "top_k"
      | "min_p"
      | "repeat_penalty"
      | "max_tokens",
    raw: string,
  ): void => {
    // Empty string explicitly clears the override by sending
    // ``key=null``. Returning early here would silently drop the user's
    // intent to clear the field; the rule is to send the explicit clear.
    // The route's Form(default=None) param + the
    // chat_service merge accept None to mean "clear".
    const FIELD_LABELS: Record<typeof key, string> = {
      temperature: "Temperature",
      top_p: "top_p",
      top_k: "top_k",
      min_p: "min_p",
      repeat_penalty: "Repeat penalty",
      max_tokens: "Max tokens",
    };
    const onError = makeOnError(FIELD_LABELS[key]);
    if (raw.trim() === "") {
      updateChat.mutate({ [key]: null }, { onError });
      return;
    }
    const num = Number(raw);
    if (!Number.isFinite(num)) return;
    updateChat.mutate({ [key]: num }, { onError });
  };

  const persistString = (key: "system_prompt", raw: string): void => {
    updateChat.mutate({ [key]: raw }, { onError: makeOnError("System prompt") });
  };

  /**
   * Persist the per-chat repeat-loop cut threshold (K). Unlike
   * persistNumber (which clears via `{[key]: null}`, a shape the numeric
   * rail fields' BE Form params can't cleanly accept as a clear signal),
   * this field's BE param is string-typed specifically so an explicit ""
   * travels as a real clear — mirrors reasoning_effort's clear-to-inherit
   * handling instead of the other numeric rail fields.
   */
  const persistRepeatWarningCutK = (raw: string): void => {
    const onError = makeOnError("Repeat-loop cut");
    const trimmed = raw.trim();
    if (trimmed === "") {
      updateChat.mutate({ repeat_warning_cut_k: "" }, { onError });
      return;
    }
    const num = Number(trimmed);
    if (!Number.isFinite(num)) return;
    const clamped = Math.max(0, Math.min(100, Math.trunc(num)));
    updateChat.mutate({ repeat_warning_cut_k: String(clamped) }, { onError });
  };

  const persistBool = (
    key:
      | "self_consistency_enabled"
      | "chain_of_verification_enabled"
      | "stateless",
    next: boolean,
  ): void => {
    const FIELD_LABELS: Record<typeof key, string> = {
      self_consistency_enabled: "Self-consistency",
      chain_of_verification_enabled: "Chain-of-verification",
      stateless: "Stateless turn",
    };
    updateChat.mutate({ [key]: next }, { onError: makeOnError(FIELD_LABELS[key]) });
  };

  const persistReasoning = (next: ReasoningLevel | ""): void => {
    // Write the canonical `reasoning_effort` key instead of the legacy
    // `reasoning` alias. The BE PATCH endpoint accepts both; the canonical
    // key removes the dual-field ambiguity. Empty string clears the per-chat
    // override; the ChatSettings field_validator coerces "" → null
    // server-side. Surface a sticky toast on failure so the user knows the
    // change didn't persist (makeOnError pattern).
    updateChat.mutate(
      { reasoning_effort: next },
      { onError: makeOnError("Reasoning effort") },
    );
    // Sync the zustand chatSettingsStore so ReasoningToggle (which reads
    // chatOverrides) stays in sync when the select changes. Without this the
    // select→toggle direction is broken: chatOverrides would retain the last
    // value set by the toggle, causing the toggle to show a stale level after
    // the select changed. Toggle and select are two controls for the single
    // canonical reasoning_effort key.
    useChatSettingsStore.getState().setChatReasoning(chatId, next);
  };

  // Preset-match highlight. We highlight the textarea when either
  //   (a) a preset is currently active (locked in by a slash command), or
  //   (b) the persisted system_prompt exactly matches a known preset's
  //       template — meaning the chat was last saved under that preset.
  // Case (b) lets the rail surface the preset relationship even after a
  // page reload where the in-session lock-in state is gone.
  // Highlight when a non-default preset is actively selected, or when the
  // persisted system_prompt exactly matches a known preset's template.
  // RAW_PRESET_ID ("none") means no preset — no highlight.
  const presetMatch =
    (activePreset !== DEFAULT_PRESET_ID && activePreset !== RAW_PRESET_ID) ||
    Object.values(PRESETS).some(
      (p) =>
        settings.system_prompt !== null &&
        settings.system_prompt === p.system_prompt,
    );

  // ─── Render ──────────────────────────────────────────────────────────────

  return (
    <>
      {/* The wrapping Drawer already renders a
          banner heading "Chat settings"; the in-body h2 was a duplicate
          that confused screen readers.  Keep just the SavedBadge row. */}
      <header className="lmchat-settings-rail__title-row">
        <SavedBadge visible={savedVisible} />
      </header>

      {/* Preset selector — wired to useChatPreset */}
      <Section title="Preset">
        <select
          aria-label="Preset"
          value={activePreset}
          onChange={(e) => {
            setPreset(e.target.value);
          }}
          className="lmchat-settings-rail__select"
          data-testid="chat-settings-preset"
        >
          {/* Regular presets — all entries before the raw escape hatch. */}
          {PRESET_OPTIONS.filter((p) => p.id !== RAW_PRESET_ID).map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
          {/* Disabled separator + raw escape hatch, always last. */}
          <option disabled>──────────────</option>
          <option value={RAW_PRESET_ID}>None · raw model</option>
        </select>
        <p className="lmchat-settings-rail__hint">
          Sets the system prompt sent on every message in this chat. Defaults
          to General (friendly, opinionated baseline). Slash commands
          (/research, /code, /write…) launch one-off sub-agents with a clean
          context — they do not change this setting.
        </p>
      </Section>

      {/* System prompt — preview + override */}
      <Section title="System prompt">
        <ResolvedPromptPreview
          activePresetId={activePreset}
          projectSystemPrompt={projectSystemPrompt}
          amendment={systemPrompt}
        />
        {/* Label + textarea + hint are ONE thing — field-stack keeps them
            at GLUE-RELAXED (8px) instead of the section-body's 16px sibling
            gap. The label previously borrowed the 12px __hint class with
            inline overrides; it now has its own 13px label treatment. */}
        <div className="lmchat-settings-rail__field-stack">
          <label
            htmlFor="chat-settings-system-prompt-textarea"
            className="lmchat-settings-rail__textarea-label"
          >
            Custom instructions
          </label>
          <textarea
            id="chat-settings-system-prompt-textarea"
            aria-label="Custom instructions for this chat"
            value={systemPrompt}
            onChange={(e) => {
              setSystemPrompt(e.target.value);
            }}
            onBlur={() => {
              persistString("system_prompt", systemPrompt);
            }}
            placeholder="Add custom instructions for this chat (appended below the preset prompt)…"
            className={`lmchat-settings-rail__textarea${presetMatch ? " lmchat-settings-rail__textarea--preset" : ""}`}
            rows={4}
            data-testid="chat-settings-system-prompt"
          />
          <p className="lmchat-settings-rail__hint">
            Appended below the preset prompt and the project prompt before
            sending. Leave blank to use the preset as-is.
          </p>
          {presetMatch && (
            <p className="lmchat-settings-rail__hint">
              Active preset: {activePreset}
            </p>
          )}
        </div>
      </Section>

      {/* Basic params */}
      <Section title="Sampler">
        <FieldRow
          label="Temperature"
          help="Range 0–2. Lower = more deterministic, higher = more creative. Most models default to 0.7."
        >
          <input
            type="number"
            min={0}
            max={2}
            step={0.1}
            value={temperature}
            onChange={(e) => {
              setTemperature(e.target.value);
            }}
            onBlur={() => {
              persistNumber("temperature", temperature);
            }}
            className="lmchat-settings-rail__input"
            aria-label="Temperature"
            data-testid="chat-settings-temperature"
          />
        </FieldRow>
      </Section>

      {/* Advanced sampler — renamed from "Advanced"
          so the section telegraphs its contents.  Reasoning effort lives
          inside as one of six advanced sampler fields. */}
      <details
        open={advancedOpen}
        onToggle={(e) => {
          setAdvancedOpen((e.target as HTMLDetailsElement).open);
        }}
        className="lmchat-settings-rail__advanced"
      >
        <summary
          className="lmchat-settings-rail__advanced-summary"
          title={ADVANCED_TOOLTIP}
          data-testid="chat-settings-advanced-summary"
        >
          Advanced sampler
        </summary>
        <div className="lmchat-settings-rail__advanced-body">
          <FieldRow
            label="top_p"
            help="Range 0–1. Nucleus sampling — keep only tokens whose cumulative probability ≤ this. 0.9 is common."
          >
            <input
              type="number"
              min={0}
              max={1}
              step={0.05}
              value={topP}
              onChange={(e) => {
                setTopP(e.target.value);
              }}
              onBlur={() => {
                persistNumber("top_p", topP);
              }}
              className="lmchat-settings-rail__input"
              aria-label="top_p"
              data-testid="chat-settings-top-p"
            />
          </FieldRow>
          <FieldRow
            label="top_k"
            help="Integer ≥1. Keep only the top-k most likely tokens. 40 is a common starting value; 0/blank disables."
          >
            <input
              type="number"
              min={1}
              step={1}
              value={topK}
              onChange={(e) => {
                setTopK(e.target.value);
              }}
              onBlur={() => {
                persistNumber("top_k", topK);
              }}
              className="lmchat-settings-rail__input"
              aria-label="top_k"
              data-testid="chat-settings-top-k"
            />
          </FieldRow>
          <FieldRow
            label="min_p"
            help="Range 0–1. Discard tokens whose probability is less than min_p × top-token probability. 0.05 is a common floor."
          >
            <input
              type="number"
              min={0}
              max={1}
              step={0.01}
              value={minP}
              onChange={(e) => {
                setMinP(e.target.value);
              }}
              onBlur={() => {
                persistNumber("min_p", minP);
              }}
              className="lmchat-settings-rail__input"
              aria-label="min_p"
              data-testid="chat-settings-min-p"
            />
          </FieldRow>
          <FieldRow
            label="Repeat penalty"
            help="Penalty for re-emitting recently used tokens. 1.0 = none; 1.1 is mild; >1.3 starts to feel forced."
          >
            <input
              type="number"
              min={0}
              max={5}
              step={0.05}
              value={repeatPenalty}
              onChange={(e) => {
                setRepeatPenalty(e.target.value);
              }}
              onBlur={() => {
                persistNumber("repeat_penalty", repeatPenalty);
              }}
              className="lmchat-settings-rail__input"
              aria-label="Repeat penalty"
              data-testid="chat-settings-repeat-penalty"
            />
          </FieldRow>
          <FieldRow
            label="Max tokens"
            help="Cap on the reply length. Blank = no cap (the model decides when to stop)."
          >
            <input
              type="number"
              min={1}
              step={1}
              value={maxTokens}
              onChange={(e) => {
                setMaxTokens(e.target.value);
              }}
              onBlur={() => {
                persistNumber("max_tokens", maxTokens);
              }}
              className="lmchat-settings-rail__input"
              aria-label="Max tokens"
              data-testid="chat-settings-max-tokens"
            />
          </FieldRow>
          <FieldRow
            label="Repeat-loop cut"
            help="Cut a tool-calling loop after this many identical calls. Empty = inherit the global default. 0 disables."
          >
            <input
              type="number"
              min={0}
              max={100}
              step={1}
              value={repeatWarningCutK}
              onChange={(e) => {
                setRepeatWarningCutK(e.target.value);
              }}
              onBlur={() => {
                persistRepeatWarningCutK(repeatWarningCutK);
              }}
              className="lmchat-settings-rail__input"
              aria-label="Repeat-loop cut"
              data-testid="chat-settings-repeat-warning-cut-k"
            />
          </FieldRow>
          {/* Gate on capabilities.reasoning, with ReasoningToggle mounted here
              next to the select. When the selected model doesn't report
              reasoning capabilities the whole section is hidden — no chrome
              for non-reasoning models. */}
          {modelCapabilities?.reasoning !== null && (
            <FieldRow
              label="Reasoning effort"
              help="For reasoning-capable models: how much hidden thinking to perform before answering. Higher = slower but more thorough."
            >
              <div className="lmchat-settings-rail__reasoning-row">
                <select
                  aria-label="Reasoning effort"
                  title={REASONING_TOOLTIP}
                  value={reasoning}
                  onChange={(e) => {
                    const next = (e.target.value || "") as ReasoningLevel | "";
                    setReasoning(next);
                    persistReasoning(next);
                  }}
                  className="lmchat-settings-rail__select"
                  data-testid="chat-settings-reasoning"
                >
                  <option value="">use global default</option>
                  {REASONING_LEVELS.map((r) => (
                    <option key={r} value={r}>
                      {r}
                    </option>
                  ))}
                </select>
                {/* ReasoningToggle: one-click cycle UI for the per-chat override.
                    Mounted in the rail, not TopBar. Passes chatId so the toggle
                    controls the per-chat override, and passes the Rail's OWN
                    resolved `reasoning` value as `reasoningOverride` so the
                    toggle displays the exact same effective value as the
                    select above it — one derivation, not two. */}
                <ReasoningToggle chatId={chatId} reasoningOverride={reasoning} />
              </div>
            </FieldRow>
          )}
        </div>
      </details>

      {/* Quality toggles */}
      <Section title="Quality">
        <FieldRow label="Self-consistency">
          <input
            type="checkbox"
            checked={settings.self_consistency_enabled === true}
            onChange={(e) => {
              persistBool("self_consistency_enabled", e.target.checked);
            }}
            aria-label="Enable self-consistency"
            data-testid="chat-settings-sc"
          />
        </FieldRow>
        <FieldRow label="Chain-of-verification">
          <input
            type="checkbox"
            checked={settings.chain_of_verification_enabled === true}
            onChange={(e) => {
              persistBool("chain_of_verification_enabled", e.target.checked);
            }}
            aria-label="Enable chain-of-verification"
            data-testid="chat-settings-cove"
          />
        </FieldRow>
        <FieldRow label="Stateless turn">
          <input
            type="checkbox"
            checked={settings.stateless === true}
            onChange={(e) => {
              persistBool("stateless", e.target.checked);
            }}
            aria-label="Stateless"
            data-testid="chat-settings-stateless"
          />
        </FieldRow>
      </Section>
    </>
  );
}

// ─── Empty state ────────────────────────────────────────────────────────────

function EmptyState() {
  return (
    <div
      className="lmchat-settings-rail__empty"
      data-testid="chat-settings-empty"
    >
      {/* Drawer banner already provides the
          page-level heading; the in-body h2 was duplicative. */}
      <p className="lmchat-settings-rail__empty-copy">
        Open a chat to configure per-chat settings — preset, system prompt,
        sampler, and quality controls live here.
      </p>
      <p className="lmchat-settings-rail__empty-hint">
        Global defaults live in <strong>Settings</strong> in the sidebar.
      </p>
    </div>
  );
}

// ─── Saved-pulse badge ──────────────────────────────────────────────────────

function SavedBadge({ visible }: { visible: boolean }) {
  return (
    <span
      role="status"
      aria-live="polite"
      className="lmchat-settings-rail__saved"
      style={{ opacity: visible ? 1 : 0 }}
      data-testid="chat-settings-saved"
    >
      <Check size={14} aria-hidden style={{ flexShrink: 0 }} />
      <span>Saved</span>
    </span>
  );
}

// ─── Helpers ────────────────────────────────────────────────────────────────

function stringOrEmpty(v: number | null | undefined): string {
  return v === null || v === undefined ? "" : String(v);
}

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="lmchat-settings-rail__section">
      <h3 className="lmchat-settings-rail__section-title">{title}</h3>
      <div className="lmchat-settings-rail__section-body">{children}</div>
    </section>
  );
}

function FieldRow({
  label,
  help,
  children,
}: {
  label: string;
  /** Optional one-line explainer rendered as a title tooltip on the
   *  ⓘ glyph next to the label — sampler params otherwise have no
   *  contextual help. */
  help?: string | undefined;
  children: ReactNode;
}) {
  return (
    <div className="lmchat-settings-rail__field-row">
      <span className="lmchat-settings-rail__field-label">
        {label}
        {help !== undefined && (
          <span
            tabIndex={0}
            role="img"
            aria-label={`${label} — ${help}`}
            title={help}
            className="lmchat-settings-rail__info-glyph"
          >
            <Info size={12} aria-hidden="true" />
          </span>
        )}
      </span>
      {children}
    </div>
  );
}

// ─── ResolvedPromptPreview ─────────────────────────────────────────────────
//
// Surfaces the system prompt LM Chat will actually send to the model for this
// chat. Three layers, in send order:
//
//   1. Project prompt (if the chat is in a project that has one set) —
//      prepended server-side.
//   2. Active preset's system_prompt — the mode's behavior contract, shown
//      verbatim. Presets carry no date/tools placeholders — the backend
//      wraps the composed prompt with its own [Context] (date, local-host
//      framing) and [Capabilities] (tool surface) blocks before it reaches
//      the model, so nothing needs substituting here.
//   3. The "Custom instructions" textarea below — appended to (2). This
//      is what the user types here to amend the chat's behavior.
//
// The "Composed (what gets sent)" section at the bottom shows the
// assembled result so the user can see exactly what reaches the model
// (before the BE's [Context]/[Capabilities] wrapping). The amendment
// field's BE behavior is OWNED BY the Composer: the Composer reads
// `currentChat.settings.system_prompt` (the amendment) and appends it to
// the preset's system_prompt on the payload at send time.

function ResolvedPromptPreview({
  activePresetId,
  projectSystemPrompt,
  amendment,
}: {
  activePresetId: string;
  projectSystemPrompt: string | null;
  amendment: string;
}) {
  // RAW_PRESET_ID ("none") and any unknown id resolve to null (no prompt).
  const preset =
    activePresetId !== "" &&
    activePresetId !== RAW_PRESET_ID &&
    activePresetId in PRESETS
      ? (PRESETS[activePresetId] ?? null)
      : null;
  const presetPrompt = preset?.system_prompt ?? "";
  const trimmedAmendment = amendment.trim();
  const trimmedProject = projectSystemPrompt?.trim() ?? "";

  // Composed = project + preset + amendment, separated by double newline.
  // Matches the Composer's compose-on-send rule + the BE's project-prompt
  // prepend in streaming_service.py.
  const composedSections = [
    trimmedProject,
    presetPrompt.trim(),
    trimmedAmendment,
  ].filter((s) => s !== "");
  const composed = composedSections.join("\n\n");
  const hasAny = composed !== "";

  return (
    <details
      className="lmchat-settings-rail__prompt-preview"
      data-testid="chat-settings-system-prompt-preview"
    >
      <summary>
        {hasAny ? "View the prompt this chat sends" : "No system prompt set"}
      </summary>
      <div className="lmchat-settings-rail__prompt-preview-body">
        {hasAny ? (
          <>
            {trimmedProject !== "" && (
              <PromptLayer
                title="Project prompt"
                detail={
                  <>
                    From the project this chat belongs to. Prepended
                    server-side.
                  </>
                }
                content={trimmedProject}
              />
            )}
            {preset !== null && (
              <PromptLayer
                title="Preset prompt"
                detail={
                  <>
                    From the active preset <strong>{preset.label}</strong>.
                    Shown verbatim — the backend adds today's date and the
                    live tool surface separately when it assembles the
                    request.
                  </>
                }
                content={presetPrompt}
              />
            )}
            {trimmedAmendment !== "" && (
              <PromptLayer
                title="Custom instructions"
                detail={<>Your amendment for this chat (appended below).</>}
                content={trimmedAmendment}
              />
            )}
            {composedSections.length > 1 && (
              <PromptLayer
                title="Composed (what gets sent)"
                detail={
                  <>
                    The assembled system prompt the model receives, with
                    layers concatenated in send order.
                  </>
                }
                content={composed}
                emphasis
              />
            )}
          </>
        ) : (
          <p className="lmchat-settings-rail__prompt-preview-empty">
            No system prompt is active for this chat. Select a preset above
            (defaults to General) or type custom instructions below.
          </p>
        )}
      </div>
    </details>
  );
}

/* Layer styles live in chat.css (.lmchat-settings-rail__prompt-layer-*);
   the mono content is 13px — Commit Mono's minimum comfortable size. */
function PromptLayer({
  title,
  detail,
  content,
  emphasis,
}: {
  title: string;
  detail: ReactNode;
  content: string;
  emphasis?: boolean;
}) {
  return (
    <div className="lmchat-settings-rail__prompt-layer">
      <div className="lmchat-settings-rail__prompt-layer-title">{title}</div>
      <div className="lmchat-settings-rail__prompt-layer-detail">{detail}</div>
      <pre
        className={`lmchat-settings-rail__prompt-layer-pre${emphasis === true ? " lmchat-settings-rail__prompt-layer-pre--emphasis" : ""}`}
      >
        {content}
      </pre>
    </div>
  );
}
