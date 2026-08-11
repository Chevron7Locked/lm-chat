/* SPDX-License-Identifier: Apache-2.0 */
/**
 * SecuritySettings — TOTP (two-factor) enrolment surface for Settings.tsx.
 *
 * Flow:
 *   1. User clicks "Enable two-factor authentication".
 *   2. UI calls POST /api/auth/totp/setup → receives { provisioning_uri,
 *      secret }. The raw secret is displayed in monospaced text along
 *      with the otpauth:// URI so the user can paste either into their
 *      authenticator app.
 *   3. User types the current 6-digit code and submits.
 *   4. UI calls POST /api/auth/totp/verify with the secret + code; on
 *      success the section flips to the "enabled" state.
 *   5. In the enabled state, the user can click "Disable" which prompts
 *      for the account password (re-auth) and posts to /api/auth/totp/disable.
 *
 * No QR-image rendering is performed; the otpauth:// URI + raw base32
 * secret are both displayed and most authenticator apps accept either.
 * A future QR image slots into the <ProvisioningArtifact /> section
 * without further refactor.
 *
 * Initial state is hydrated from `authStore.user.totp_enabled`. The backend
 * reports `totp_enabled` on both /api/auth/login and /api/auth/me so the
 * component renders the correct state on mount and survives page reload.
 * On successful verify the store's flag is flipped imperatively (no /me
 * re-fetch needed).
 *
 * Uses settings.css semantic classes rather than inline CSSProperties.
 * Spacing grammar applied:
 *   - Description → first button: GROUP-RELAXED via SecuritySection wrapper
 *   - Card internal gap: SIBLING (12px) via .lmchat-totp-card
 *   - KV row gap: SIBLING (12px) via .lmchat-totp-kv-row
 *   - Artifacts gap: GLUE-RELAXED (8px) via .lmchat-totp-artifacts
 */
import { useState } from "react";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";
import { useToast, useToastStore } from "@/stores/toastStore";
import "@/styles/settings.css";

// ─── Wire types ─────────────────────────────────────────────────────────────

interface TotpSetupResponse {
  provisioning_uri: string;
  secret: string;
}

type Stage =
  | { kind: "idle" } // no setup started yet
  | { kind: "setup"; uri: string; secret: string } // setup returned; awaiting verify
  | { kind: "enabled" } // verified in this session
  | { kind: "disabling" }; // disabling flow active

// ─── Component ──────────────────────────────────────────────────────────────

