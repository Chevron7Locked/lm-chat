/* SPDX-License-Identifier: Apache-2.0 */
/**
 * LmStudioAuthBanner — non-fatal banner shown when LM Studio model refreshes
 * are blocked: the API key was pruned by a secret rotation (`key_pruned`), or
 * the probe got a 401 (`auth_failed`). It links to Settings → LM Studio.
 *
 * Rendered in two places: inside AppShell (settings/admin routes) AND directly
 * on the Chat view, which lives OUTSIDE AppShell (router.tsx intentionally
 * excludes Chat from AppLayout). Without it the chat surface shows only an
 * opaque "model not loaded" error with no actionable cause — see the
 * model-state robustness plan, R1b. The banner clears automatically once the
 * backend clears `auth_failed` (the forced-reprobe recovery path) and
 * `useLmStudioConfig` refetches.
 */
import type { CSSProperties } from "react";
import { Link } from "react-router-dom";
import { useLmStudioConfig } from "@/hooks/useLmStudioConfig";

export function LmStudioAuthBanner() {
  const { data: lmConfig } = useLmStudioConfig();
  const keyPruned = lmConfig?.key_pruned === true;
  const authFailed = lmConfig?.auth_failed === true;

  if (keyPruned) {
    return (
      <div
        role="alert"
        aria-label="LM Studio API key needs to be re-entered"
        style={bannerStyle}
      >
        <span>
          LM Studio API key was cleared by a secret rotation. Models won't load
          until it's re-saved.
        </span>
        <Link to="/settings/lm-studio" style={linkStyle}>
          Open Settings → LM Studio
        </Link>
      </div>
    );
  }
  if (authFailed) {
    return (
      <div
        role="alert"
        aria-label="LM Studio authentication failed"
        style={bannerStyle}
      >
        <span>
          LM Studio returned 401 — the API key may be incorrect or expired.
          Model refreshes are paused until re-authenticated.
        </span>
        <Link to="/settings/lm-studio" style={linkStyle}>
          Open Settings → LM Studio
        </Link>
      </div>
    );
  }
  return null;
}

const bannerStyle: CSSProperties = {
  display: "flex",
  flexWrap: "wrap",
  alignItems: "center",
  gap: "var(--space-sibling)",
  padding: "var(--space-glue-relaxed) var(--space-sibling-relaxed)",
  background: "var(--color-warning-bg, oklch(0.92 0.07 75))",
  color: "var(--color-warning-text, oklch(0.25 0.05 65))",
  borderBottom: "1px solid var(--color-border)",
  fontSize: "var(--fs-body)",
};

const linkStyle: CSSProperties = {
  color: "inherit",
  fontWeight: 600,
  textDecoration: "underline",
};
