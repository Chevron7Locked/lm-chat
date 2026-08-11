/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Sanitize `returnTo` query strings before navigating.
 *
 * A naive `returnTo.startsWith("/")` check (as `Login.tsx` used) accepts
 * `/login?returnTo=//evil.example.com/pwn`: protocol-relative URLs
 * (`//host/...`) start with `/` but are absolute cross-origin URLs in
 * the browser. React Router's `navigate("//host")` jumps off-origin,
 * exposing an open-redirect attack surface.
 *
 * Allowed shapes:
 *   - `/path`               — same-origin absolute path
 *   - `/path?with=query`    — query strings OK
 *   - `/path#fragment`      — fragments OK
 *
 * Rejected shapes:
 *   - `""` / null / undefined — no destination
 *   - `//host/...`           — protocol-relative cross-origin
 *   - `/\\host/...`          — backslash-disguised cross-origin
 *   - `http://...`           — absolute URL
 *   - `javascript:...`       — XSS vector
 *   - anything not starting with `/`
 */

export function sanitizeReturnTo(
  raw: string | null | undefined,
): string | null {
  if (raw === null || raw === undefined || raw === "") {
    return null;
  }
  if (!raw.startsWith("/")) {
    return null;
  }
  // Protocol-relative URLs slip past `startsWith("/")` checks.
  if (raw.startsWith("//") || raw.startsWith("/\\")) {
    return null;
  }
  // Defense in depth: reject anything that decodes into a scheme.
  if (raw.includes("://") || raw.toLowerCase().startsWith("/javascript:")) {
    return null;
  }
  return raw;
}
