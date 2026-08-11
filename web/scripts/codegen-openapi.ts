/**
 * Regenerate web/src/types/api.ts from docs/api/openapi.yaml.
 *
 * Usage: pnpm codegen
 *
 * The output file is gitignored (it's a build artifact). Run this script
 * whenever the backend's OpenAPI spec changes. P6 auto-emits openapi.yaml;
 * P7 consumes it via this codegen step.
 */
import { execSync } from "node:child_process";
import { resolve, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const yaml = resolve(__dirname, "../../docs/api/openapi.yaml");
const out = resolve(__dirname, "../src/types/api.ts");

execSync(`pnpm exec openapi-typescript "${yaml}" -o "${out}"`, { stdio: "inherit" });
console.log(`openapi-typescript wrote ${out}`);
