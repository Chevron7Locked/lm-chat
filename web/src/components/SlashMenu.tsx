/* SPDX-License-Identifier: Apache-2.0 */
import "@/styles/composer.css";
/**
 * SlashMenu — autocomplete popover for slash commands in the Composer.
 *
 * Shown when the Composer input starts with "/". Renders a list of matching
 * commands. Selection replaces the slash-prefix in the input.
 *
 * Commands with a presetId launch a TRANSIENT sub-agent session (clean
 * context, chainable) — they do NOT change the chat's persistent system
 * prompt.  The rail picker (ChatSettingsRail) is the sole way to set that.
 *
 * Built-in commands:
 *   /research  — launch a Deep Research sub-agent
 *   /code      — launch a Coding Agent sub-agent
 *   /write     — launch a Creative Writing sub-agent
 *   /analyze   — launch a Strategic Analyst sub-agent
 *   /architect — launch a Systems Architect sub-agent
 *   /help      — show command list
 *   /clear     — clear chat history (with confirm)
 *   /memory    — pin an insight inline
 *   /panel     — multi-model panel (shows coming-soon notice)
 *   /compact   — compact this chat
 *   /fork      — fork at current point
 *   /prompt    — insert a saved prompt
 */
// ─── Types ──────────────────────────────────────────────────────────────────

export interface SlashCommand {
  name: string;
  description: string;
  /** If true, shows a "not yet available" badge instead of running. */
  comingSoon?: boolean | undefined;
  /**
   * When set, this command launches a sub-agent session using the
   * identified persona (see ``@/lib/presets``).  The Composer reads this
   * field to call onPresetActivate; it does NOT set the chat's active_preset.
   * Plain (non-preset) commands leave this undefined.
   */
  presetId?: string | undefined;
}

export interface SlashMenuProps {
  /** The current query text after the leading "/". */
  query: string;
  /** Controlled highlight index (owned by Composer for keyboard nav). */
  activeIdx: number;
  /** Called when mouse hovers a row — lets Composer sync keyboard + mouse state. */
  onHighlight: (idx: number) => void;
  /** Called when the user selects a command. */
  onSelect: (cmd: SlashCommand) => void;
  /** Called when the menu should close (Esc, blur). */
  onClose: () => void;
}

// ─── Command registry ────────────────────────────────────────────────────────

export const BUILTIN_COMMANDS: SlashCommand[] = [
  // Preset-mode commands — listed first for discoverability.
  {
    name: "research",
    description: "Launch a Deep Research sub-agent (clean context)",
    presetId: "research",
  },
  {
    name: "code",
    description: "Launch a Coding Agent sub-agent (clean context)",
    presetId: "coder",
  },
  {
    name: "write",
    description: "Launch a Creative Writing sub-agent (clean context)",
    presetId: "creative",
  },
  {
    name: "analyze",
    description: "Launch a Strategic Analyst sub-agent (clean context)",
    presetId: "analyst",
  },
  {
    name: "architect",
    description: "Launch a Systems Architect sub-agent (clean context)",
    presetId: "architect",
  },
  // Built-in utility commands.
  { name: "help", description: "Show all available commands" },
  { name: "clear", description: "Clear this chat's history" },
  { name: "memory", description: "Pin an insight — /memory <text>" },
  {
    name: "compact",
    description: "Summarize & archive older messages into a foldable recall tab",
  },
  { name: "fork", description: "Fork the conversation from this point" },
  { name: "prompt", description: "Insert a saved prompt — /prompt <name>" },
  {
    name: "compare",
    description: "Compare two models side by side — /compare",
  },
  {
    name: "panel",
    description: "Run a multi-model panel on this chat",
    comingSoon: true,
  },
];

/** Filter commands by prefix match on name. */
export function filterCommands(query: string): SlashCommand[] {
  const q = query.toLowerCase().trim();
  if (q === "") return BUILTIN_COMMANDS;
  return BUILTIN_COMMANDS.filter((c) => c.name.startsWith(q));
}

/** Parse a slash-command string into { name, args }. */
export function parseSlashCommand(
  input: string,
): { name: string; args: string } | null {
  const trimmed = input.trimStart();
  if (!trimmed.startsWith("/")) return null;
  const parts = trimmed.slice(1).split(" ");
  const nameRaw = parts[0] ?? "";
  const args = parts.slice(1).join(" ");
  return { name: nameRaw.toLowerCase(), args };
}

// ─── Component ──────────────────────────────────────────────────────────────

export function SlashMenu({
  query,
  activeIdx,
  onHighlight,
  onSelect,
  onClose,
}: SlashMenuProps) {
  const matches = filterCommands(query);

  if (matches.length === 0) return null;

  const presets = matches.filter((c) => c.presetId !== undefined);
  const utilities = matches.filter((c) => c.presetId === undefined);
  const showGroups = query === "" && presets.length > 0 && utilities.length > 0;

  const renderCmd = (cmd: SlashCommand, globalIdx: number) => (
    <button
      key={cmd.name}
      role="option"
      aria-selected={activeIdx === globalIdx}
      type="button"
      onMouseEnter={() => {
        onHighlight(globalIdx);
      }}
      onClick={() => {
        if (cmd.comingSoon !== true) onSelect(cmd);
        onClose();
      }}
      className={`lmchat-slash-item${activeIdx === globalIdx ? " lmchat-slash-item--active" : ""}`}
    >
      <span className="lmchat-slash-cmd-name">/{cmd.name}</span>
      <span className="lmchat-slash-cmd-desc">{cmd.description}</span>
      {cmd.comingSoon === true && (
        <span className="lmchat-slash-badge">soon</span>
      )}
    </button>
  );

  return (
    <div
      role="listbox"
      aria-label="Slash commands"
      className="lmchat-slash-menu"
      onMouseDown={(e) => {
        e.preventDefault();
      }}
    >
      {showGroups ? (
        <>
          <div className="lmchat-slash-group-label">Sub-agents</div>
          {presets.map((cmd, i) => renderCmd(cmd, i))}
          <div
            className="lmchat-slash-group-label"
            style={{ marginTop: "var(--space-glue)" }}
          >
            Utilities
          </div>
          {utilities.map((cmd, i) => renderCmd(cmd, presets.length + i))}
        </>
      ) : (
        matches.map((cmd, i) => renderCmd(cmd, i))
      )}
    </div>
  );
}

// Styles moved to web/src/styles/composer.css
