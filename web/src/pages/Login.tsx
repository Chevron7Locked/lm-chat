/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Login page.
 *
 * Flow:
 *  1. User submits username + password.
 *  2. If the server returns 401 "totp required", reveal the TOTP input.
 *  3. User submits again with the TOTP code.
 *
 * Co-located auth.css replaces inline CSSProperties.
 * Brand lockup uses --fs-display (40px) wordmark, italic tagline,
 * CHAPTER (48px) gap to the form below.
 * "Powered by LM Studio" attribution sits at footer in marginalia type.
 */
import { useEffect, useRef, useState } from "react";
import type { ReactNode, SyntheticEvent } from "react";
import {
  Navigate,
  useLocation,
  useNavigate,
  useSearchParams,
} from "react-router-dom";
import { sanitizeReturnTo } from "@/lib/returnTo";
import { Eye, EyeOff } from "lucide-react";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { useAuthStore } from "@/stores/authStore";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { BrandMark, BRAND_NAME } from "@/components/BrandMark";
import "@/styles/auth.css";

interface SetupStatusResponse {
  needs_setup: boolean;
}

interface LoginLocationState {
  justRegistered?: boolean;
  username?: string;
  /**
   * Set by Register when this was the bootstrap-admin first run. After
   * successful login we route to /setup/lm-studio so the admin
   * configures LM Studio (base URL / API key / chat model) BEFORE
   * landing in Chat — previously they landed in Chat with an empty
   * model dropdown and had to discover Settings to fix it.
   */
  needsLmStudioSetup?: boolean;
}

