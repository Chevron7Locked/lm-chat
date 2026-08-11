/* SPDX-License-Identifier: Apache-2.0 */
import { useEffect, useRef, useId } from "react";
import { useFocusTrap } from "@/hooks/useFocusTrap";

// ─── ConfirmDialog ───────────────────────────────────────────────────────────

interface ConfirmDialogProps {
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({ message, onConfirm, onCancel }: ConfirmDialogProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const confirmBtnRef = useRef<HTMLButtonElement>(null);
  const titleId = useId();
  const messageId = useId();

  // WCAG 2.1.2 + 4.1.2: trap focus inside the dialog; focus the destructive
  // button on open (standard for destructive confirm dialogs — user must
  // deliberately reach Cancel). Restore focus to trigger on close.
  useFocusTrap(dialogRef, true, { initialFocusRef: confirmBtnRef });

  // Close on Esc.
  useEffect(() => {
    function onKey(e: KeyboardEvent): void {
      if (e.key === "Escape") {
        e.preventDefault();
        onCancel();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [onCancel]);

  return (
    <div className="lmchat-overlay">
      <div
        ref={dialogRef}
        className="lmchat-confirm-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={messageId}
      >
        <h2 id={titleId} className="lmchat-confirm-dialog__title">
          Confirm
        </h2>
        <p id={messageId} className="lmchat-confirm-dialog__message">
          {message}
        </p>
        <div className="lmchat-confirm-dialog__actions">
          <button
            type="button"
            onClick={onCancel}
            className="lmchat-confirm-cancel"
          >
            Cancel
          </button>
          <button
            ref={confirmBtnRef}
            type="button"
            onClick={onConfirm}
            className="lmchat-confirm-btn"
          >
            Confirm
          </button>
        </div>
      </div>
    </div>
  );
}