export function SecuritySettings() {
  const { push } = useToast();
  // Hydrate the initial stage from the auth store so a page reload
  // doesn't reset the UI to "not configured" when TOTP is actually on.
  // The store's `totp_enabled` is populated from /api/auth/me on mount
  // (and from /api/auth/login on first login).
  const totpEnabled = useAuthStore((s) => s.user?.totp_enabled ?? false);
  const setTotpEnabledStore = useAuthStore((s) => s.setTotpEnabled);
  const [stage, setStage] = useState<Stage>(() =>
    totpEnabled ? { kind: "enabled" } : { kind: "idle" },
  );
  const [verifyCode, setVerifyCode] = useState("");
  const [disablePassword, setDisablePassword] = useState("");
  const [submitting, setSubmitting] = useState(false);

  async function handleSetup(): Promise<void> {
    setSubmitting(true);
    try {
      const body = await api.request<TotpSetupResponse>(
        "/api/auth/totp/setup",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
        },
      );
      setStage({
        kind: "setup",
        uri: body.provisioning_uri,
        secret: body.secret,
      });
    } catch (err) {
      const apiErr = err as ApiError;
      console.error("[totp] setup failed:", apiErr.detail ?? apiErr.message);
      push({
        variant: "error",
        message:
          "Couldn't set up two-factor authentication — check the console for details.",
      });
    } finally {
      setSubmitting(false);
    }
  }

  async function handleVerify(): Promise<void> {
    if (stage.kind !== "setup") return;
    if (!/^\d{6}$/.test(verifyCode)) {
      push({
        variant: "warning",
        message: "Enter a 6-digit authenticator code.",
      });
      return;
    }
    setSubmitting(true);
    try {
      const params = new URLSearchParams();
      params.set("secret", stage.secret);
      params.set("code", verifyCode);
      await api.request<{ status: string }>("/api/auth/totp/verify", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: params.toString(),
      });
      setStage({ kind: "enabled" });
      setVerifyCode("");
      // Keep the auth store in sync so this component and other consumers
      // render the correct state without a /me re-fetch.
      setTotpEnabledStore(true);
      push({
        variant: "success",
        message: "Two-factor authentication enabled.",
      });
    } catch (err) {
      const apiErr = err as ApiError;
      console.error("[totp] verify failed:", apiErr.detail ?? apiErr.message);
      push({
        variant: "error",
        message:
          "That code didn't match — double-check your authenticator and try again.",
      });
    } finally {
      setSubmitting(false);
    }
  }

  async function handleDisable(): Promise<void> {
    if (stage.kind !== "disabling") return;
    if (disablePassword === "") {
      push({
        variant: "warning",
        message: "Enter your password to disable TOTP.",
      });
      return;
    }
    setSubmitting(true);
    try {
      const params = new URLSearchParams();
      params.set("password", disablePassword);
      await api.request<{ status: string }>("/api/auth/totp/disable", {
        method: "POST",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: params.toString(),
      });
      setStage({ kind: "idle" });
      setDisablePassword("");
      setTotpEnabledStore(false);
      push({
        variant: "success",
        message: "Two-factor authentication disabled.",
      });
    } catch (err) {
      const apiErr = err as ApiError;
      console.error("[totp] disable failed:", apiErr.detail ?? apiErr.message);
      push({
        variant: "error",
        message:
          "Couldn't disable two-factor authentication — check the console for details.",
      });
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <div className="lmchat-security-container">
      {stage.kind === "idle" && (
        <button
          type="button"
          onClick={() => {
            void handleSetup();
          }}
          disabled={submitting}
          className="lmchat-btn-primary"
          aria-label="Enable two-factor authentication"
        >
          {submitting ? "Starting…" : "Enable two-factor authentication"}
        </button>
      )}

      {stage.kind === "setup" && (
        <div className="lmchat-totp-card" aria-label="TOTP setup">
          <p className="lmchat-section-description" style={{ margin: 0 }}>
            Scan this URI with your authenticator, or enter the secret manually.
          </p>
          <ProvisioningArtifact uri={stage.uri} secret={stage.secret} />
          <div className="lmchat-totp-verify-row">
            <label className="lmchat-field-label" htmlFor="lmchat-totp-verify">
              Authenticator code
            </label>
            <input
              id="lmchat-totp-verify"
              type="text"
              inputMode="numeric"
              pattern="[0-9]{6}"
              maxLength={6}
              value={verifyCode}
              onChange={(e) => {
                setVerifyCode(e.target.value);
              }}
              placeholder="000 000"
              autoComplete="one-time-code"
              autoFocus
              className="lmchat-code-input"
            />
            <button
              type="button"
              onClick={() => {
                void handleVerify();
              }}
              disabled={submitting || verifyCode === ""}
              className="lmchat-btn-primary"
              aria-label="Verify TOTP code"
            >
              {submitting ? "Verifying…" : "Verify"}
            </button>
          </div>
          <button
            type="button"
            onClick={() => {
              setStage({ kind: "idle" });
              setVerifyCode("");
            }}
            className="lmchat-btn-ghost"
            aria-label="Cancel TOTP setup"
          >
            Cancel
          </button>
        </div>
      )}

      {stage.kind === "enabled" && (
        <div className="lmchat-totp-card" aria-label="TOTP enabled">
          <p
            className="lmchat-section-description"
            style={{ margin: 0, color: "var(--color-text)" }}
          >
            <strong>TOTP enabled.</strong> Your account will require a 6-digit
            code on every sign-in.
          </p>
          <button
            type="button"
            onClick={() => {
              setStage({ kind: "disabling" });
            }}
            className="lmchat-btn-danger"
            aria-label="Disable two-factor authentication"
          >
            Disable two-factor authentication
          </button>
        </div>
      )}

      {stage.kind === "disabling" && (
        <div className="lmchat-totp-card" aria-label="Disable TOTP">
          <p className="lmchat-section-description" style={{ margin: 0 }}>
            Confirm your password to disable two-factor authentication.
          </p>
          <div className="lmchat-totp-verify-row">
            <label
              className="lmchat-field-label"
              htmlFor="lmchat-totp-disable-pw"
            >
              Password
            </label>
            <input
              id="lmchat-totp-disable-pw"
              type="password"
              value={disablePassword}
              onChange={(e) => {
                setDisablePassword(e.target.value);
              }}
              autoComplete="current-password"
              className="lmchat-code-input"
            />
            <button
              type="button"
              onClick={() => {
                void handleDisable();
              }}
              disabled={submitting || disablePassword === ""}
              className="lmchat-btn-danger"
              aria-label="Confirm disable TOTP"
            >
              {submitting ? "Disabling…" : "Confirm disable"}
            </button>
          </div>
          <button
            type="button"
            onClick={() => {
              setStage({ kind: "enabled" });
              setDisablePassword("");
            }}
            className="lmchat-btn-ghost"
            aria-label="Cancel disable TOTP"
          >
            Cancel
          </button>
        </div>
      )}
    </div>
  );
}

// ─── Sub-components ─────────────────────────────────────────────────────────

function ProvisioningArtifact({
  uri,
  secret,
}: {
  uri: string;
  secret: string;
}) {
  async function copy(text: string, label: string): Promise<void> {
    try {
      await navigator.clipboard.writeText(text);
      pushSuccessToast(`${label} copied to clipboard.`);
    } catch {
      // Clipboard API can throw in non-secure contexts; the user can also
      // select-and-copy from the visible text.
    }
  }

  return (
    <div className="lmchat-totp-artifacts">
      <div className="lmchat-totp-kv-row">
        <span className="lmchat-field-label">Secret</span>
        <code className="lmchat-code-mono" aria-label="TOTP secret">
          {secret}
        </code>
        <button
          type="button"
          onClick={() => {
            void copy(secret, "Secret");
          }}
          className="lmchat-btn-ghost"
          aria-label="Copy TOTP secret"
        >
          Copy
        </button>
      </div>
      <div className="lmchat-totp-kv-row">
        <span className="lmchat-field-label">URI</span>
        <code
          className="lmchat-code-mono"
          style={{ fontSize: "var(--fs-micro)" }}
          aria-label="TOTP provisioning URI"
        >
          {uri}
        </code>
        <button
          type="button"
          onClick={() => {
            void copy(uri, "URI");
          }}
          className="lmchat-btn-ghost"
          aria-label="Copy provisioning URI"
        >
          Copy
        </button>
      </div>
    </div>
  );
}

// The inner ProvisioningArtifact already uses its parent's toast. To avoid
// double-wiring, reach into the store directly. toastStore is a leaf Zustand
// store (already statically imported above), so a synchronous getState() push
// is correct — the previous dynamic import split nothing (Vite flagged it as
// an ineffective dynamic import) and only deferred the toast by a microtask.
function pushSuccessToast(message: string): void {
  useToastStore.getState().push({ variant: "success", message });
}
