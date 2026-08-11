import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { resolve } from "node:path";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": resolve(__dirname, "./src") },
  },
  server: {
    fs: {
      // Allow the dev server to serve files from the repo-root guide/ directory
      // (one level above `web/`). Required for the import.meta.glob in Docs.tsx
      // which reaches `../../../guide/*.md` from `web/src/pages/`.
      allow: [".."],
    },
    // Bind to all interfaces so the dev server is reachable over the
    // Tailscale tailnet (2026-06-07). Exposes the dev
    // server at `http://100.x.x.x:<port>` and the Magic-DNS hostname.
    // `false` would restrict to loopback (the pre-2026-06-07 default).
    // The cookie middleware (auth.py:106-114) drops the `Secure` flag
    // for plain-HTTP requests, so Tailscale-over-HTTP auth works
    // without TLS termination.
    host: true,
    // Vite 4+ rejects requests whose Host header doesn't match
    // localhost / the dev-server port by default (DNS-rebinding
    // protection). Allow the configured Tailscale hosts so the dev
    // server accepts requests from another tailnet device. `.ts.net`
    // is the Tailscale Magic-DNS domain; the `100.x` block is the
    // Tailscale CGNAT range.
    allowedHosts: [
      ".ts.net",
      "kevins-mac-studio",
      "kevins-mac-studio.local",
      "localhost",
    ],
    proxy: {
      "/api": process.env["LM_CHAT_BE_URL"] ?? "http://localhost:8011",
      "/healthz": process.env["LM_CHAT_BE_URL"] ?? "http://localhost:8011",
      "/readyz": process.env["LM_CHAT_BE_URL"] ?? "http://localhost:8011",
    },
  },
  build: {
    outDir: "dist",
    // P12c: F-CONV-001 — 'hidden' generates .js.map files for
    // error-tracking tools (Sentry, etc.) but omits the `//# sourceMappingURL=`
    // comment from the JS bundle, so source maps are not publicly accessible
    // to end-users browsing the production site.
    sourcemap: "hidden",
    manifest: true,
  },
});
