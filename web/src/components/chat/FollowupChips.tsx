/* SPDX-License-Identifier: Apache-2.0 */
// ─── Feature 5 — FollowupChips ───────────────────────────────────────────────

/**
 * FollowupChips — clickable suggestion chips after an assistant reply.
 * Ported from v0.5.x app.js:4046–4065 (renderFollowups).
 * Sourcing: MODEL-GENERATED (the model emits suggestions via hidden comment).
 */
export function FollowupChips({
  suggestions,
  onSelect,
  streaming = false,
}: {
  suggestions: string[];
  onSelect: (q: string) => void;
  streaming?: boolean;
}) {
  if (suggestions.length === 0) return null;
  return (
    <div
      className="lmchat-followup-chips"
      role="group"
      aria-label="Follow-up suggestions"
      data-streaming={streaming ? "true" : undefined}
    >
      {suggestions.map((q) => (
        <button
          key={q}
          type="button"
          onClick={() => {
            onSelect(q);
          }}
          disabled={streaming}
          aria-disabled={streaming || undefined}
          className="lmchat-followup-chip atelier-btn"
          data-testid="followup-chip"
        >
          {q}
        </button>
      ))}
    </div>
  );
}
