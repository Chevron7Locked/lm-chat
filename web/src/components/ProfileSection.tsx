/* SPDX-License-Identifier: Apache-2.0 */
/**
 * ProfileSection — Settings → Profile tab.
 *
 * Surfaces the user's public identity beyond the opaque username:
 * email, display name, avatar URL. All three are nullable server-side
 * (migration 0020) so a fresh account renders empty fields and the
 * user populates them as desired.
 *
 * Writes go through `PATCH /api/auth/profile`. The endpoint uses the
 * same "form fields + clear list" pattern as
 * `PUT /api/settings/lmstudio` — an omitted field leaves the stored
 * value alone, and `clear=<comma list>` NULLs out the named fields.
 *
 * Validation rules (mirrored on backend):
 *   - email: must be a syntactically valid address (single @, dot in
 *     domain, no spaces)
 *   - display_name: any text, ≤1024 chars, no NUL bytes
 *   - avatar_url: must be http(s); `javascript:` and `data:` are
 *     rejected at the route layer.
 */
import { useEffect, useState } from "react";
import type { SubmitEvent } from "react";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";
import { useToast } from "@/stores/toastStore";
import "@/styles/settings.css";

interface MeShape {
  user_id: number;
  username: string;
  is_admin: boolean;
  needs_setup: boolean;
  totp_enabled: boolean;
  email: string | null;
  display_name: string | null;
  avatar_url: string | null;
}

export function ProfileSection() {
  const { user } = useAuthStore();
  const { push } = useToast();
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [avatarUrl, setAvatarUrl] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void api
      .request<MeShape>("/api/auth/me")
      .then((me) => {
        if (cancelled) return;
        setEmail(me.email ?? "");
        setDisplayName(me.display_name ?? "");
        setAvatarUrl(me.avatar_url ?? "");
      })
      .catch(() => {
        if (!cancelled) {
          setError("Couldn't load profile — try again.");
        }
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleSave(e: SubmitEvent<HTMLFormElement>): Promise<void> {
    e.preventDefault();
    setSaving(true);
    setError(null);

    // Build form body. We always send all three fields (empty string =
    // clear via the explicit `clear` list, non-empty = set). FastAPI
    // collapses omitted and empty-string into None server-side, so we
    // use the `clear` list to disambiguate.
    const body = new URLSearchParams();
    const clears: string[] = [];
    if (email.trim() === "") {
      clears.push("email");
    } else {
      body.append("email", email.trim());
    }
    if (displayName.trim() === "") {
      clears.push("display_name");
    } else {
      body.append("display_name", displayName.trim());
    }
    if (avatarUrl.trim() === "") {
      clears.push("avatar_url");
    } else {
      body.append("avatar_url", avatarUrl.trim());
    }
    if (clears.length > 0) {
      body.append("clear", clears.join(","));
    }

    try {
      await api.request<MeShape>("/api/auth/profile", {
        method: "PATCH",
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: body.toString(),
      });
      push({ variant: "success", message: "Profile saved." });
    } catch (err) {
      const apiErr = err as ApiError;
      setError(apiErr.detail ?? "Save failed.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div
      className="lmchat-section-container"
      data-testid="settings-profile-section"
    >
      <p className="lmchat-section-description">
        Your presentable identity. Used as the display name in shared chats and
        the avatar in the chat shell. All fields are optional; when blank we
        fall back to your username
        {user !== null ? ` (${user.username})` : ""}.
      </p>

      {loading ? (
        <p className="lmchat-section-description">Loading…</p>
      ) : (
        <form
          onSubmit={(e) => {
            void handleSave(e);
          }}
          className="lmchat-form"
          style={{ maxWidth: 480 }}
          data-testid="settings-profile-form"
          noValidate
        >
          <div className="lmchat-field">
            <label
              htmlFor="profile-display-name"
              className="lmchat-field-label"
            >
              Display name
            </label>
            <input
              id="profile-display-name"
              name="display_name"
              type="text"
              value={displayName}
              onChange={(e) => {
                setDisplayName(e.target.value);
              }}
              maxLength={1024}
              className="lmchat-input"
              data-testid="settings-profile-display-name"
              autoComplete="name"
            />
          </div>

          <div className="lmchat-field">
            <label htmlFor="profile-email" className="lmchat-field-label">
              Email
            </label>
            <input
              id="profile-email"
              name="email"
              type="email"
              value={email}
              onChange={(e) => {
                setEmail(e.target.value);
              }}
              maxLength={1024}
              className="lmchat-input"
              data-testid="settings-profile-email"
              autoComplete="email"
            />
          </div>

          <div className="lmchat-field">
            <label htmlFor="profile-avatar-url" className="lmchat-field-label">
              Avatar URL
            </label>
            <input
              id="profile-avatar-url"
              name="avatar_url"
              type="url"
              value={avatarUrl}
              onChange={(e) => {
                setAvatarUrl(e.target.value);
              }}
              maxLength={1024}
              placeholder="https://"
              className="lmchat-input"
              data-testid="settings-profile-avatar-url"
              autoComplete="photo"
            />
          </div>

          {error !== null && (
            <p className="lmchat-form-error" role="alert">
              {error}
            </p>
          )}

          <div className="lmchat-form-actions">
            <button
              type="submit"
              disabled={saving}
              className="lmchat-btn-primary"
              data-testid="settings-profile-save"
            >
              {saving ? "Saving…" : "Save"}
            </button>
          </div>
        </form>
      )}
    </div>
  );
}
