#!/usr/bin/env node
/**
 * Bundle size CI gate — P7 DEFERRED §8, closed in P9b.
 *
 * Computes the gzipped size of the initial-route entry chunk
 * (dist/assets/index-*.js) and exits 1 if it exceeds LIMIT_BYTES.
 *
 * No new dependencies: uses Node built-ins zlib + fs only.
 *
 * Usage: node scripts/size-check.mjs
 *        (run after `pnpm build` — reads from dist/assets/)
 *
 * Gate: 300 KB gzipped per initial route chunk.
 *
 * Closes P7 DEFERRED §8.
 */

import { readFileSync, readdirSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";
import { gzipSync } from "zlib";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const WEB_ROOT = join(__dirname, "..");
const DIST_ASSETS = join(WEB_ROOT, "dist", "assets");

/** Maximum allowed gzipped size for the initial entry chunk in bytes. */
const LIMIT_BYTES = 300 * 1024; // 300 KB

function humanKB(bytes) {
  return (bytes / 1024).toFixed(1) + " KB";
}

// Find all index-*.js files (Vite names the entry chunk index-<hash>.js).
let entryFiles;
try {
  const all = readdirSync(DIST_ASSETS);
  entryFiles = all.filter((f) => /^index-[^.]+\.js$/.test(f));
} catch (err) {
  console.error(
    `size-check: could not read ${DIST_ASSETS} — run 'pnpm build' first.`
  );
  console.error(err.message);
  process.exit(2);
}

if (entryFiles.length === 0) {
  console.error(
    `size-check: no initial-route chunk (index-*.js) found in ${DIST_ASSETS}.`
  );
  process.exit(2);
}

let exceeded = false;

for (const file of entryFiles) {
  const filePath = join(DIST_ASSETS, file);
  const raw = readFileSync(filePath);
  const gzipped = gzipSync(raw, { level: 9 });
  const gzSize = gzipped.length;
  const rawSize = raw.length;

  const status = gzSize > LIMIT_BYTES ? "FAIL" : "PASS";
  const limit = humanKB(LIMIT_BYTES);
  console.log(
    `${status}  ${file}  raw=${humanKB(rawSize)}  gzip=${humanKB(gzSize)}  limit=${limit}`
  );

  if (gzSize > LIMIT_BYTES) {
    exceeded = true;
  }
}

if (exceeded) {
  console.error(
    `\nsize-check FAILED: one or more initial-route chunks exceed ${humanKB(LIMIT_BYTES)} gzipped.`
  );
  process.exit(1);
} else {
  console.log(
    `\nsize-check PASSED: all initial-route chunks are within ${humanKB(LIMIT_BYTES)} gzipped.`
  );
}
