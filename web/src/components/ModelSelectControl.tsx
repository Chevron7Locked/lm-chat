/* SPDX-License-Identifier: Apache-2.0 */
/**
 * ModelSelectControl — the canonical model-picker `<select>` used at
 * every model-selection surface across the app.
 *
 * Canonical model-picker component that consolidates
 * the three live model-picker sites that previously inlined ad-hoc
 * `<select>` markup with subtly different typography and padding:
 *
 *   1. ``pages/Chat.tsx``         — chat-header picker, optgroups
 *      (Loaded / Not loaded), controlled.
 *   2. ``components/ChatSection.tsx`` — Default-model in Settings →
 *      Chat, flat options, uncontrolled (``defaultValue``; the control
 *      tracks the live selection locally so the capability icons stay
 *      current).
 *   3. ``components/LmStudioSection.tsx`` — Default-model in Settings
 *      → LM Studio, flat options + "(unloaded)" label suffix,
 *      controlled.
 *
 * The component is intentionally a thin wrapper over a real ``<select>``
 * — no portal-driven popover, no custom keyboard handling — so the
 * "real dropdown, no defaulting" rule is honored. The visual
 * unification is purely typographic via the shared CSS class
 * ``lmchat-model-select``.
 *
 * The design requires lucide-react Eye/Wrench/Brain capability icons.
 * Native <option> elements cannot render SVGs, so text tokens ([V][T][R])
 * are retained inside the dropdown for accessibility. The icons are
 * rendered via the exported ``ModelCapabilityIcons`` companion component,
 * intended to be mounted adjacent to the select trigger.
 */
import { useState, type ChangeEvent, type CSSProperties, type JSX } from "react";
import { Eye, Wrench, Brain } from "lucide-react";
import type { ModelCapabilities } from "@/hooks/useModels";

/**
 * ModelCapabilityIcons — lucide-react icons for a model's capabilities.
 *
 * Eye (vision), Wrench (trained_for_tool_use), Brain (reasoning).
 * Mounted inside ModelSelectControl adjacent to the <select> trigger
 * (in an inline-flex wrapper) so every picker surface shows the icons
 * for the currently-selected model automatically.
 * Returns null when caps is null/undefined or no capabilities are active.
 */
export function ModelCapabilityIcons({
  caps,
  size = 12,
  className,
  "data-testid": testId,
}: {
  caps: ModelCapabilities | null | undefined;
  size?: number;
  className?: string;
  "data-testid"?: string;
}): JSX.Element | null {
  if (!caps) return null;
  const hasVision = caps.vision;
  const hasTool = caps.trained_for_tool_use;
  const hasReasoning = caps.reasoning !== null;
  if (!hasVision && !hasTool && !hasReasoning) return null;
  return (
    <span
      className={["lmchat-model-cap-icons", className].filter(Boolean).join(" ")}
      aria-label={[
        hasVision ? "vision" : null,
        hasTool ? "tool use" : null,
        hasReasoning ? "reasoning" : null,
      ]
        .filter(Boolean)
        .join(", ")}
      data-testid={testId}
    >
      {hasVision && (
        <Eye
          size={size}
          aria-label="vision"
          aria-hidden={false}
          style={{ flexShrink: 0 }}
          data-testid="cap-icon-vision"
        />
      )}
      {hasTool && (
        <Wrench
          size={size}
          aria-label="tool use"
          aria-hidden={false}
          style={{ flexShrink: 0 }}
          data-testid="cap-icon-tool"
        />
      )}
      {hasReasoning && (
        <Brain
          size={size}
          aria-label="reasoning"
          aria-hidden={false}
          style={{ flexShrink: 0 }}
          data-testid="cap-icon-reasoning"
        />
      )}
    </span>
  );
}