export default function Login() {
  useDocumentTitle("Sign in");
  const { login, isLoading, isInitializing, error, clearError } =
    useAuthStore();
  const navigate = useNavigate();
  useEffect(() => {
    clearError();
  }, [clearError]);
  const location = useLocation();
  const locationState = (location.state ?? null) as LoginLocationState | null;
  const justRegistered = locationState?.justRegistered === true;
  const [searchParams] = useSearchParams();
  // `sanitizedReturnTo` is the only safe destination we accept. A bare
  // string check (`startsWith("/")`) would accept `//evil.com` (a
  // protocol-relative URL), so we validate more strictly. We also
  // collapse the empty-string case (the previous "sessionExpired" banner
  // flashed when the URL was `/login?returnTo=` with no value).
  const sanitizedReturnTo = sanitizeReturnTo(searchParams.get("returnTo"));
  const sessionExpired = sanitizedReturnTo !== null && !justRegistered;

  const [totpRequired, setTotpRequired] = useState(false);
  // Fresh-install bypass: when no users exist server-side the login
  // form is meaningless — nothing to sign into. We redirect to
  // /register so the user can't get stuck at /login with no path
  // forward. (If there's no account, login is meaningless — always
  // redirect to the wizard when accounts are empty.) `null` = pending
  // check; `true` = redirect; `false` = render the normal sign-in form.
  const [needsSetup, setNeedsSetup] = useState<boolean | null>(null);

  const usernameRef = useRef<HTMLInputElement>(null);
  const passwordRef = useRef<HTMLInputElement>(null);
  const [showPassword, setShowPassword] = useState(false);
  const totpRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    let cancelled = false;
    void api
      .request<SetupStatusResponse>("/api/auth/setup_status")
      .then((resp) => {
        if (!cancelled) {
          setNeedsSetup(resp.needs_setup);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setNeedsSetup(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  function handleSubmit(e: SyntheticEvent<HTMLFormElement>) {
    e.preventDefault();

    const username = usernameRef.current?.value ?? "";
    const password = passwordRef.current?.value ?? "";
    const totpCode = totpRef.current?.value ?? "";

    void login(username, password, totpRequired ? totpCode : undefined)
      .then(() => {
        const needsLmStudioSetup = locationState?.needsLmStudioSetup === true;
        const dest = needsLmStudioSetup
          ? "/setup/lm-studio"
          : (sanitizedReturnTo ?? "/");
        void navigate(dest, { replace: true });
      })
      .catch((err: unknown) => {
        const apiErr = err as ApiError;
        if (apiErr.detail === "totp required") {
          setTotpRequired(true);
        }
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

  // Fresh install: there's nothing to sign into yet. Force the
  // bootstrap-admin path before showing a meaningless form.
  // Render nothing while the check is pending so we don't flash the
  // login form for one tick.
  if (needsSetup === null) {
    return <div className="lmchat-auth-page" aria-hidden="true" />;
  }
  if (needsSetup) {
    return <Navigate to="/register" replace />;
  }

  return (
    <main id="main-content" tabIndex={-1} className="lmchat-auth-page">
      <div className="lmchat-auth-column">
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
            <h2 className="lmchat-auth-form-title">Sign in</h2>
            <p className="lmchat-auth-form-subtitle">Welcome back.</p>
          </header>

          {sessionExpired && (
            <div
              role="status"
              data-testid="login-session-expired-banner"
              className="lmchat-auth-alert lmchat-auth-alert--info"
            >
              Your session expired. Sign in to continue where you left off.
            </div>
          )}

          {justRegistered && (
            <div
              role="status"
              className="lmchat-auth-alert lmchat-auth-alert--success"
            >
              Account created — sign in to continue.
            </div>
          )}

          {error !== null && error !== "totp required" && (
            <div
              role="alert"
              className="lmchat-auth-alert lmchat-auth-alert--error"
            >
              {error}
            </div>
          )}

          <Field
            label="Username"
            htmlFor="lmchat-username"
            input={
              <input
                ref={usernameRef}
                id="lmchat-username"
                name="username"
                type="text"
                autoComplete="username"
                placeholder="username"
                required
                className="lmchat-auth-input"
              />
            }
          />

          <Field
            label="Password"
            htmlFor="lmchat-password"
            input={
              <div style={{ position: "relative" }}>
                <input
                  ref={passwordRef}
                  id="lmchat-password"
                  name="password"
                  type={showPassword ? "text" : "password"}
                  autoComplete="current-password"
                  required
                  className="lmchat-auth-input"
                  style={{ paddingRight: "60px" }}
                />
                <button
                  type="button"
                  onClick={() => {
                    setShowPassword((v) => !v);
                  }}
                  aria-label="Toggle password visibility"
                  aria-pressed={showPassword}
                  title="Toggle password visibility"
                  data-testid="login-password-toggle"
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

          {totpRequired && (
            <Field
              label="Authenticator code"
              htmlFor="lmchat-totp"
              hint="6-digit code from your authenticator app."
              input={
                <input
                  ref={totpRef}
                  id="lmchat-totp"
                  type="text"
                  inputMode="numeric"
                  pattern="[0-9]{6}"
                  maxLength={6}
                  placeholder="000 000"
                  autoComplete="one-time-code"
                  autoFocus
                  required
                  className="lmchat-auth-input lmchat-auth-input--mono"
                />
              }
            />
          )}

          <button
            type="submit"
            disabled={isLoading}
            className="lmchat-auth-submit atelier-cta"
          >
            {isLoading ? "Signing in…" : "Sign in"}
          </button>

          {/* No register link here by design: a fresh install (needsSetup) is
              redirected to /register above, so this form only renders once users
              exist — and then registration is invite-only (/register?token=…). */}
        </form>

        {/* ── "Powered by LM Studio" attribution — marginalia footer ──── */}
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
  input: ReactNode;
}

function Field({ label, htmlFor, hint, input }: FieldProps) {
  return (
    // Programmatic label/input association via htmlFor/id is intentional;
    // rendered HTML is <label for="X"><input id="X"/></label>.
    <div className="lmchat-auth-field">
      <label htmlFor={htmlFor} className="lmchat-auth-field-label">
        {label}
      </label>
      {input}
      {hint !== undefined && (
        <span className="lmchat-auth-field-hint">{hint}</span>
      )}
    </div>
  );
}
