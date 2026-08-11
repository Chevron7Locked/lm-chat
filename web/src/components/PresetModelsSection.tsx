/* SPDX-License-Identifier: Apache-2.0 */
/**
 * PresetModelsSection — Settings → Preset models.
 *
 * Lets the admin assign a default model+provider to each system-prompt
 * preset. Sub-session slash commands (/research, /code, …) pick up that
 * assignment instead of always using the top-bar model.
 *
 * Design:
 *   - One row per preset (6 total, from PRESET_LIST).
 *   - Each row: preset label (left) + provider-grouped model dropdown (right).
 *   - Dropdown reuses ModelSelectControl with the `groups` from
 *     useChatModelOptions and the composite "<provider>::<model_id>" encoding.
 *   - First option: "Use the chat's model" (value = "") — selects it clears
 *     the entry.
 *   - Changes fire PUT /api/settings/preset-models immediately (no save
 *     button needed — every row change is persisted in-place).
 *
 * CSS: all classes from settings.css — lmchat-section-container,
 * lmchat-section-description, lmchat-field-row, lmchat-field-row-label,
 * lmchat-select, lmchat-field-hint.  No new classes added.
 */
import { useMemo, type JSX } from "react";
import { useToast } from "@/stores/toastStore";
import { PRESET_LIST } from "@/lib/presets";
import { useChatModelOptions } from "@/hooks/useChatModelOptions";
import { usePresetModels, useSetPresetModels } from "@/hooks/usePresetModels";
import type { PresetModelsMap } from "@/hooks/usePresetModels";
import {
  ModelSelectControl,
  type ModelOption,
  type ModelOptionGroup,
} from "@/components/ModelSelectControl";
import "@/styles/settings.css";

/**
 * Synthetic provider slug for the "saved but not loaded" optgroup. Chosen so
 * it never collides with a real provider slug and so ModelSelectControl's
 * `provider === "lmstudio"` loaded/unloaded split does not apply to it.
 */
const STALE_PROVIDER = "__unavailable__";

// ─── Component ───────────────────────────────────────────────────────────────