/** One model option. */
export interface ModelOption {
  /**
   * Stable id used as the ``<option value>``.
   * When multi-provider groups are in use, this is the composite
   * "<provider>::<model_id>" string. When used without groups (LM Studio only),
   * this is the plain model key — same as before.
   */
  id: string;
  /** Human-readable label. */
  label: string;
  /**
   * Whether the model is currently loaded in LM Studio. When at least
   * one option carries ``loaded: true`` AND at least one carries
   * ``loaded: false``, the component splits the options into two
   * `<optgroup>` blocks ("Loaded" / "Not loaded"). Otherwise it renders
   * a flat option list.
   */
  loaded?: boolean;
  /**
   * Optional capability flags used to append capability glyphs to the
   * option label: Eye (vision), Wrench (trained_for_tool_use),
   * Brain (reasoning).
   * In a native <select>/<option> these are rendered as text abbreviations:
   * [V] = vision, [T] = tool_use, [R] = reasoning.
   */
  capabilities?: ModelCapabilities | undefined;
  /** Provider slug for grouping. Used when the `groups` prop is set. */
  provider?: string;
}

/** One provider group for grouped rendering. */
export interface ModelOptionGroup {
  provider: string;
  label: string;
  options: ModelOption[];
}

export type ModelSelectControlProps = {
  /** Required for a11y. */
  ariaLabel: string;
  /** Options to render. */
  options: ModelOption[];
  /**
   * Optional grouped rendering — one optgroup per provider.
   * When provided, renders provider-grouped optgroups (LM Studio with
   * loaded/unloaded sub-grouping; others flat). When absent, falls back
   * to the existing flat / loaded-split behaviour.
   */
  groups?: ModelOptionGroup[];
  /**
   * While true the select renders a single disabled "Loading models…"
   * option and gains the ``lmchat-model-select--loading`` class for a
   * CSS-only pulse (opacity breath via @keyframes).
   */
  isLoading?: boolean;
  /**
   * Text for the disabled placeholder option that appears as the
   * first entry. Pass an empty string to omit the placeholder.
   */
  placeholder?: string;
  /** Optional extra className merged with the canonical class. */
  className?: string;
  /** Optional ``data-testid`` for Playwright + vitest selectors. */
  testId?: string;
  /** Optional ``title`` attribute (native browser tooltip). */
  title?: string;
  /** Optional ``id`` (used when a sibling ``<label htmlFor>`` exists). */
  id?: string;
  /** Optional inline style escape hatch. Avoid; prefer ``className``. */
  style?: CSSProperties;
} & (
  | {
      /** Controlled mode — caller supplies value + onChange. */
      value: string;
      onChange: (value: string) => void;
      defaultValue?: never;
    }
  | {
      /** Uncontrolled mode — caller supplies defaultValue only. */
      defaultValue: string;
      value?: never;
      onChange?: never;
    }
);

