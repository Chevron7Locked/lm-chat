/* SPDX-License-Identifier: Apache-2.0 */
// ─── Feature 3 — SubchatDivider ──────────────────────────────────────────────

/**
 * SubchatDivider — thin labeled rule marking where a preset session begins.
 * Ported from v0.5.x app.js:4369–4376 (subchat-frame / subchat-label).
 * Uses copper accent + a 1px full-width rule. NO side-stripe border.
 */
export function SubchatDivider({ label }: { label: string }) {
  return (
    <div
      className="lmchat-subchat-divider"
      role="separator"
      aria-label={`${label} mode`}
    >
      <span className="lmchat-subchat-line" aria-hidden="true" />
      <span className="lmchat-subchat-label">{label} mode</span>
      <span className="lmchat-subchat-line" aria-hidden="true" />
    </div>
  );
}
