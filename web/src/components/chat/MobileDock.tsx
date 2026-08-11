/* SPDX-License-Identifier: Apache-2.0 */
import type { ReactNode } from "react";
import {
  Brain,
  FileText,
  Database,
  Settings as SettingsIcon,
} from "lucide-react";

// ─── MobileDock — persistent panel toggles above the composer ────────────────
// On mobile the TopBar is too narrow to keep Memory/Docs/RAG/Settings inline,
// and burying them in a ⋯ menu makes primary navigation feel hidden. The dock
// surfaces them as four equal targets at the bottom of the chat surface so
// they're always one tap away.
interface MobileDockProps {
  onMemoryOpen: () => void;
  onDocumentsOpen: () => void;
  onRagToggle: (() => void) | undefined;
  onSettingsOpen: () => void;
  panelView: "memory" | "documents" | "settings" | null;
  ragEnabled: boolean;
}

export function MobileDock({
  onMemoryOpen,
  onDocumentsOpen,
  onRagToggle,
  onSettingsOpen,
  panelView,
  ragEnabled,
}: MobileDockProps) {
  return (
    <nav className="lmchat-mobile-dock" aria-label="Chat panels">
      <DockBtn
        icon={<Brain size={16} aria-hidden />}
        label="Memory"
        onClick={onMemoryOpen}
        active={panelView === "memory"}
      />
      <DockBtn
        icon={<FileText size={16} aria-hidden />}
        label="Docs"
        onClick={onDocumentsOpen}
        active={panelView === "documents"}
      />
      {onRagToggle !== undefined && (
        <DockBtn
          icon={<Database size={16} aria-hidden />}
          label="RAG"
          onClick={onRagToggle}
          active={ragEnabled}
        />
      )}
      <DockBtn
        icon={<SettingsIcon size={16} aria-hidden />}
        label="Tune"
        onClick={onSettingsOpen}
        active={panelView === "settings"}
      />
    </nav>
  );
}

function DockBtn({
  icon,
  label,
  onClick,
  active,
}: {
  icon: ReactNode;
  label: string;
  onClick: () => void;
  active: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      aria-pressed={active}
      aria-label={label}
      className="atelier-btn lmchat-dock-btn"
      data-active={active ? "true" : "false"}
    >
      {icon}
      <span className="lmchat-dock-btn-label">{label}</span>
    </button>
  );
}
