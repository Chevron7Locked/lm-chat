/* SPDX-License-Identifier: Apache-2.0 */
import { humanizeApiError } from "@/lib/errorMessages";
import type { StreamState } from "@/hooks/useSSE";

// ─── StreamErrorBanner — main-thread stream error banner ────────────────────
// Renders sseState.error with humanizeApiError, plus retry/dismiss/sign-in/
// settings actions. Includes the MTP-suspected dedupe check (only show the
// banner once per chat this session — mtpAlreadyShown is a pure read of the
// caller's mtpSuspectedShownRef, computed by the caller).

interface StreamErrorBannerProps {
  error: NonNullable<StreamState["error"]>;
  chatId: number;
  mtpAlreadyShown: boolean;
  canRetry: boolean;
  onRetry: () => void;
  onDismiss: () => void;
  onSignIn: () => void;
  onOpenSettings: () => void;
}

export function StreamErrorBanner({
  error,
  chatId,
  mtpAlreadyShown,
  canRetry,
  onRetry,
  onDismiss,
  onSignIn,
  onOpenSettings,
}: StreamErrorBannerProps) {
  const h = humanizeApiError(error);
  const code = error.code;
  // MTP-suspected dedupe: only show banner once per chat this session.
  // Ephemeral by design (resets on tab reload). The ref is a pure
  // READ here (mutation lives in the useEffect above; see the
  // ref declaration comment for the StrictMode rationale).
  const isMtpSuspected = code === "mtp_suspected";
  if (isMtpSuspected && mtpAlreadyShown) {
    console.info(
      `MTP-suspected banner already shown for chat ${String(chatId)}, suppressing duplicate`,
    );
    return null;
  }
  // Give the user
  // a concrete recovery path.  "Dismiss" calls stop() which
  // resets the stream state to idle and lets them retry from
  // the composer.  For settings-class errors (no models /
  // unreachable / bad key) we also surface a deep link.
  const showSettingsLink =
    code === "no_models_loaded" ||
    code === "lmstudio_unreachable" ||
    code === "invalid_api_key";
  // For 401/403 stream errors, the "Retry" button would always
  // fail — the session is already revoked. Replace it with a
  // "Sign in" affordance that navigates to /login.
  const isAuthError = code === "http_401" || code === "http_403";
  return (
    <div
      role="alert"
      className="lmchat-error-banner"
      data-testid="chat-stream-error"
    >
      <div className="lmchat-error-banner__title">
        {h.title}
      </div>
      <div>{h.body}</div>
      {h.hint !== undefined && (
        <div className="lmchat-error-banner__hint">
          {h.hint}
        </div>
      )}
      <div className="lmchat-error-actions">
        {isAuthError ? (
          /* Sign-in action replaces dead Retry for
             session-expired / revoked errors. */
          <button
            type="button"
            onClick={onSignIn}
            className="lmchat-error-btn-primary"
            data-testid="chat-stream-error-sign-in"
          >
            Sign in
          </button>
        ) : (
          /* Retry re-sends the last submitted message via the
             captured ref so the user doesn't have to re-type.
             Hidden when no prior submit exists. */
          canRetry && (
            <button
              type="button"
              onClick={onRetry}
              className="lmchat-error-btn-primary"
              data-testid="chat-stream-error-retry"
            >
              Retry
            </button>
          )
        )}
        {!isAuthError && (
          <button
            type="button"
            onClick={onDismiss}
            className={
              canRetry
                ? "lmchat-error-btn-secondary"
                : "lmchat-error-btn-primary"
            }
            data-testid="chat-stream-error-dismiss"
          >
            Dismiss
          </button>
        )}
        {showSettingsLink && (
          <button
            type="button"
            onClick={onOpenSettings}
            className="lmchat-error-btn-secondary"
            data-testid="chat-stream-error-open-settings"
          >
            Open LM Studio settings
          </button>
        )}
      </div>
    </div>
  );
}
