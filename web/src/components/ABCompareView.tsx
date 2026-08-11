/* SPDX-License-Identifier: Apache-2.0 */
/**
 * ABCompareView — two-column side-by-side A/B model comparison view.
 *
 * Renders two panes side by side, each streaming content from a different
 * LM Studio model.  Each pane shows streaming deltas via ChatMessage and
 * a "Use this response" button to commit the chosen response.
 *
 * Props:
 *   state       — ABStreamState from useABStream
 *   modelALabel — display name or ID for pane A
 *   modelBLabel — display name or ID for pane B
 *   onSelectA   — called when user chooses pane A's response
 *   onSelectB   — called when user chooses pane B's response
 */
import type { CSSProperties } from "react";
import type { ABStreamState } from "@/hooks/useABStream";

// ─── Props ────────────────────────────────────────────────────────────────────

interface ABCompareViewProps {
  state: ABStreamState;
  modelALabel: string;
  modelBLabel: string;
  onSelectA: (content: string) => void;
  onSelectB: (content: string) => void;
}

// ─── Component ───────────────────────────────────────────────────────────────

export function ABCompareView({
  state,
  modelALabel,
  modelBLabel,
  onSelectA,
  onSelectB,
}: ABCompareViewProps) {
  const contentA = state.paneA.contentDeltas.join("");
  const contentB = state.paneB.contentDeltas.join("");

  return (
    // ``lmchat-ab-compare-view`` carries the responsive override so
    // mobile gets a stacked layout. The inline ``containerStyle`` stays
    // for the desktop default — the hardcoded row layout had no breakpoint
    // and overflowed at 393px viewport width.
    <div
      style={containerStyle}
      className="lmchat-ab-compare-view"
      aria-label="A/B model comparison"
    >
      <ABPane
        label={modelALabel}
        content={contentA}
        status={state.paneA.status}
        error={state.paneA.error}
        onSelect={() => {
          onSelectA(contentA);
        }}
      />
      <div style={dividerStyle} aria-hidden="true" />
      <ABPane
        label={modelBLabel}
        content={contentB}
        status={state.paneB.status}
        error={state.paneB.error}
        onSelect={() => {
          onSelectB(contentB);
        }}
      />
    </div>
  );
}

// ─── Pane ─────────────────────────────────────────────────────────────────────

interface ABPaneProps {
  label: string;
  content: string;
  status: "idle" | "streaming" | "complete" | "error";
  error: { code: string; message: string } | null;
  onSelect: () => void;
}

function ABPane({ label, content, status, error, onSelect }: ABPaneProps) {
  const isDone = status === "complete" || status === "error";

  return (
    <div style={paneStyle} role="region" aria-label={`Model ${label} response`}>
      <div style={paneHeaderStyle}>
        <span style={labelStyle}>{label}</span>
        <span style={statusBadgeStyle(status)}>{statusText(status)}</span>
      </div>

      <div style={contentStyle}>
        {error !== null ? (
          <p style={errorStyle} role="alert">
            Error: {error.message}
          </p>
        ) : (
          <pre style={preStyle}>{content}</pre>
        )}
      </div>

      {isDone && error === null && content.length > 0 && (
        <div style={footerStyle}>
          <button
            type="button"
            onClick={onSelect}
            style={selectBtnStyle}
            aria-label={`Use ${label} response`}
          >
            Use this response
          </button>
        </div>
      )}
    </div>
  );
}

// ─── Helpers ──────────────────────────────────────────────────────────────────

function statusText(status: ABPaneProps["status"]): string {
  switch (status) {
    case "idle":
      return "Waiting";
    case "streaming":
      return "Streaming…";
    case "complete":
      return "Done";
    case "error":
      return "Error";
  }
}

// ─── Styles ───────────────────────────────────────────────────────────────────

const containerStyle: CSSProperties = {
  display: "flex",
  flexDirection: "row",
  gap: 0,
  width: "100%",
  height: "100%",
  overflow: "hidden",
};

const dividerStyle: CSSProperties = {
  width: "1px",
  background: "var(--color-border)",
  flexShrink: 0,
};

const paneStyle: CSSProperties = {
  flex: 1,
  display: "flex",
  flexDirection: "column",
  overflow: "hidden",
  minWidth: 0,
};

const paneHeaderStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  gap: "var(--space-glue)",
  padding: "var(--space-glue) var(--space-glue-relaxed)",
  borderBottom: "1px solid var(--color-border)",
  background: "var(--color-surface)",
  flexShrink: 0,
};

const labelStyle: CSSProperties = {
  fontSize: "var(--fs-label)",
  fontWeight: 600,
  color: "var(--color-text)",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};

function statusBadgeStyle(status: ABPaneProps["status"]): CSSProperties {
  const colors: Record<ABPaneProps["status"], string> = {
    idle: "var(--color-text-subtle)",
    streaming: "var(--color-accent)",
    complete: "var(--color-success)",
    error: "var(--color-danger)",
  };
  return {
    fontSize: "var(--fs-caption)",
    color: colors[status],
    marginLeft: "auto",
    flexShrink: 0,
  };
}

const contentStyle: CSSProperties = {
  flex: 1,
  overflowY: "auto",
  padding: "var(--space-glue-relaxed)",
};

const preStyle: CSSProperties = {
  margin: 0,
  fontFamily: "var(--font-sans)",
  fontSize: "var(--fs-body-lg)",
  color: "var(--color-text)",
  lineHeight: 1.6,
  whiteSpace: "pre-wrap",
  wordBreak: "break-word",
};

const errorStyle: CSSProperties = {
  color: "var(--color-danger)",
  fontSize: "var(--fs-body)",
  margin: 0,
};

const footerStyle: CSSProperties = {
  padding: "var(--space-glue) var(--space-glue-relaxed)",
  borderTop: "1px solid var(--color-border)",
  flexShrink: 0,
};

const selectBtnStyle: CSSProperties = {
  padding: "6px var(--space-sibling-relaxed)",
  background: "var(--color-accent)",
  border: "none",
  borderRadius: "var(--radius-md)",
  fontSize: "var(--fs-body)",
  fontWeight: 600,
  color: "var(--color-text-on-accent)",
  cursor: "pointer",
};
