/* SPDX-License-Identifier: Apache-2.0 */
/**
 * LoginSecuritySection — merged "Login & Security" page.
 *
 * Combines AccountSection (identity + password change + sign-out) with
 * SecuritySettings (TOTP/2FA) into a single page, fulfilling contract §2A.
 */
import { AccountSection } from "@/components/AccountSection";
import { SecuritySettings } from "@/components/SecuritySettings";
import "@/styles/settings.css";

export function LoginSecuritySection() {
  return (
    <div
      className="lmchat-section-container"
      data-testid="settings-login-security-section"
    >
      {/* ── Account identity + password + sign-out ─────────────────────────── */}
      <AccountSection />

      {/* ── Two-factor authentication ──────────────────────────────────────── */}
      <hr className="lmchat-section-divider" aria-hidden="true" />
      <section
        className="lmchat-section"
        aria-label="Two-factor authentication"
      >
        <h3 className="lmchat-section-heading">Two-factor authentication</h3>
        <p className="lmchat-section-description">
          Add a second factor to your login. Use any TOTP authenticator app
          (1Password, Authy, Google Authenticator, Bitwarden, etc.).
        </p>
        <div style={{ marginTop: "var(--space-group-relaxed)" }}>
          <SecuritySettings />
        </div>
      </section>
    </div>
  );
}
