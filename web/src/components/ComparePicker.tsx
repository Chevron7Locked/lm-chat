/* SPDX-License-Identifier: Apache-2.0 */
/**
 * ComparePicker — /compare slash-command popover: two model selects +
 * confirm/cancel, anchored above the composer bar.
 *
 * Extracted from Composer.tsx — behavior-preserving; JSX moved verbatim.
 * Composer keeps ownership of showComparePicker/compareModelA/compareModelB
 * (the "compare" case in dispatchSlashCommand writes them too) and passes
 * them down as a controlled component; onConfirm/onCancel carry the exact
 * side-effects (close picker, clear text, refocus) the inline handlers used
 * to run directly in Composer.
 */
import { X } from "lucide-react";
import { ModelSelectControl } from "@/components/ModelSelectControl";

interface ComparePickerProps {
  modelOptions: { id: string; label: string; loaded: boolean }[];
  modelA: string;
  modelB: string;
  onChangeA: (id: string) => void;
  onChangeB: (id: string) => void;
  onCancel: () => void;
  onConfirm: (modelA: string, modelB: string) => void;
}

export function ComparePicker({
  modelOptions,
  modelA,
  modelB,
  onChangeA,
  onChangeB,
  onCancel,
  onConfirm,
}: ComparePickerProps) {
  const bothChosen = modelA !== "" && modelB !== "" && modelA !== modelB;
  return (
    <div
      className="lmchat-compare-picker"
      role="dialog"
      aria-label="Compare two models"
      aria-modal="true"
      data-testid="compare-picker"
    >
      <div className="lmchat-compare-picker__header">
        <span className="lmchat-compare-picker__title">Compare models</span>
        <button
          type="button"
          className="lmchat-compare-picker__close"
          aria-label="Cancel comparison"
          onClick={onCancel}
        >
          <X size={14} aria-hidden="true" />
        </button>
      </div>
      <div className="lmchat-compare-picker__body">
        <label className="lmchat-compare-picker__label" htmlFor="compare-model-a">
          Model A
        </label>
        <ModelSelectControl
          id="compare-model-a"
          ariaLabel="Model A"
          options={modelOptions}
          value={modelA}
          placeholder="Choose model A…"
          onChange={onChangeA}
          testId="compare-model-a-select"
        />
        <label className="lmchat-compare-picker__label" htmlFor="compare-model-b">
          Model B
        </label>
        <ModelSelectControl
          id="compare-model-b"
          ariaLabel="Model B"
          options={modelOptions}
          value={modelB}
          placeholder="Choose model B…"
          onChange={onChangeB}
          testId="compare-model-b-select"
        />
        {modelA !== "" && modelB !== "" && modelA === modelB && (
          <p className="lmchat-compare-picker__warn" role="alert">
            Pick two different models.
          </p>
        )}
      </div>
      <div className="lmchat-compare-picker__footer">
        <button
          type="button"
          className="lmchat-compare-picker__cancel"
          onClick={onCancel}
        >
          Cancel
        </button>
        <button
          type="button"
          className="lmchat-compare-picker__confirm"
          disabled={!bothChosen}
          data-testid="compare-picker-confirm"
          onClick={() => {
            if (!bothChosen) return;
            onConfirm(modelA, modelB);
          }}
        >
          Start comparison
        </button>
      </div>
    </div>
  );
}
