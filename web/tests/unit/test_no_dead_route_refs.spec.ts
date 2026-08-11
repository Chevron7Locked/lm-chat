/**
 * Spring-clean guard — ratchet against dead-route rot.
 *
 * Fails if any spec file under web/tests/ references:
 *   - "/admin/plugins"  (route was renamed to /admin/integrations)
 *   - "P8.5"           (stale phase label removed from the toast message)
 *
 * Add new dead strings here as they are cleaned up.
 */
import { describe, it, expect } from "vitest";
import { readdirSync, readFileSync, statSync } from "fs";
import { join } from "path";

/** Collect all .spec.ts / .spec.tsx files recursively under a root. */
function collectSpecs(dir: string): string[] {
  const results: string[] = [];
  for (const entry of readdirSync(dir)) {
    const full = join(dir, entry);
    if (statSync(full).isDirectory()) {
      results.push(...collectSpecs(full));
    } else if (/\.spec\.[tj]sx?$/.test(entry)) {
      results.push(full);
    }
  }
  return results;
}

const TESTS_ROOT = join(__dirname, ".."); // web/tests/

const DEAD_STRINGS: Array<{ pattern: RegExp; label: string }> = [
  {
    pattern: /\/admin\/plugins/,
    label: '"/admin/plugins" (dead route — use /admin/integrations)',
  },
  {
    pattern: /P8\.5/,
    label: '"P8.5" (stale phase label — toast message was updated)',
  },
];

describe("dead-route / stale-string guard", () => {
  const specFiles = collectSpecs(TESTS_ROOT).filter(
    // Exclude this guard file itself from its own scan.
    (f) => !f.includes("test_no_dead_route_refs"),
  );

  for (const { pattern, label } of DEAD_STRINGS) {
    it(`no spec file references ${label}`, () => {
      const violations: string[] = [];
      for (const file of specFiles) {
        const content = readFileSync(file, "utf-8");
        if (pattern.test(content)) {
          violations.push(file.replace(TESTS_ROOT, "tests/"));
        }
      }
      expect(violations, `Found dead reference ${label} in these files`).toEqual([]);
    });
  }
});
