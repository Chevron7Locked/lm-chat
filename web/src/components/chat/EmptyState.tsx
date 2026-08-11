/* SPDX-License-Identifier: Apache-2.0 */
import { usePlatform } from "@/hooks/usePlatform";
import { useModelList } from "@/hooks/useModelList";
import { BrandMark } from "@/components/BrandMark";

interface EmptyStateProps {
  onNewChat: () => void;
}

// Friendlier empty-state with a clear primary CTA + light orientation
// copy.  Replaces the lone "Select a chat" line that v1 shipped.
export function EmptyState({ onNewChat }: EmptyStateProps) {
  // Welcome hints render the active platform's modifier
  // (⌘ on Mac, Ctrl elsewhere) instead of hard-coded "Cmd".
  const platform = usePlatform();
  const mod = platform.modLabel;
  // LM Studio reachability check for the marginalia copy.
  const { status: lmStatus } = useModelList();
  // Time-of-day greeting variation.  Replaces the static
  // "Welcome to lm-chat" with one of four context-aware lines based
  // on the local hour.  Subtle, considered, no emoji change — keeps
  // the refined-not-ornate principle.  Computed once per mount so it
  // doesn't reflow during a chat-list refresh.
  const greeting = (() => {
    const h = new Date().getHours();
    if (h < 5) return "Working late?";
    if (h < 12) return "Good morning";
    if (h < 18) return "Good afternoon";
    if (h < 22) return "Good evening";
    return "Burning the midnight tokens?";
  })();
  return (
    <div className="lmchat-empty-hint lmchat-foxing-bottomleft">
      {/* Oversized copper bars motif — the LM Chat mark blown up as an
          editorial backdrop (low opacity, bled off the right edge), giving
          the empty canvas character the way LM Studio's site leans on its
          mascot. aria-hidden; purely decorative. */}
      <div
        className="atelier-motif-drift lmchat-empty-motif"
        aria-hidden="true"
      >
        <BrandMark size={440} className="lmchat-empty-motif__mark" />
      </div>
      <div className="lmchat-empty-content" data-testid="chat-empty-state">
        <span className="lmchat-empty-kicker">LM Chat · for LM Studio</span>
        <h1 className="lmchat-empty-title">{greeting}</h1>
        {lmStatus === "error" ? (
          <p className="fs-marginalia lmchat-empty-marginalia">
            Load a model in LM Studio to begin.
          </p>
        ) : (
          <p className="fs-marginalia lmchat-empty-marginalia">
            the room is here, waiting.
          </p>
        )}
        <p className="lmchat-empty-copy">
          Local models via LM Studio, or any cloud provider you connect. Start
          a conversation, pin the replies worth keeping, attach documents for
          RAG, and switch models per chat.
        </p>
        {/* Editorial rhythm: a hand-drawn fade-divider separates the
            description from the action block, giving the empty state a
            two-act structure rather than a single block of text. */}
        <hr className="lmchat-fade-divider" />
        <div className="lmchat-empty-hints-row">
          <span className="lmchat-empty-hint-item">
            <kbd className="lmchat-kbd-chip">{mod}</kbd>
            <kbd className="lmchat-kbd-chip">K</kbd>
            <span className="lmchat-empty-hint-label">command search</span>
          </span>
          <span className="lmchat-empty-hint-dot" aria-hidden="true" />
          <span className="lmchat-empty-hint-item">
            <kbd className="lmchat-kbd-chip">{mod}</kbd>
            <kbd className="lmchat-kbd-chip">/</kbd>
            <span className="lmchat-empty-hint-label">slash palette</span>
          </span>
        </div>
        <button
          type="button"
          onClick={onNewChat}
          className="atelier-cta lmchat-empty-cta"
        >
          Start your first chat
          <span aria-hidden="true" className="lmchat-empty-cta__arrow">
            →
          </span>
        </button>
      </div>
    </div>
  );
}
