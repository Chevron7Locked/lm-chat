import { defineConfig } from "vitest/config";
import { resolve } from "node:path";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: ["tests/setup.ts"],
    // Exclude Playwright e2e tests — they run via `pnpm test:e2e:stubbed`
    // and `pnpm test:e2e:live` (playwright test).
    include: ["tests/unit/**/*.spec.{ts,tsx}", "tests/security/**/*.spec.{ts,tsx}"],
    exclude: ["tests/e2e-stubbed/**", "tests/e2e-live/**", "node_modules/**"],
    coverage: {
      provider: "v8",
      include: ["src/lib/**", "src/stores/**", "src/hooks/**"],
      reporter: ["text", "lcov"],
      thresholds: {
        lines: 70,
        branches: 70,
      },
    },
  },
  resolve: {
    alias: { "@": resolve(__dirname, "./src") },
  },
});
