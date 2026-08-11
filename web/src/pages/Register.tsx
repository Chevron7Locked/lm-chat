/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Register page.
 *
 * Flow:
 *  1. User submits username + password + password_confirm.
 *  2. Client validates password === password_confirm before any network call.
 *  3. On 201 → navigate to /login with `state.justRegistered=true`.
 *  4. On 400 → inline error with the server-supplied detail.
 *  5. On 422 → inline error with the server-supplied detail.
 *
 * Co-located auth.css replaces inline CSSProperties.
 * Visual style mirrors Login.tsx: same brand lockup, same form chrome,
 * same field component, same submit button.
 */
import { useEffect, useRef, useState } from "react";
import type { ReactNode, SyntheticEvent } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";
import { Eye, EyeOff, AlertTriangle } from "lucide-react";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { useAuthStore } from "@/stores/authStore";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { BrandMark, BRAND_NAME } from "@/components/BrandMark";
import "@/styles/auth.css";

export default function Register() {
  useDocumentTitle("Create account");
  const { register, login, isLoading, isInitializing, clearError } =
    useAuthStore();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const token = searchParams.get("token") ?? undefined;
  useEffect(() => {
    clearError();
  }, [clearError]);

  const [error, setError] = useState<string | null>(null);

  const [needsSetup, setNeedsSetup] = useState<boolean | null>(null);
  useEffect(() => {
    void api
      .request<{ needs_setup: boolean }>("/api/auth/setup_status")
      .then((res) => {
        setNeedsSetup(res.needs_setup);
      })
      .catch(() => {
        setNeedsSetup(false);
      });
  }, []);

  const [username, setUsername] = useState("");
  const usernameTouched = username.length > 0;
  const usernameValid = /^[a-zA-Z0-9_]{3,64}$/.test(username);
  const usernameError =
    usernameTouched && !usernameValid
      ? username.length < 3
        ? "Use at least 3 characters."
        : /[^a-zA-Z0-9_]/.test(username)
          ? "Letters, numbers, and underscores only."
          : "Use 3–64 characters."
      : null;

  const usernameRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const passwordConfirmRef = useRef<HTMLInputElement>(null);

  const [passwordsMatch, setPasswordsMatch] = useState<boolean | null>(null);
  const [showPassword, setShowPassword] = useState(false);
  function recomputeMatch() {
    const p = passwordRef.current?.value ?? "";
    const c = passwordConfirmRef.current?.value ?? "";
    setPasswordsMatch(p === "" || c === "" ? null : p === c);
  }

  function handleSubmit(e: SyntheticEvent<HTMLFormElement>) {
    e.preventDefault();

    const submittedUsername = usernameRef.current?.value ?? "";
    const password = passwordRef.current?.value ?? "";
    const passwordConfirm = passwordConfirmRef.current?.value ?? "";

    if (password !== passwordConfirm) {
      setError("Passwords do not match.");
      return;
    }
    if (!/^[a-zA-Z0-9_]{3,64}$/.test(submittedUsername)) {
      setError(
        "Username must be 3–64 chars, letters/numbers/underscores only.",
      );
      return;
    }

    setError(null);

    // Capture bootstrap-admin status BEFORE register flips needs_setup
    // server-side — we want the second screen (LM Studio setup) to fire
    // when THIS submission was the bootstrap admin, not when a later
    // re-render happens to see needs_setup=false.
    const wasBootstrapAdmin = needsSetup === true;
    void register(submittedUsername, password, token)
      .then(async () => {
        // Auto-login so the user doesn't have to retype the creds
        // they just typed. POST /api/auth/register doesn't issue a
        // session cookie; chaining login() here is the simplest path
        // to "create account → straight to setup → main view" without
        // a backend change.
        try {
          await login(submittedUsername, password);
          void navigate(wasBootstrapAdmin ? "/setup/lm-studio" : "/", {
            replace: true,
          });
        } catch {
          // Account was created but the immediate login failed (rare —
          // network blip, totp surfaced, etc.). Fall back to the login
          // page so the user can finish manually, carrying the
          // bootstrap-admin flag so they still land on /setup/lm-studio.
          void navigate("/login", {
            replace: true,
            state: {
              justRegistered: true,
              username,
              needsLmStudioSetup: wasBootstrapAdmin,
            },
          });
        }
      })
      .catch((err: unknown) => {
        const apiErr = err as ApiError;
        setError(apiErr.detail ?? "Registration failed.");
      });
  }

  if (isInitializing) {
    return (
      <div className="lmchat-auth-page">
        <p
          style={{
            color: "var(--color-text-muted)",
            fontSize: "var(--fs-body-lg)",
            margin: 0,
          }}
        >
          Loading…
        </p>
      </div>
    );
  }

  return (
    <main id="main-content" tabIndex={-1} className="lmchat-auth-page">
      <div className="lmchat-auth-column">
        {/* ── Brand lockup — identical to Login.tsx ─────────────────────── */}
        <div className="lmchat-auth-brand">
          <BrandMark size={56} />
          <h1 className="lmchat-auth-wordmark">{BRAND_NAME}</h1>
          <p className="lmchat-auth-tagline">
            A panel of experts, on your machine.
          </p>
        </div>

        {/* CHAPTER gap between brand and form */}
        <div className="lmchat-auth-form-gap" aria-hidden="true" />

        <form onSubmit={handleSubmit} className="lmchat-auth-form">
          <header className="lmchat-auth-form-header">
            <h2 className="lmchat-auth-form-title">Create account</h2>
            <p className="lmchat-auth-form-subtitle">
              A few details and you're in.
            </p>
          </header>

          {/* Bootstrap-admin callout — gated on setup_status so it only
              renders on the fresh-install path. */}
          {needsSetup === true && (
            <div
              role="note"
              className="lmchat-auth-alert lmchat-auth-alert--note"
              data-testid="register-admin-bootstrap-callout"
            >
              <strong style={{ fontWeight: 600 }}>
                This account becomes the admin.
              </strong>{" "}
              All subsequent accounts join as regular users.
            </div>
          )}

          {error !== null && (
            <div
              role="alert"
              className="lmchat-auth-alert lmchat-auth-alert--error"
            >
              {error}
            </div>
          )}

          <Field
            label="Username"
            htmlFor="lmchat-register-username"
            hint="3–64 characters. Letters, numbers, and underscores."
            hintTone="muted"
            error={usernameError}
            input={
              <input
                ref={usernameRef}
                id="lmchat-register-username"
                name="username"
                type="text"
                autoComplete="username"
                placeholder="john_doe"
                required
                minLength={3}
                maxLength={64}
                pattern="[a-zA-Z0-9_]+"
                value={username}
                onChange={(e) => {
                  setUsername(e.target.value);
                }}
                aria-invalid={usernameError !== null}
                aria-describedby={
                  usernameError !== null
                    ? "lmchat-register-username-error"
                    : undefined
                }
                className={[
                  "lmchat-auth-input",
                  usernameError !== null ? "lmchat-auth-input--error" : "",
                ]
                  .filter(Boolean)
                  .join(" ")}
              />
            }
          />

          <Field
            label="Password"
            htmlFor="lmchat-register-password"
            input={
              <div style={{ position: "relative" }}>
                <input
                  ref={passwordRef}
                  id="lmchat-register-password"
                  name="password"
                  type={showPassword ? "text" : "password"}
                  autoComplete="new-password"
                  required
                  minLength={8}
                  maxLength={4096}
                  onInput={recomputeMatch}
                  className="lmchat-auth-input"
                  style={{ paddingRight: "44px" }}
                />
                <button
                  type="button"
                  onClick={() => {
                    setShowPassword((v) => !v);
                  }}
                  aria-label={showPassword ? "Hide password" : "Show password"}
                  aria-pressed={showPassword}
                  title={showPassword ? "Hide password" : "Show password"}
                  className="lmchat-pw-toggle"
                >
                  {showPassword ? (
                    <EyeOff size={16} aria-hidden />
                  ) : (
                    <Eye size={16} aria-hidden />
                  )}
                </button>
              </div>
            }
          />

          <Field
            label="Confirm password"
            htmlFor="lmchat-register-password-confirm"
            hint={
              passwordsMatch === true
                ? "Passwords match."
                : passwordsMatch === false
                  ? "Passwords do not match."
                  : undefined
            }
            hintTone={
              passwordsMatch === true
                ? "success"
                : passwordsMatch === false
                  ? "error"
                  : undefined
            }
            input={
              <div style={{ position: "relative" }}>
                <input
                  ref={passwordConfirmRef}
                  id="lmchat-register-password-confirm"
                  name="password_confirm"
                  type={showPassword ? "text" : "password"}
                  autoComplete="new-password"
                  required
                  minLength={8}
                  maxLength={4096}
                  onInput={recomputeMatch}
                  className="lmchat-auth-input"
                  style={{ paddingRight: "44px" }}
                />
                <button
                  type="button"
                  onClick={() => {
                    setShowPassword((v) => !v);
                  }}
                  aria-label={
                    showPassword
                      ? "Hide confirmation password"
                      : "Show confirmation password"
                  }
                  aria-pressed={showPassword}
                  title={
                    showPassword
                      ? "Hide confirmation password"
                      : "Show confirmation password"
                  }
                  className="lmchat-pw-toggle"
                >
                  {showPassword ? (
                    <EyeOff size={16} aria-hidden />
                  ) : (
                    <Eye size={16} aria-hidden />
                  )}
                </button>
              </div>
            }
          />

          <button
            type="submit"
            disabled={isLoading}
            className="lmchat-auth-submit atelier-cta"
          >
            {isLoading ? "Creating account…" : "Create account"}
          </button>

          {/* Hide the back-link during bootstrap — there's nothing to
              sign in to yet. /login would redirect right back here,
              creating a navigation loop. */}
          {needsSetup !== true && (
            <a
              href="/login"
              onClick={(ev) => {
                ev.preventDefault();
                void navigate("/login");
              }}
              className="lmchat-auth-footer-link"
            >
              ← Back to sign in
            </a>
          )}
        </form>

        {/* ── "Powered by LM Studio" attribution — marginalia footer ────── */}
        <p className="lmchat-auth-attribution">Powered by LM Studio</p>
      </div>
    </main>
  );
}

// ─── Sub-components ───────────────────────────────────────────────────────────

interface FieldProps {
  label: string;
  htmlFor: string;
  hint?: string | undefined;
  hintTone?: "muted" | "error" | "success" | undefined;
  error?: string | null | undefined;
  input: ReactNode;
}

function Field({ label, htmlFor, hint, hintTone, error, input }: FieldProps) {
  const hintClass =
    hintTone === "error"
      ? "lmchat-auth-field-hint lmchat-auth-field-hint--error"
      : hintTone === "success"
        ? "lmchat-auth-field-hint lmchat-auth-field-hint--success"
        : "lmchat-auth-field-hint";

  return (
    <div className="lmchat-auth-field">
      <label htmlFor={htmlFor} className="lmchat-auth-field-label">
        {label}
      </label>
      {input}
      {hint !== undefined && <span className={hintClass}>{hint}</span>}
      {error !== null && error !== undefined && error !== "" && (
        <span
          id={`${htmlFor}-error`}
          role="alert"
          aria-live="polite"
          className="lmchat-auth-field-hint lmchat-auth-field-hint--error"
        >
          <AlertTriangle size={12} aria-hidden style={{ flexShrink: 0 }} />
          {error}
        </span>
      )}
    </div>
  );
}
