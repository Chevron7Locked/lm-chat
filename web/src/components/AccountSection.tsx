/* SPDX-License-Identifier: Apache-2.0 */
/**
 * AccountSection — Account tab content.
 *
 * Surfaces the authenticated user's identity and lets them change their
 * password.  Mounted by ``pages/Settings.tsx`` inside the tabbed shell.
 *
 * Spacing grammar applied:
 *   - Identity block → divider: CHAPTER via .lmchat-section-divider
 *   - Heading → description: GLUE-RELAXED (8px)
 *   - Description → form: GROUP-RELAXED (32px) via .lmchat-form margin-top
 *   - Field → field: GROUP (24px) via .lmchat-form gap
 *   - Label → input: GLUE (4px) via .lmchat-field gap
 *   - End of form → divider: CHAPTER via .lmchat-section-divider
 *   - Divider → sign-out: CHAPTER implicit in divider margin
 */
import { useEffect, useState, type ReactNode, type SubmitEvent } from "react";
import { useNavigate } from "react-router-dom";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";
import { useToast } from "@/stores/toastStore";
import "@/styles/settings.css";

export function AccountSection() {
  const { user, logout } = useAuthStore();
  const { push } = useToast();
  const navigate = useNavigate();

  // ─── Change-password form state ─────────────────────────────────────────
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [pwSubmitting, setPwSubmitting] = useState(false);
  const [pwError, setPwError] = useState<string | null>(null);

  // Two-step sign-out: first click shows confirm, second commits.
  const [signOutConfirming, setSignOutConfirming] = useState(false);
  useEffect(() => {
    if (!signOutConfirming) return;
    const t = setTimeout(() => {
      setSignOutConfirming(false);
    }, 5_000);
    return () => {
      clearTimeout(t);
    };
  }, [signOutConfirming]);

  async function handleLogout(): Promise<void> {
    try {
      await logout();
      void navigate("/login");
    } catch {
      push({ variant: "error", message: "Couldn't sign out — try again." });
    }
  }

  function resetPasswordForm(): void {
    setOldPassword("");
    setNewPassword("");
    setConfirmPassword("");
    setPwError(null);
  }

  async function handleChangePassword(
    event: SubmitEvent<HTMLFormElement>,
  ): Promise<void> {
    event.preventDefault();
    setPwError(null);

    if (oldPassword === "" || newPassword === "" || confirmPassword === "") {
      setPwError("All password fields are required.");
      return;
    }
    if (newPassword !== confirmPassword) {
      setPwError("Passwords do not match.");
      return;
    }
    if (newPassword === oldPassword) {
      setPwError("New password must differ from the current password.");
      return;
    }

    setPwSubmitting(true);
    try {
      await api.postForm<{ status: string }>("/api/auth/password", {
        old_password: oldPassword,
        new_password: newPassword,
      });
      resetPasswordForm();
      push({ variant: "success", message: "Password changed." });
    } catch (err) {
      const apiErr = err as ApiError;
      setPwError(apiErr.detail ?? "Password change failed.");
    } finally {
      setPwSubmitting(false);
    }
  }

  if (user === null) {
    return <p className="lmchat-meta-value">Sign in to manage your account.</p>;
  }

  return (
    <div
      className="lmchat-section-container"
      data-testid="settings-account-section"
    >
      {/* ── Identity read-out ─────────────────────────────────────────────── */}
      <section className="lmchat-section" aria-label="Account identity">
        <div className="lmchat-meta-block">
          <div className="lmchat-meta-row">
            <span className="lmchat-meta-label">Username</span>
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                gap: "var(--space-glue-relaxed)",
              }}
            >
              <span className="lmchat-meta-value">{user.username}</span>
              <CopyUsernameButton value={user.username} />
            </span>
          </div>
          {user.is_admin && (
            <div className="lmchat-meta-row">
              <span className="lmchat-meta-label">Role</span>
              <span className="lmchat-meta-value lmchat-meta-value--accent">
                Admin
              </span>
            </div>
          )}
        </div>
      </section>

      {/* ── Ink-bleed divider — CHAPTER (48px) above + below ──────────────── */}
      <hr className="lmchat-section-divider" aria-hidden="true" />

      {/* ── Change-password form ────────────────────────────────────────────── */}
      <section className="lmchat-section" aria-label="Change password">
        <h3 className="lmchat-section-heading">Change password</h3>
        <p className="lmchat-section-description">
          Changing your password keeps you signed in on this device and signs
          out any other browsers where you were logged in.
        </p>
        <form
          onSubmit={(e) => {
            void handleChangePassword(e);
          }}
          className="lmchat-form"
          data-testid="account-change-password-form"
          aria-describedby={pwError !== null ? "account-pw-error" : undefined}
          noValidate
        >
          <div className="lmchat-field">
            <label
              htmlFor="account-old-password"
              className="lmchat-field-label"
            >
              Current password
            </label>
            <input
              id="account-old-password"
              type="password"
              value={oldPassword}
              onChange={(e) => {
                setOldPassword(e.target.value);
              }}
              autoComplete="current-password"
              required
              className="lmchat-input"
              data-testid="account-old-password"
            />
          </div>

          <div className="lmchat-field">
            <label
              htmlFor="account-new-password"
              className="lmchat-field-label"
            >
              New password
            </label>
            <input
              id="account-new-password"
              type="password"
              value={newPassword}
              onChange={(e) => {
                setNewPassword(e.target.value);
              }}
              autoComplete="new-password"
              required
              minLength={8}
              maxLength={4096}
              className="lmchat-input"
              data-testid="account-new-password"
            />
          </div>

          <div className="lmchat-field">
            <label
              htmlFor="account-confirm-password"
              className="lmchat-field-label"
            >
              Confirm new password
            </label>
            <input
              id="account-confirm-password"
              type="password"
              value={confirmPassword}
              onChange={(e) => {
                setConfirmPassword(e.target.value);
              }}
              autoComplete="new-password"
              required
              minLength={8}
              maxLength={4096}
              className="lmchat-input"
              data-testid="account-confirm-password"
            />
          </div>

          {pwError !== null && (
            <p
              id="account-pw-error"
              role="alert"
              className="lmchat-form-error"
              data-testid="account-pw-error"
            >
              {pwError}
            </p>
          )}

          <div className="lmchat-form-actions">
            <button
              type="submit"
              disabled={pwSubmitting}
              className="lmchat-btn-primary"
              data-testid="account-change-password-submit"
            >
              {pwSubmitting ? "Saving…" : "Change password"}
            </button>
          </div>
        </form>
      </section>

      {/* ── Ink-bleed divider — CHAPTER (48px) above + below ──────────────── */}
      <hr className="lmchat-section-divider" aria-hidden="true" />

      {/* ── Sign out — foxing-tint button (quiet danger register) ─────────── */}
      <section className="lmchat-section" aria-label="Session">
        {signOutConfirming ? (
          <div className="lmchat-signout-cluster">
            <span role="status" className="lmchat-signout-prompt">
              Sign out of LM Chat?
            </span>
            <div className="lmchat-signout-actions">
              <button
                type="button"
                onClick={() => {
                  void handleLogout();
                }}
                className="lmchat-btn-signout"
                data-testid="settings-account-signout-confirm"
                autoFocus
              >
                Yes, sign out
              </button>
              <button
                type="button"
                onClick={() => {
                  setSignOutConfirming(false);
                }}
                className="lmchat-btn-secondary"
                data-testid="settings-account-signout-cancel"
              >
                Cancel
              </button>
            </div>
          </div>
        ) : (
          <button
            type="button"
            onClick={() => {
              setSignOutConfirming(true);
            }}
            className="lmchat-btn-signout"
            data-testid="settings-account-signout"
          >
            Sign out
          </button>
        )}
      </section>
    </div>
  );
}

function CopyUsernameButton({ value }: { value: string }): ReactNode {
  const [copied, setCopied] = useState(false);
  async function handleCopy(): Promise<void> {
    try {
      const clip = navigator.clipboard as unknown as Clipboard | undefined;
      if (clip !== undefined) {
        await clip.writeText(value);
        setCopied(true);
        setTimeout(() => {
          setCopied(false);
        }, 1500);
      }
    } catch {
      // No-op — older browsers without Clipboard API just won't copy.
    }
  }
  return (
    <button
      type="button"
      onClick={() => {
        void handleCopy();
      }}
      aria-label="Copy username"
      title="Copy username"
      data-testid="account-username-copy"
      className="lmchat-copy-btn"
    >
      {copied ? "Copied!" : "Copy"}
    </button>
  );
}
