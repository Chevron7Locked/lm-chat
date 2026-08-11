/* SPDX-License-Identifier: Apache-2.0 */
/**
 * LmStudioStatusBadge — green/yellow/red dot in the chat TopBar.
 *
 * Reflects the live LM Studio probe state via `useLmStudioStatus`.
 * Renders as a small pill with a status dot, label, tooltip on hover,
 * and an aria-label for screen-readers.  Keyboard focusable for AT users.
 */
import { useNavigate } from "react-router-dom";
import { useLmStudioStatus } from "@/hooks/useLmStudioStatus";
import "@/styles/lm-studio-status-badge.css";

// Status dot colors — OKLCH, theme-agnostic (direct values, not token aliases)
const DOT_COLORS = {
  ok: { dot: "oklch(0.72 0.18 145)", glow: "oklch(0.72 0.18 145 / 0.4)" },
  stale: { dot: "oklch(0.78 0.18 80)", glow: "oklch(0.78 0.18 80 / 0.4)" },
  error: { dot: "oklch(0.65 0.22 25)", glow: "oklch(0.65 0.22 25 / 0.4)" },
  idle: { dot: "oklch(0.55 0 0)", glow: "oklch(0.55 0 0 / 0.4)" },
} as const;

interface LmStudioStatusBadgeProps {
  /** Optional override label for the visible text (defaults to "LM Studio"). */
  label?: string;
  /** Hides the text and shows the dot only.  Used in compact layouts. */
  compact?: boolean;
}

export function LmStudioStatusBadge({
  label = "LM Studio",
  compact = false,
}: LmStudioStatusBadgeProps) {
  const { status, tooltip } = useLmStudioStatus();
  const palette = DOT_COLORS[status];
  const navigate = useNavigate();

  // 2026-06-06: previously a static <span> that looked clickable but did
  // nothing. Clicking the pill in the topbar naturally suggests opening
  // the Settings page.
  // Now a real button that navigates to /settings/lm-studio.
  return (
    <button
      type="button"
      onClick={() => {
        void navigate("/settings/lm-studio");
      }}
      aria-label={`${label}: ${tooltip} — open LM Studio settings`}
      title={`${tooltip} — click to open settings`}
      data-status={status}
      data-testid="lm-studio-status-badge"
      className="lmsb-wrapper lmsb-wrapper--button"
    >
      <span
        aria-hidden="true"
        className="lmsb-dot lmchat-status-dot"
        style={{
          background: palette.dot,
          boxShadow: `0 0 0 2px ${palette.glow}`,
          // The ok-state pulse uses the @keyframes lmstatus-pulse defined in
          // globals.css (CSP requirement: no inline keyframe declarations).
          // 7s slow breath (0.5→1.0 opacity) — presence, not alarm.
          ...(status === "ok"
            ? { animation: "lmstatus-pulse 7s ease-in-out infinite" }
            : {}),
        }}
      />
      {!compact && <span className="lmsb-label">{label}</span>}
    </button>
  );
}
