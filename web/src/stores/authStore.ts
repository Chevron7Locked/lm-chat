/* SPDX-License-Identifier: Apache-2.0 */
import { create } from "zustand";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { queryClient } from "@/lib/queryClient";

interface User {
  id: number;
  username: string;
  is_admin: boolean;
  /**
   * True iff the user has a TOTP secret on file.  Surfaced by both
   * /api/auth/login and /api/auth/me so the Settings TOTP surface can
   * render the correct state on mount + after reload.
   */
  totp_enabled: boolean;
}

interface AuthState {
  user: User | null;
  isLoading: boolean;
  /** True while the mount-time /me hydration is in-flight. */
  isInitializing: boolean;
  /**
   * True when the server has ZERO users (fresh install). Sourced from the
   * mount-time /api/auth/me/probe response. A fresh install must route
   * straight to /register — never bounce through /login, where there's
   * nothing to sign into.
   */
  needsSetup: boolean;
  error: string | null;
  login: (
    username: string,
    password: string,
    totpCode?: string,
  ) => Promise<void>;
  /**
   * Create a new account via POST /api/auth/register.
   *
   * The backend register endpoint does NOT set a session cookie (see
   * `src/lmchat/routes/auth.py:131-162`), so this action does NOT populate
   * `user` — the caller must navigate to /login afterwards. We still
   * surface loading state and error so the Register page can render
   * consistently with the Login page.
   *
   * On 400 (username taken) or 422 (invalid username) the promise rejects
   * with the same ApiError shape as `login()` — the detail string carries
   * the server-supplied reason.
   */
  register: (
    username: string,
    password: string,
    token?: string,
  ) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
  /**
   * Imperatively set `user.totp_enabled` after a successful enable/disable
   * flow so the UI reflects server truth without waiting for a /me
   * round-trip.  No-op if no user is signed in.
   */
  setTotpEnabled: (enabled: boolean) => void;
  /**
   * The shared `error` field is read by both Login and
   * Register.  When a registration failure populates it and the user
   * navigates to Login, the stale alert "carries over."  Pages should
   * call clearError() on mount to scope errors to the current view.
   */
  clearError: () => void;
}

// Login response shape from POST /api/auth/login (per auth.py).
// Backend extended to include username + is_admin
// so the SPA can hydrate the auth store without a separate /me round-trip.
// `totp_enabled` is included too (same purpose, for the Settings TOTP
// surface).
interface LoginResponse {
  user_id: number;
  expires_at: string;
  username: string | null;
  is_admin: boolean;
  totp_enabled?: boolean;
}

// /me/probe response shape — the SAME fields as /me, but `user_id` and
// `username` can be `null` when no session is present (vs /me which
// 401s in that case). The probe endpoint exists so AuthHydrator can
// resolve auth state without generating a console-visible 401 on every
// cold load.
interface MeProbeResponse {
  user_id: number | null;
  username: string | null;
  is_admin: boolean;
  totp_enabled?: boolean;
  needs_setup?: boolean;
}

