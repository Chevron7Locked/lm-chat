/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Fetch wrapper for lm-chat API calls.
 *
 * CSRF strategy: the backend (auth.py) uses SameSite=Lax session cookies.
 * SameSite=Lax blocks cross-origin POST requests from third-party pages,
 * which is the primary CSRF attack vector. The backend does NOT require an
 * X-CSRF-Token header on any route. We therefore omit the header
 * entirely — adding a header the server ignores would be noise and could
 * interfere with preflight OPTIONS checks.
 *
 * If a CSRF-token endpoint is added later, update this module and the
 * backend simultaneously.
 *
 * credentials: "same-origin" ensures the session cookie is sent on every
 * request to the backend and is never sent cross-origin (defense in depth).
 *
 * Central 401 interceptor.  Any non-auth-probe request that returns 401
 * clears the auth store and redirects to /login.
 * This kills the zombie-SPA pattern where a revoked session keeps polling
 * forever with dead cookies.  Auth probe paths (/api/auth/*) are exempt
 * because the probe is designed to be hit with possibly-invalid sessions.
 */

export interface ApiError extends Error {
  status: number;
  /** Human-readable message — populated when the body's `detail` is a string. */
  detail?: string | undefined;
  /**
   * Structured `detail` body when FastAPI returns an object. Example: the
   * regenerate 412 gate returns `{code, subsequent_count, chat_id,
   * message_id}`. Earlier this was thrown away because `detail` was only
   * kept when it was a string — that silently broke the regenerate
   * confirm modal (the frontend's `detail.code === "confirm_required"`
   * check always saw `undefined`, fell through to the generic toast,
   * and regen looked perma-broken).
   */
  detailObject?: Record<string, unknown> | undefined;
}

/**
 * Paths that are allowed to receive a 401 without triggering the
 * auth-clear + redirect.  The /api/auth/me/probe
 * endpoint exists specifically to be called with possibly-invalid cookies;
 * other /api/auth/* paths (login, register) may also return 401 in normal
 * operation (wrong password, etc.) and must not redirect.
 */
function isAuthPath(path: string): boolean {
  return path.startsWith("/api/auth/");
}

/**
 * Clear Zustand auth state + navigate to /login.
 * Lazy import avoids a circular dependency (authStore → api → authStore).
 * Uses window.location.href so this works outside React's render cycle.
 */
async function handle401(): Promise<void> {
  try {
    // Dynamic import to avoid circular dependency.
    const { useAuthStore } = await import("@/stores/authStore");
    // Clear the user out of Zustand so any component that reads it sees null.
    useAuthStore.setState({ user: null, isInitializing: false });
  } catch {
    // Guard: if the import fails for any reason, still redirect.
  }
  // Navigate to /login; preserve the current path as returnTo so the user
  // lands back after re-authenticating.
  const here = window.location.pathname + window.location.search;
  const dest = here && here !== "/" && !here.startsWith("/login")
    ? `/login?returnTo=${encodeURIComponent(here)}`
    : "/login";
  window.location.href = dest;
}

export class ApiClient {
  constructor(private readonly baseUrl = "") {}

  async request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const { headers: initHeaders, ...restInit } = init;
    const headers: Record<string, string> = { ...(initHeaders as Record<string, string> | undefined) };
    // Auto-set Content-Type for JSON-string bodies. Many callers do
    // `request(path, { method, body: JSON.stringify(obj) })` without a header;
    // with no Content-Type FastAPI cannot parse the body and returns 422.
    // Skip when:
    //   - the body is not a string (FormData/Blob/URLSearchParams) — the
    //     browser must set its own multipart/form boundary; forcing JSON here
    //     would break file uploads.
    //   - the caller already supplied a Content-Type (e.g. postForm's
    //     x-www-form-urlencoded) — that explicit header is preserved.
    const hasContentType = Object.keys(headers).some(
      (k) => k.toLowerCase() === "content-type"
    );
    if (typeof restInit.body === "string" && !hasContentType) {
      headers["Content-Type"] = "application/json";
    }
    const response = await fetch(this.baseUrl + path, {
      ...restInit,
      headers,
      credentials: "same-origin",
    });
    if (!response.ok) {
      // Intercept 401 on non-auth paths.
      if (response.status === 401 && !isAuthPath(path)) {
        void handle401();
      }
      throw await normalizeError(response);
    }
    // 204 / 205 No Content — no body to parse.
    if (response.status === 204 || response.status === 205) {
      return undefined as unknown as T;
    }
    return response.json() as Promise<T>;
  }

  async postForm<T>(path: string, fields: Record<string, string>): Promise<T> {
    const body = new URLSearchParams(fields).toString();
    return this.request<T>(path, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body,
    });
  }
}

async function normalizeError(response: Response): Promise<ApiError> {
  let detail: string | undefined;
  let detailObject: Record<string, unknown> | undefined;
  try {
    const body = (await response.json()) as { detail?: unknown };
    if (typeof body.detail === "string") {
      detail = body.detail;
    } else if (
      body.detail !== null &&
      typeof body.detail === "object" &&
      !Array.isArray(body.detail)
    ) {
      detailObject = body.detail as Record<string, unknown>;
    }
  } catch {
    // ignore parse failure
  }
  const message =
    detail ??
    (typeof detailObject?.code === "string"
      ? detailObject.code
      : response.statusText);
  const err = new Error(message) as ApiError;
  err.status = response.status;
  err.detail = detail;
  err.detailObject = detailObject;
  return err;
}

export const api = new ApiClient();
