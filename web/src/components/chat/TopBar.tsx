/* SPDX-License-Identifier: Apache-2.0 */
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { Lock, Menu } from "lucide-react";
import { useViewport } from "@/hooks/useViewport";
import { useModelList } from "@/hooks/useModelList";
import { useChatModelOptions } from "@/hooks/useChatModelOptions";
import { ModelSelectControl } from "@/components/ModelSelectControl";
import { OverflowMenu } from "@/components/OverflowMenu";
import { LmStudioStatusBadge } from "@/components/LmStudioStatusBadge";
import { ChatHeaderMenu } from "@/components/ChatHeaderMenu";
import type { PanelView } from "./shared";
import { AUTO_MODEL_VALUE, AUTO_MODEL_LABEL } from "./shared";

// ─── TopBar ──────────────────────────────────────────────────────────────────

interface TopBarProps {
  title: string;
  modelId: string;
  onModelChange: (id: string) => void;
  /** True when the user has a configured default model, so "Auto" has
   *  something to resolve to. When false the picker prompts the user to
   *  choose a model instead of offering "Auto". */
  hasDefaultModel?: boolean | undefined;
  /** Render a hamburger that toggles the mobile sidebar drawer. */
  onMobileMenuClick?: (() => void) | undefined;
  pinned: boolean;
  onPinToggle: () => void;
  onDelete: () => void;
  onFork: () => void;
  onSettingsOpen: () => void;
  onMemoryOpen: () => void;
  onDocumentsOpen: () => void;
  /** Toggle the pinned-messages panel. */
  onPinsOpen: () => void;
  pinsOpen: boolean;
  panelView: PanelView;
  chatId: number | null;
  ragEnabled?: boolean | undefined;
  onRagToggle?: (() => void) | undefined;
  /** When true, render an "Incognito" badge next to the title. */
  incognito?: boolean | undefined;
  /** Sub-agent mode: the active sub-session's persona label (e.g. "CODER"), or
   * null when no sub-session is running. This is the ONE allowed persistent pill
   * (per the persona-no-pill / sub-agent-is-the-pill model) — it surfaces the
   * transient mode as topbar chrome so it stays visible while scrolled. */
  subAgentLabel?: string | null | undefined;
  /** "Turn this chat into a Project" — undefined hides the overflow item
   * entirely (chat already in a project, incognito, or a sub-session is
   * open); set means the caller already ran the gate. */
  onPromoteToProject?: (() => void) | undefined;
  /** Chat metadata + messages for the export/share menu. */
  exportChat?:
    | {
        id: number;
        title: string;
        created_at?: string | null;
      }
    | null
    | undefined;
  exportMessages?:
    | readonly {
        id: number;
        role: string;
        content: string;
        reasoning_content?: string | null;
        created_at?: string | null;
      }[]
    | undefined;
  /** Counter that opens the export menu when it changes. */
  exportMenuSignal?: number | undefined;
}

