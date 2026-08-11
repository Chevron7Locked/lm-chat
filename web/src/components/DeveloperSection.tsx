/* SPDX-License-Identifier: Apache-2.0 */
/**
 * DeveloperSection — Settings → Developer tab.
 *
 * Houses developer / debug toggles. The verbose-logging checkbox was
 * previously in the Sidebar stats footer (confusing cognitive load);
 * moved here so power users can find it without cluttering the chat surface.
 *
 * Uses settings.css semantic classes rather than inline CSSProperties.
 * Spacing grammar applied:
 *   - Toggle row ↔ description: SIBLING (12px) via .lmchat-section-container gap
 *   - Label ↔ control: SIBLING (12px) via .lmchat-toggle-row gap
 *   - Checkbox ↔ hint text: GLUE-RELAXED (8px) via .lmchat-toggle-label gap
 */
import { useDebugStore } from "@/stores/debugStore";
import "@/styles/settings.css";

export function DeveloperSection() {
  const debugEnabled = useDebugStore((s) => s.enabled);
  const setDebugEnabled = useDebugStore((s) => s.setEnabled);

  return (
    <div
      className="lmchat-section-container"
      data-testid="settings-developer-section"
    >
      <section className="lmchat-section" aria-label="Developer options">
        <div className="lmchat-meta-block">
          <div className="lmchat-toggle-row">
            <span className="lmchat-meta-label">Verbose console logging</span>
            <label
              className="lmchat-toggle-label"
              data-testid="settings-debug-toggle"
            >
              <input
                type="checkbox"
                checked={debugEnabled}
                onChange={(e) => {
                  setDebugEnabled(e.target.checked);
                }}
                aria-label="Enable verbose console logging"
                style={{ margin: 0 }}
              />
              <span className="lmchat-toggle-hint">
                {debugEnabled ? "On" : "Off"}
              </span>
            </label>
          </div>
        </div>
        <p
          className="lmchat-section-description"
          style={{ maxWidth: "44rem", marginTop: "var(--space-sibling)" }}
        >
          Adds debug-level entries to the browser console. Useful when
          diagnosing issues with chat streaming, sidebar refresh, or keyboard
          shortcuts.
        </p>
      </section>
    </div>
  );
}