export function ModelSelectControl(
  props: ModelSelectControlProps,
): JSX.Element {
  const {
    ariaLabel,
    options,
    groups,
    isLoading = false,
    placeholder,
    className,
    testId,
    title,
    id,
    style,
  } = props;

  const hasLoadedSplit =
    !isLoading &&
    options.some((o) => o.loaded === true) &&
    options.some((o) => o.loaded === false);

  const mergedClass = [
    "lmchat-model-select",
    isLoading ? "lmchat-model-select--loading" : "",
    className,
  ]
    .filter((c): c is string => typeof c === "string" && c !== "")
    .join(" ");

  // Single switch on controlled-vs-uncontrolled — avoid a per-render
  // ternary in the JSX that React-DOM would warn about
  // ("changing controlled/uncontrolled inputs").
  const isControlled = "value" in props;
  // In uncontrolled mode, deriving the icon row from ``defaultValue`` alone
  // freezes the icons at the mount-time selection. Track the live selection
  // in local state synced via onChange (the select itself stays uncontrolled —
  // we only observe the change, we don't feed ``value`` back).
  const [uncontrolledSelection, setUncontrolledSelection] = useState<
    string | null
  >(null);
  const currentValue = isControlled
    ? (props as { value: string }).value
    : (uncontrolledSelection ??
      (props as { defaultValue: string }).defaultValue);
  const selectProps = isControlled
    ? {
        value: currentValue,
        onChange: (e: ChangeEvent<HTMLSelectElement>) => {
          (props as { onChange: (v: string) => void }).onChange(e.target.value);
        },
      }
    : {
        defaultValue: (props as { defaultValue: string }).defaultValue,
        onChange: (e: ChangeEvent<HTMLSelectElement>) => {
          setUncontrolledSelection(e.target.value);
        },
      };

  /**
   * Capability glyph suffix.
   * Eye=vision, Wrench=trained_for_tool_use, Brain=reasoning.
   * Native <option> elements can't render SVGs; use compact text tokens.
   * Only non-false capabilities are included to keep labels clean on
   * models that have no special capabilities.
   */
  const capabilityGlyphs = (caps: ModelCapabilities | undefined): string => {
    if (!caps) return "";
    const parts: string[] = [];
    if (caps.vision) parts.push("V");
    if (caps.trained_for_tool_use) parts.push("T");
    if (caps.reasoning !== null) parts.push("R");
    return parts.length > 0 ? ` [${parts.join("")}]` : "";
  };

  const renderOption = (o: ModelOption): JSX.Element => (
    <option key={o.id} value={o.id}>
      {`${o.label}${capabilityGlyphs(o.capabilities)}`}
    </option>
  );

  /**
   * lucide-react icons (Eye/Wrench/Brain) render in the production UI,
   * not just in tests. We mount ModelCapabilityIcons
   * adjacent to the <select> inside an inline-flex wrapper. The icons
   * reflect the currently-selected option's capabilities so all consumer
   * sites (Chat.tsx TopBar, ChatSection.tsx, LmStudioSection.tsx) get
   * the icons automatically without any consumer code changes.
   */
  const selectedCaps = options.find((o) => o.id === currentValue)?.capabilities;

  return (
    // Layout lives in the .lmchat-model-select-wrap / .lmchat-model-cap-icons
    // CSS rules in chat.css — the wrap is the flex child of the topbar rows,
    // so the flex constraints that would otherwise sit on the bare <select>
    // live there.
    <span
      className="lmchat-model-select-wrap"
      data-testid={testId !== undefined ? `${testId}-wrap` : undefined}
    >
      <select
        aria-label={ariaLabel}
        id={id}
        className={mergedClass}
        data-testid={testId}
        title={title}
        style={style}
        disabled={isLoading || undefined}
        {...selectProps}
      >
        {isLoading ? (
          <option value="" disabled>
            Loading models…
          </option>
        ) : (
          <>
            {placeholder !== undefined && placeholder !== "" && (
              <option value="" disabled>
                {placeholder}
              </option>
            )}
            {groups !== undefined && groups.length > 0 ? (
              // Multi-provider grouped rendering: one optgroup per provider.
              // For the LM Studio group, apply loaded/unloaded sub-grouping.
              // Other provider groups render flat (all options are "available"
              // from the cloud perspective — no local loaded/unloaded concept).
              groups.map((group) => {
                const lmStudioGroup = group.provider === "lmstudio";
                const hasLmSplit =
                  lmStudioGroup &&
                  group.options.some((o) => o.loaded === true) &&
                  group.options.some((o) => o.loaded === false);

                return (
                  <optgroup key={group.provider} label={group.label}>
                    {hasLmSplit ? (
                      <>
                        {group.options.filter((o) => o.loaded === true).map(renderOption)}
                        {group.options.filter((o) => o.loaded === false).map((o) => (
                          <option key={o.id} value={o.id} style={{ opacity: 0.6 }}>
                            {`${o.label}${capabilityGlyphs(o.capabilities)}`}
                          </option>
                        ))}
                      </>
                    ) : (
                      group.options.map(renderOption)
                    )}
                  </optgroup>
                );
              })
            ) : hasLoadedSplit ? (
              <>
                <optgroup label="Loaded">
                  {options.filter((o) => o.loaded === true).map(renderOption)}
                </optgroup>
                <optgroup label="Not loaded">
                  {options.filter((o) => o.loaded === false).map(renderOption)}
                </optgroup>
              </>
            ) : (
              options.map(renderOption)
            )}
          </>
        )}
      </select>
      {/* Icons render at 14px — 12px is too small for the Brain glyph to
          read. The mobile topbar wrap caps at 138px with flex-shrink:0 on
          the icon row and min-width:0 on the select, so the select text
          truncates gracefully; the bar itself cannot overflow. */}
      <ModelCapabilityIcons
        caps={selectedCaps ?? null}
        size={14}
        {...(testId !== undefined
          ? { "data-testid": `${testId}-cap-icons` }
          : {})}
      />
    </span>
  );
}