export const useAuthStore = create<AuthState>((set, get) => ({
  user: null,
  isLoading: false,
  isInitializing: true,
  needsSetup: false,
  error: null,

  login: async (username: string, password: string, totpCode?: string) => {
    set({ isLoading: true, error: null });
    try {
      const fields: Record<string, string> = { username, password };
      if (totpCode !== undefined && totpCode !== "") {
        fields.totp_code = totpCode;
      }
      // POST /api/auth/login returns { user_id, expires_at, username, is_admin }
      // + sets session cookie. is_admin is sourced from the users.is_admin
      // schema column so admin-gated UI (Memory.reindex, Settings
      // admin controls) render correctly without a separate /me round-trip.
      const result = await api.postForm<LoginResponse>(
        "/api/auth/login",
        fields,
      );
      set({
        user: {
          id: result.user_id,
          username: result.username ?? username,
          is_admin: result.is_admin,
          // Optional for back-compat with older backends that pre-date
          // its addition; default to false until the user enables TOTP.
          totp_enabled: result.totp_enabled ?? false,
        },
        isLoading: false,
        isInitializing: false,
        needsSetup: false,
      });
    } catch (err: unknown) {
      const apiErr = err as ApiError;
      set({ error: apiErr.detail ?? "login failed", isLoading: false });
      throw err;
    }
  },

  register: async (username: string, password: string, token?: string) => {
    set({ isLoading: true, error: null });
    try {
      // POST /api/auth/register returns { id, username } with status 201.
      // No session cookie is set; the caller is responsible for routing
      // to /login afterwards. When a token is present it rides on the
      // query string for the setup-token gate and the invite gate.
      const path = token
        ? `/api/auth/register?token=${encodeURIComponent(token)}`
        : "/api/auth/register";
      await api.postForm<{ id: number; username: string }>(path, {
        username,
        password,
      });
      // A user now exists — clear the fresh-install flag so a post-register
      // visit to a protected route routes to /login, not back to /register.
      set({ isLoading: false, needsSetup: false });
    } catch (err: unknown) {
      const apiErr = err as ApiError;
      set({ error: apiErr.detail ?? "registration failed", isLoading: false });
      throw err;
    }
  },

  logout: async () => {
    await api.request<{ status: string }>("/api/auth/logout", {
      method: "POST",
    });
    // Sweep stream state from localStorage so a different user logging in
    // on the same browser does not inherit the previous user's
    // response-id chain or in-flight msg-id. Fix: response_id was not
    // cleared on logout.
    try {
      const stale: string[] = [];
      for (let i = 0; i < localStorage.length; i++) {
        const key = localStorage.key(i);
        if (key?.startsWith("lmchat:sse:")) {
          stale.push(key);
        }
      }
      for (const key of stale) {
        localStorage.removeItem(key);
      }
    } catch {
      // localStorage access can throw in private browsing modes; safe to
      // ignore — the worst case is a stale key surviving until next
      // explicit chat-switch in the same browser.
    }
    // fe-14 fix: drop every cached query (chats, projects, models, …) so a
    // same-browser user switch never briefly shows user A's cached data to
    // user B. Previously only the SSE localStorage keys were swept — the
    // TanStack cache survived logout entirely.
    queryClient.clear();
    set({ user: null, isInitializing: false });
  },

  refresh: async () => {
    // If already authenticated (e.g. from login() in this same session),
    // no need to re-fetch. isInitializing is still true here if this is
    // the mount-time call (called from App.tsx on first render).
    const current = get().user;
    if (current !== null) {
      set({ isInitializing: false });
      return;
    }
    // Call GET /api/auth/me/probe — always returns 200 so we don't
    // generate a console-visible 401 on every cold load. The probe
    // returns user_id=null when no session is active; we then set
    // user=null without an error path. Real authentication-required
    // endpoints continue to use /me + 401, but the mount-time
    // hydration probe is allowed to ask "is there a session?" without
    // the noise.
    try {
      const me = await api.request<MeProbeResponse>("/api/auth/me/probe");
      if (me.user_id === null || me.username === null) {
        // No session. Capture needs_setup so RequireAuth can route a
        // fresh install straight to /register instead of /login.
        set({
          user: null,
          isInitializing: false,
          needsSetup: me.needs_setup ?? false,
        });
        return;
      }
      set({
        user: {
          id: me.user_id,
          username: me.username,
          is_admin: me.is_admin,
          totp_enabled: me.totp_enabled ?? false,
        },
        isInitializing: false,
        needsSetup: false,
      });
    } catch {
      // Network/server error — graceful degradation.
      set({ user: null, isInitializing: false });
    }
  },

  setTotpEnabled: (enabled: boolean) => {
    const current = get().user;
    if (current === null) return;
    set({ user: { ...current, totp_enabled: enabled } });
  },

  clearError: () => {
    set({ error: null });
  },
}));
