/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Toast notification stack — Direction B: Ambient Bleed.
 *
 * Toasts render in the bottom-right corner above the chat footer.
 * Auto-dismiss is managed in toastStore; this component only renders the
 * current queue and wires the dismiss button.
 *
 * Composition: leading 6px dot → mono-uppercase label → Hubot Sans title →
 * body text → plain-text action. The surface dissolves to the right — no
 * hard container, just a warm atmospheric wash anchored on the left.
 *
 * Variants: info / success / warning / error
 */
import { X } from "lucide-react";
import { useToastStore } from "@/stores/toastStore";
import type { Toast, ToastVariant } from "@/stores/toastStore";
import "@/styles/toast.css";

// ─── Variant label map ───────────────────────────────────────────────────────

const VARIANT_LABEL: Record<ToastVariant, string> = {
  info: "info",
  success: "success",
  warning: "warning",
  error: "error",
};

// ─── ToastItem ──────────────────────────────────────────────────────────────

interface ToastItemProps {
  toast: Toast;
  onDismiss: (id: string) => void;
}

function ToastItem({ toast, onDismiss }: ToastItemProps) {
  const hasTitle = toast.title !== undefined;

  // role="alert" preempts the screen
  // reader and is reserved (per ARIA spec) for content requiring
  // immediate user attention. Success / info / warning toasts are
  // routine and should NOT preempt. Move role="alert" + assertive to
  // the error variant only; everything else uses role="status" with
  // polite live-region updates.
  const isError = toast.variant === "error";
  return (
    <div
      role={isError ? "alert" : "status"}
      aria-live={isError ? "assertive" : "polite"}
      className={`toast-item toast-item--${toast.variant}`}
    >
      {/* 6px leading dot — sole variant indicator */}
      <span aria-hidden="true" className="toast-dot" />

      {/* Body: label + title + message + action */}
      <span className="toast-body">
        {/* Mono-uppercase category label — always shown. A ×N badge appears
            only when the same toast has fired more than once (coalesced). */}
        <span className="toast-label">
          {VARIANT_LABEL[toast.variant]}
          {toast.count > 1 && (
            <span className="toast-count" aria-label={`Repeated ${String(toast.count)} times`}>
              {"×"}
              {toast.count}
            </span>
          )}
        </span>

        {/* Optional Hubot Sans display title */}
        {hasTitle && <span className="toast-title">{toast.title}</span>}

        {/* Message body */}
        <span
          className={`toast-message${hasTitle ? "" : " toast-message--solo"}`}
        >
          {toast.message}
        </span>

        {/* Optional plain-text action */}
        {toast.action !== undefined && (
          <span className="toast-action-row">
            <button
              type="button"
              className="toast-action"
              onClick={() => {
                toast.action?.onClick();
              }}
            >
              {toast.action.label}
            </button>
          </span>
        )}
      </span>

      {/* Dismiss — plain text / icon */}
      <button
        type="button"
        aria-label="Dismiss notification"
        onClick={() => {
          onDismiss(toast.id);
        }}
        className="toast-dismiss"
      >
        <X size={14} aria-hidden />
      </button>
    </div>
  );
}

// ─── ToastContainer ─────────────────────────────────────────────────────────

/**
 * Renders the toast stack fixed to the bottom-right corner.
 * Mount this once near the root of the React tree (e.g. in router.tsx or App).
 */
export function ToastContainer() {
  const toasts = useToastStore((s) => s.toasts);
  const dismiss = useToastStore((s) => s.dismiss);

  if (toasts.length === 0) return null;

  return (
    <div aria-label="Notifications" className="toast-container">
      {toasts.map((t) => (
        <div key={t.id} className="toast-item-wrap">
          <ToastItem toast={t} onDismiss={dismiss} />
        </div>
      ))}
    </div>
  );
}