export function TopBar({
  title,
  modelId,
  onModelChange,
  hasDefaultModel,
  onMobileMenuClick,
  pinned,
  onPinToggle,
  onDelete,
  onFork,
  onSettingsOpen,
  onMemoryOpen,
  onDocumentsOpen,
  panelView,
  chatId,
  onPinsOpen,
  pinsOpen,
  ragEnabled,
  onRagToggle,
  incognito,
  subAgentLabel,
  onPromoteToProject,
  exportChat,
  exportMessages,
  exportMenuSignal,
}: TopBarProps) {
  // Consume the LM Studio model list through the
  // state-machine-aware hook so the dropdown surfaces loaded vs unloaded
  // models distinctly and falls back to a helpful empty state.
  const { status: lmStatus } = useModelList();
  const navigate = useNavigate();
  // Mobile overflow: collapse panel-toggle buttons + share into the ⋯ menu
  // so nothing sits off-screen at 390px. Read isMobile here (TopBar doesn't
  // receive it as a prop — the parent already renders the full desktop layout).
  const { isMobile } = useViewport();
  // Share / Export: local signal so the hidden-trigger ChatHeaderMenu can
  // be imperatively opened from the overflow menu item — both the desktop
  // and mobile branches route their "Share / Export" action through it.
  const [shareMenuSignal, setShareMenuSignal] = useState(0);
  // Use the canonical chat-model options hook so embedding models are
  // excluded and the (unloaded) suffix is consistent across surfaces
  // (composer here, Appearance default, LM Studio default, Memory pin).
  const { options: chatModelOptionsRaw, groups: chatModelGroupsRaw, isLoading: chatModelOptionsLoading } =
    useChatModelOptions();
  // Build composite "<provider>::<model_id>" option ids for multi-provider support.
  // ModelSelectControl receives these composite values; onModelChange decodes them.
  const chatModelOptions = chatModelOptionsRaw.map((o) => ({
    ...o,
    id: `${o.provider}::${o.id}`,
  }));
  const chatModelGroups = chatModelGroupsRaw.map((g) => ({
    ...g,
    options: g.options.map((o) => ({
      ...o,
      id: `${o.provider}::${o.id}`,
    })),
  }));
  const showLoadedFirst: { id: string; name: string; loaded: boolean }[] =
    chatModelOptions.map((o) => ({
      id: o.id,
      name: o.label,
      loaded: o.loaded,
    }));
  // Healthy state (LM Studio reachable + at least one model available): the
  // picker's leading entry is a selectable "Auto" — the chat has no explicit
  // per-chat model override, so it resolves to the user's default model at
  // send time. "Auto" is only offered when a default actually exists to
  // resolve to (``hasDefaultModel``); otherwise the composer would block the
  // send, so we keep the "Select a model…" prompt. The other informative
  // placeholders cover the states where nothing can be sent at all (no model
  // loaded / unreachable / still loading).
  const healthy =
    lmStatus !== "no_models" &&
    lmStatus !== "error" &&
    showLoadedFirst.length > 0;
  const showAuto = healthy && hasDefaultModel === true;
  const autoOption = showAuto
    ? { value: AUTO_MODEL_VALUE, label: AUTO_MODEL_LABEL }
    : undefined;
  const placeholderLabel =
    lmStatus === "no_models"
      ? "No model loaded — open Settings"
      : lmStatus === "error"
        ? "LM Studio unreachable"
        : showLoadedFirst.length === 0
          ? "Loading models…"
          : showAuto
            ? ""
            : "Select a model…";

  // Single-row mobile header — 72px floor on most mobile widths,
  // collapsing to 56px (iOS standard nav height) at ≤480px.
  // Hamburger (44×44) → title+model-pill column (flex:1) → status badge → ⋯ overflow.
  // Model selector collapsed to a compact status pill below the title.
  // The MobileDock (Memory/Docs/RAG/Settings) stays above the composer.
  if (isMobile) {
    const activeModelName = showLoadedFirst.find((m) => m.id === modelId)?.name;
    const modelPillLabel =
      modelId === AUTO_MODEL_VALUE
        ? AUTO_MODEL_LABEL
        : activeModelName !== undefined && activeModelName !== ""
          ? activeModelName
          : lmStatus === "no_models"
            ? "no model · open settings"
            : lmStatus === "error"
              ? "LM Studio unreachable"
              : "select model";

    return (
      <header className="lmchat-topbar-shell--mobile">
        {onMobileMenuClick !== undefined && (
          <button
            type="button"
            aria-label="Open sidebar"
            onClick={onMobileMenuClick}
            className="lmchat-mobile-menu-btn"
            data-testid="topbar-mobile-menu"
          >
            <Menu size={18} aria-hidden />
          </button>
        )}

        {/* Title + model pill stacked vertically, flex:1 */}
        <div className="lmchat-topbar-title-stack">
          <div className="lmchat-topbar-title-row">
            <span className="lmchat-chat-title--mobile" title={title}>
              {title}
            </span>
            {incognito === true && (
              <span
                role="status"
                aria-label="Incognito chat"
                title="Incognito — memory writes disabled, chat purged on logout"
                className="lmchat-incognito-chip lmchat-incognito-chip--mobile"
                data-testid="chat-incognito-badge"
              >
                <Lock size={11} aria-hidden />
              </span>
            )}
          </div>
          {/* Model status pill — compact select styled as italic marginalia.
              Routed through the canonical ``ModelSelectControl`` so the
              typography stays unified with the other model-picker sites;
              the ``--mobile-wide`` modifier class preserves the
              chat-header pill skin. */}
          <ModelSelectControl
            ariaLabel="Model"
            value={modelId}
            onChange={onModelChange}
            placeholder={placeholderLabel}
            autoOption={autoOption}
            className="lmchat-model-select--mobile-wide"
            testId="chat-header-model-select"
            title={modelPillLabel}
            options={chatModelOptions}
            {...(chatModelGroups.length > 1 ? { groups: chatModelGroups } : {})}
            isLoading={chatModelOptionsLoading}
          />
        </div>

        {/* Badge before the ⋯ overflow — convention puts the overflow
            trigger rightmost. */}
        <span className="lmchat-topbar-badge-wrap">
          <LmStudioStatusBadge compact />
        </span>
        <OverflowMenu
          actions={[
            {
              label: "Settings",
              onClick: () => {
                void navigate("/settings");
              },
            },
            { label: "Reasoning effort", onClick: onSettingsOpen },
            { label: "Pinned messages", onClick: onPinsOpen, active: pinsOpen },
            {
              // Share / Export folded into the ⋯ overflow —
              // a second visible trigger at 390px was redundant chrome and
              // pushed the bar past its pixel budget. Mirrors the desktop
              // overflow wiring; opens the hidden-trigger ChatHeaderMenu.
              label: "Share / Export",
              onClick: () => {
                setShareMenuSignal((s) => s + 1);
              },
            },
            {
              label: pinned ? "Unpin chat" : "Pin chat",
              onClick: onPinToggle,
              active: pinned,
            },
            { label: "Fork chat", onClick: onFork },
            ...(onPromoteToProject !== undefined
              ? [
                  {
                    label: "New project from this chat",
                    onClick: onPromoteToProject,
                  },
                ]
              : []),
            { label: "Delete chat", onClick: onDelete, danger: true },
          ]}
        />
        {/* Share / Export reachable on mobile through the ⋯ overflow item
            above; the menu itself mounts hidden-trigger (zero footprint) and
            opens on either the overflow signal or Cmd/Ctrl+Shift+E. */}
        <ChatHeaderMenu
          chatId={chatId}
          chat={exportChat ?? null}
          messages={exportMessages ?? []}
          incognito={incognito === true}
          hiddenTrigger
          openSignal={shareMenuSignal + (exportMenuSignal ?? 0)}
        />
      </header>
    );
  }

  return (
    <header className="lmchat-topbar-shell">
      {onMobileMenuClick !== undefined && (
        <button
          type="button"
          aria-label="Open sidebar"
          onClick={onMobileMenuClick}
          className="lmchat-mobile-menu-btn"
          data-testid="topbar-mobile-menu"
        >
          <Menu size={18} aria-hidden />
        </button>
      )}
      {/* Title: flex-shrinks on mobile so controls stay reachable */}
      <span className="lmchat-chat-title" title={title}>
        {title}
      </span>
      {incognito === true && (
        <span
          role="status"
          aria-label="Incognito chat"
          title="Incognito — memory writes are disabled and this chat is purged on logout or TTL expiry"
          className="lmchat-incognito-chip"
          data-testid="chat-incognito-badge"
        >
          <Lock size={11} aria-hidden /> Incognito
        </span>
      )}
      {subAgentLabel != null && subAgentLabel !== "" && (
        <span
          role="status"
          aria-label={`Sub-agent mode: ${subAgentLabel}`}
          title={`You're in a ${subAgentLabel} sub-session — a clean-context side conversation with that agent`}
          className="lmchat-subagent-chip"
          data-testid="chat-subagent-badge"
        >
          {subAgentLabel}
        </span>
      )}

      {/* LM Studio connection status badge. Compact on mobile. */}

      {/* Model selector — loaded vs unloaded split into optgroups so the
          difference is structural (native, cross-browser) rather than a
          parenthetical suffix. Loaded models are what LM Studio can serve
          right now; unloaded require a load first.
          Routed through the canonical ModelSelectControl — a bare <select>
          would bypass the capability icons (Eye/Wrench/Brain) that the mobile
          path and the two Settings surfaces already render. */}
      <ModelSelectControl
        ariaLabel="Model"
        value={modelId}
        onChange={onModelChange}
        placeholder={placeholderLabel}
        autoOption={autoOption}
        testId="chat-header-model-select"
        options={chatModelOptions}
        {...(chatModelGroups.length > 1 ? { groups: chatModelGroups } : {})}
        isLoading={chatModelOptionsLoading}
      />
      {/* Distilled action row — primary panel toggles inline; secondary
          actions (reasoning, settings, pins, share, lifecycle) collapsed
          into the ⋯ overflow to match the mobile pattern. */}
      <div className="lmchat-topbar-action-row">
        <>
          <TopBarBtn
            onClick={onMemoryOpen}
            label="Memory"
            aria="Open memory panel"
            active={panelView === "memory"}
          />
          <TopBarBtn
            onClick={onDocumentsOpen}
            label="Docs"
            aria="Open documents panel"
            active={panelView === "documents"}
          />
          {onRagToggle !== undefined && (
            <TopBarBtn
              onClick={onRagToggle}
              label="RAG"
              aria={
                ragEnabled === true
                  ? "Disable RAG for this chat"
                  : "Enable RAG for this chat"
              }
              active={ragEnabled === true}
            />
          )}
          {/* Secondary + lifecycle actions collapsed into ⋯ overflow */}
          <OverflowMenu
            actions={[
              {
                label: "Chat settings",
                onClick: onSettingsOpen,
                active: panelView === "settings",
              },
              { label: "Reasoning effort", onClick: onSettingsOpen },
              {
                label: "Pinned messages",
                onClick: onPinsOpen,
                active: pinsOpen,
              },
              {
                label: "Share / Export",
                onClick: () => {
                  setShareMenuSignal((s) => s + 1);
                },
              },
              {
                label: pinned ? "Unpin chat" : "Pin chat",
                onClick: onPinToggle,
                active: pinned,
              },
              { label: "Fork chat", onClick: onFork },
              ...(onPromoteToProject !== undefined
                ? [
                    {
                      label: "New project from this chat",
                      onClick: onPromoteToProject,
                    },
                  ]
                : []),
              { label: "Delete chat", onClick: onDelete, danger: true },
            ]}
          />
          {/* Hidden-trigger mount. A 0×0 opacity-0 aria-hidden wrapper
              would clip the dropdown panel, so Cmd/Ctrl+Shift+E and the
              overflow "Share / Export" item would open a structurally
              invisible menu. hiddenTrigger renders the trigger sr-only while
              the panel renders in normal flow when opened. */}
          <ChatHeaderMenu
            chatId={chatId}
            chat={exportChat ?? null}
            messages={exportMessages ?? []}
            incognito={incognito === true}
            hiddenTrigger
            // If the desktop branch read only the local share signal,
            // Cmd/Ctrl+Shift+E (which ticks exportMenuSignal) would be
            // silently dead on desktop while KeyboardHelp still advertised
            // it. ChatHeaderMenu opens on ANY change vs its ref, so summing
            // both counters lets either source open the menu without extra
            // plumbing.
            openSignal={shareMenuSignal + (exportMenuSignal ?? 0)}
          />
          <LmStudioStatusBadge compact={false} />
        </>
      </div>
    </header>
  );
}

interface TopBarBtnProps {
  onClick: () => void;
  label: string;
  aria: string;
  active?: boolean | undefined;
  danger?: boolean | undefined;
}

function TopBarBtn({
  onClick,
  label,
  aria,
  active = false,
  danger = false,
}: TopBarBtnProps) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-label={aria}
      aria-pressed={active}
      className="atelier-btn lmchat-topbar-btn"
      data-active={active ? "true" : "false"}
      data-danger={danger ? "true" : "false"}
    >
      {active && <span aria-hidden className="lmchat-topbar-btn__dot" />}
      {label}
    </button>
  );
}