export function PresetModelsSection(): JSX.Element {
  const { push } = useToast();
  const { data: presetModels, isLoading: pmLoading } = usePresetModels();
  const { groups, isLoading: modelsLoading } = useChatModelOptions();
  const setMutation = useSetPresetModels();

  const isLoading = pmLoading || modelsLoading;

  /**
   * For each preset, derive the composite value to pre-populate the select:
   *   "<provider>::<model_id>" when a mapping exists, "" otherwise.
   *
   * Memoized over both presetModels and PRESET_LIST so the values stay
   * stable across model-list refetches.
   */
  const compositeValues = useMemo<Record<string, string>>(() => {
    const map: Record<string, string> = {};
    for (const preset of PRESET_LIST) {
      const entry = presetModels?.[preset.id];
      map[preset.id] =
        entry !== undefined ? `${entry.provider}::${entry.model_id}` : "";
    }
    return map;
  }, [presetModels]);

  /**
   * The grouped options from useChatModelOptions carry BARE model ids; the
   * select uses composite "<provider>::<model_id>" option values (so onChange
   * can recover the provider). Map them exactly like Chat.tsx's TopBar does —
   * without this, cloud options render with bare ids that never match the
   * composite `value`, and selecting one saves provider="lmstudio".
   */
  const compositeGroups = useMemo(
    () =>
      groups.map((g) => ({
        ...g,
        options: g.options.map((o) => ({ ...o, id: `${o.provider}::${o.id}` })),
      })),
    [groups],
  );

  /**
   * A saved preset can point at a model that is no longer in the loaded
   * catalog — the admin picked it, then unloaded/removed/renamed that model in
   * LM Studio. A native `<select>` can't render a `value` matching none of its
   * `<option>`s, so it silently falls back to showing its FIRST option —
   * misrepresenting a stale preset as a different, valid model. (At run time
   * the backend resolves the stale id to a loaded fallback, so routing never
   * breaks; only the settings display lied.) Surface each such value as an
   * explicit "not loaded" option so the row shows the truth.
   *
   * Same stale-model class the chat header handles by auto-switching the pin
   * (Chat.tsx, 45f81ca) — but a preset is durable admin config we must NOT
   * silently rewrite on page-open, and it degrades gracefully server-side, so
   * here we show it truthfully and let the admin re-pick, rather than mutate.
   */
  const groupsWithStale = useMemo<ModelOptionGroup[]>(() => {
    const known = new Set<string>();
    for (const g of compositeGroups)
      for (const o of g.options) known.add(o.id);

    const seen = new Set<string>();
    const stale: ModelOption[] = [];
    for (const preset of PRESET_LIST) {
      const value = compositeValues[preset.id];
      if (!value || known.has(value) || seen.has(value)) continue;
      seen.add(value);
      // value is "<provider>::<model_id>" — show the bare model id.
      const sepIdx = value.indexOf("::");
      const modelId = sepIdx >= 0 ? value.slice(sepIdx + 2) : value;
      stale.push({
        id: value,
        label: `${modelId} — not loaded`,
        provider: STALE_PROVIDER,
      });
    }
    if (stale.length === 0) return compositeGroups;
    return [
      ...compositeGroups,
      { provider: STALE_PROVIDER, label: "Saved but not loaded", options: stale },
    ];
  }, [compositeGroups, compositeValues]);

  /**
   * Handle a selection change for a given preset id.
   * compositeId is either "" (clear) or "<provider>::<model_id>".
   * Builds a new full mapping and fires PUT immediately.
   */
  function handleChange(presetId: string, compositeId: string): void {
    const current: PresetModelsMap = presetModels ?? {};

    if (compositeId === "") {
      // User selected "Use the chat's model" — drop this preset's entry.
      // Object rest-destructuring yields every entry EXCEPT presetId, which
      // is exactly the pruned map to persist.
      const { [presetId]: _removed, ...rest } = current;
      void _removed; // presetId intentionally dropped
      setMutation.mutate(rest, {
        onError: (err) => {
          push({
            variant: "error",
            message: err.detail ?? "Couldn't save preset model — try again.",
          });
        },
      });
      return;
    }

    // Split on the FIRST "::" only — mirrors Chat.tsx onModelChange exactly.
    const sepIdx = compositeId.indexOf("::");
    const provider = sepIdx >= 0 ? compositeId.slice(0, sepIdx) : "lmstudio";
    const model_id =
      sepIdx >= 0 ? compositeId.slice(sepIdx + 2) : compositeId;

    const updated: PresetModelsMap = {
      ...current,
      [presetId]: { provider, model_id },
    };

    setMutation.mutate(updated, {
      onError: (err) => {
        push({
          variant: "error",
          message: err.detail ?? "Couldn't save preset model — try again.",
        });
      },
    });
  }

  return (
    <div
      className="lmchat-section-container"
      data-testid="settings-preset-models-section"
    >
      <p className="lmchat-section-description">
        Assign a default model to each mode. When a slash command opens a
        sub-session, it will stream on that model instead of the top-bar model.
        Leave a row empty to keep using the chat&apos;s current model.
      </p>

      {isLoading ? (
        <p
          className="lmchat-section-description"
          data-testid="preset-models-loading"
        >
          Loading…
        </p>
      ) : (
        <div
          className="lmchat-form"
          data-testid="preset-models-list"
          style={{ marginTop: "var(--space-group)" }}
        >
          {PRESET_LIST.map((preset) => (
            <div
              key={preset.id}
              className="lmchat-field-row"
              data-testid={`preset-models-row-${preset.id}`}
            >
              <span
                className="lmchat-field-row-label"
                id={`preset-models-label-${preset.id}`}
              >
                {preset.label}
              </span>

              <ModelSelectControl
                ariaLabel={`Model for ${preset.label} preset`}
                id={`preset-models-select-${preset.id}`}
                options={[]}
                groups={groupsWithStale}
                value={compositeValues[preset.id] ?? ""}
                onChange={(compositeId) => {
                  handleChange(preset.id, compositeId);
                }}
                placeholder="Use the chat's model"
                className="lmchat-select"
                testId={`preset-models-select-${preset.id}`}
              />
            </div>
          ))}
        </div>
      )}

      <p
        className="lmchat-field-hint"
        style={{ marginTop: "var(--space-group)" }}
      >
        Cloud models route through their configured provider. Finalization
        always uses the local model.
      </p>
    </div>
  );
}
