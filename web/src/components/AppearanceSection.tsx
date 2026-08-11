/* SPDX-License-Identifier: Apache-2.0 */
/**
 * AppearanceSection — Settings → Appearance tab.
 *
 * Four grouped rows:
 *   1. Theme (existing 3 buttons + View-Transitions origin logic) + live preview
 *   2. Text size  (Compact / Default / Large)
 *   3. Chat density (Comfortable / Compact)
 *   4. Message style (Bubbles / Flat)
 *
 * Rows 2–4 wire to displayStore. All buttons reuse .lmchat-theme-btn styling
 * with aria-pressed for accessibility.
 */
import { useThemeStore } from "@/stores/themeStore";
import type { Theme } from "@/stores/themeStore";
import { useDisplayStore } from "@/stores/displayStore";
import type { TextSize, Density, MessageStyle } from "@/stores/displayStore";
import "@/styles/settings.css";

// ─── Theme ────────────────────────────────────────────────────────────────────

const THEMES: { value: Theme; label: string }[] = [
  { value: "dark", label: "Dark" },
  { value: "light", label: "Light" },
  { value: "system", label: "System" },
];

// ─── Display pref options ─────────────────────────────────────────────────────

const TEXT_SIZES: { value: TextSize; label: string }[] = [
  { value: "sm", label: "Compact" },
  { value: "md", label: "Default" },
  { value: "lg", label: "Large" },
];

const DENSITIES: { value: Density; label: string }[] = [
  { value: "comfortable", label: "Comfortable" },
  { value: "compact", label: "Compact" },
];

const MESSAGE_STYLES: { value: MessageStyle; label: string }[] = [
  { value: "bubbles", label: "Bubbles" },
  { value: "flat", label: "Flat" },
];

// ─── Segmented-button group (reused for text size, density, message style) ───

interface SegmentedGroupProps<T extends string> {
  options: { value: T; label: string }[];
  current: T;
  onSelect: (v: T) => void;
  groupLabel: string;
}

function SegmentedGroup<T extends string>({
  options,
  current,
  onSelect,
  groupLabel,
}: SegmentedGroupProps<T>) {
  return (
    <div
      role="group"
      aria-label={groupLabel}
      style={{ display: "flex", flexWrap: "wrap" as const, gap: "var(--space-glue)" }}
    >
      {options.map((opt) => (
        <button
          key={opt.value}
          type="button"
          aria-pressed={current === opt.value}
          onClick={() => { onSelect(opt.value); }}
          className={[
            "lmchat-theme-btn",
            current === opt.value ? "lmchat-theme-btn--active" : "",
          ]
            .filter(Boolean)
            .join(" ")}
        >
          {opt.label}
        </button>
      ))}
    </div>
  );
}

// ─── Live preview card ────────────────────────────────────────────────────────

function AppearanceThemePreview() {
  return (
    <div
      className="lmchat-appearance-preview"
      aria-hidden="true"
    >
      <span className="lmchat-preview-eyebrow">Preview</span>

      {/* Faux response line — uses "response" not "assistant" per brand rule */}
      <p className="lmchat-preview-response-line">
        Here is your response, rendered in the current palette.
      </p>

      {/* Faux user bubble — mirrors .lmchat-bubble-user tint */}
      <div className="lmchat-preview-user-bubble">
        How does this look?
      </div>

      {/* Copper accent action button — decorative showcase */}
      <button type="button" className="lmchat-preview-action-btn" tabIndex={-1}>
        Continue
      </button>
    </div>
  );
}

// ─── Main section ─────────────────────────────────────────────────────────────

export function AppearanceSection() {
  const { theme, setTheme } = useThemeStore();
  const { textSize, density, messageStyle, setTextSize, setDensity, setMessageStyle } =
    useDisplayStore();

  return (
    <div
      className="lmchat-section-container"
      data-testid="settings-appearance-section"
    >
      <div className="lmchat-meta-block">

        {/* ── Row 1: Theme ───────────────────────────────────────────────────── */}
        <div className="lmchat-appearance-row">
          <span className="lmchat-appearance-row__label">Theme</span>
          <span className="lmchat-appearance-row__hint">
            Dark, light, or follow the OS setting.
          </span>
          <div className="lmchat-appearance-row__control">
            <div className="lmchat-appearance-theme-cluster">
              <div className="lmchat-appearance-theme-btns" role="group" aria-label="Theme">
                {THEMES.map((t) => (
                  <button
                    key={t.value}
                    type="button"
                    aria-pressed={theme === t.value}
                    onClick={(e) => {
                      // Pass click origin for the View Transitions
                      // circular-wipe — matches the existing AppearanceSection pattern.
                      const rect = e.currentTarget.getBoundingClientRect();
                      setTheme(t.value, {
                        x: rect.left + rect.width / 2,
                        y: rect.top + rect.height / 2,
                      });
                    }}
                    className={[
                      "lmchat-theme-btn",
                      theme === t.value ? "lmchat-theme-btn--active" : "",
                    ]
                      .filter(Boolean)
                      .join(" ")}
                  >
                    {t.label}
                  </button>
                ))}
              </div>

              <AppearanceThemePreview />
            </div>
          </div>
        </div>

        {/* ── Row 2: Text size ───────────────────────────────────────────────── */}
        <div className="lmchat-appearance-row">
          <span className="lmchat-appearance-row__label">Text size</span>
          <span className="lmchat-appearance-row__hint">
            Scales the type system globally.
          </span>
          <div className="lmchat-appearance-row__control">
            <SegmentedGroup
              options={TEXT_SIZES}
              current={textSize}
              onSelect={setTextSize}
              groupLabel="Text size"
            />
          </div>
        </div>

        {/* ── Row 3: Chat density ────────────────────────────────────────────── */}
        <div className="lmchat-appearance-row">
          <span className="lmchat-appearance-row__label">Chat density</span>
          <span className="lmchat-appearance-row__hint">
            Comfortable gives more breathing room between turns.
          </span>
          <div className="lmchat-appearance-row__control">
            <SegmentedGroup
              options={DENSITIES}
              current={density}
              onSelect={setDensity}
              groupLabel="Chat density"
            />
          </div>
        </div>

        {/* ── Row 4: Message style ───────────────────────────────────────────── */}
        <div className="lmchat-appearance-row">
          <span className="lmchat-appearance-row__label">Message style</span>
          <span className="lmchat-appearance-row__hint">
            Bubbles tint user messages; flat removes the surface.
          </span>
          <div className="lmchat-appearance-row__control">
            <SegmentedGroup
              options={MESSAGE_STYLES}
              current={messageStyle}
              onSelect={setMessageStyle}
              groupLabel="Message style"
            />
          </div>
        </div>

      </div>
    </div>
  );
}
