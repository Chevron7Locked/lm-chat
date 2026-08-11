/* SPDX-License-Identifier: Apache-2.0 */
import { ABCompareView } from "@/components/ABCompareView";
import type { ABStreamState } from "@/hooks/useABStream";

// ─── ABComparePane — A/B compare exit strip + dual-pane view ────────────────
// A/B compare mode: render two-pane view instead of message list.

interface ABComparePaneProps {
  abState: ABStreamState;
  modelALabel: string;
  modelBLabel: string;
  onSelect: (pane: "A" | "B", content: string) => void;
  onExit: () => void;
}

export function ABComparePane({
  abState,
  modelALabel,
  modelBLabel,
  onSelect,
  onExit,
}: ABComparePaneProps) {
  return (
    <>
      {/* Exit strip — lets the user leave compare mode without
          committing either pane. Sits at the top of the messages
          area; styled inline to avoid a new CSS class for a
          one-liner affordance. */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "flex-end",
          padding: "var(--space-glue) var(--space-sibling)",
          borderBottom: "1px solid var(--color-border)",
          background: "var(--color-surface)",
          flexShrink: 0,
        }}
      >
        <button
          type="button"
          onClick={onExit}
          style={{
            padding: "4px var(--space-sibling)",
            background: "none",
            border: "1px solid var(--color-border-default)",
            borderRadius: "var(--radius-sm)",
            fontSize: "var(--fs-caption)",
            color: "var(--color-text-subtle)",
            cursor: "pointer",
          }}
          data-testid="ab-compare-exit"
        >
          Exit compare
        </button>
      </div>
      <ABCompareView
        state={abState}
        modelALabel={modelALabel}
        modelBLabel={modelBLabel}
        onSelectA={(content) => {
          onSelect("A", content);
        }}
        onSelectB={(content) => {
          onSelect("B", content);
        }}
      />
    </>
  );
}
