#!/usr/bin/env node
/**
 * dogfood-preflight — hard-fail LOUD gate before the live-model dogfood
 * Playwright suite (`make dogfood-live`) runs. Checks:
 *
 *   1. LM Studio is reachable at LMCHAT_DOGFOOD_LMSTUDIO_URL (default
 *      http://localhost:1234) via the OpenAI-compat /v1/models endpoint,
 *      returns HTTP 200, and the loaded fleet has AT LEAST ONE general
 *      (non-embedding) chat model AND at least one embedding model — the
 *      dogfood journeys need both (chat turns + memory recall / RAG).
 *   2. web/dist exists — the dogfood backend serves the BUILT SPA, not the
 *      Vite dev server.
 *
 * On success, prints a one-line fleet summary (counts + the fast/slow model
 * pick, via a tiny inline rank mirroring web/src/lib/modelSpeedRank.ts:
 * MoE active param ("a3b") if present, else total param ("35b"); lower is
 * faster). Duplicated here in plain JS rather than imported — this script
 * runs standalone via `node`, outside the TS build.
 *
 * On failure, exits nonzero with exactly what to load / build. Never
 * silently degrade: a "0 models" read must mean the environment is unready,
 * not a false negative from a missing auth header — always pass the Bearer
 * key when LMCHAT_DOGFOOD_LMSTUDIO_KEY is set.
 *
 * Usage: node web/scripts/dogfood-preflight.mjs
 */
import { existsSync } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);
const WEB_ROOT = join(__dirname, "..");

// Resolve from LMCHAT_DOGFOOD_* first, then the backend's own LM_STUDIO_* env
// (sourced from `.env.local` by `make dogfood-live`) — no inline key needed.
const LMSTUDIO_URL =
  process.env["LMCHAT_DOGFOOD_LMSTUDIO_URL"] ??
  process.env["LM_STUDIO_BASE_URL"] ??
  "http://localhost:1234";
const LMSTUDIO_KEY =
  process.env["LMCHAT_DOGFOOD_LMSTUDIO_KEY"] ??
  process.env["LM_STUDIO_API_KEY"] ??
  "";

/**
 * Mirrors web/src/lib/modelSpeedRank.ts's modelSpeedRank. Lower is faster.
 * MoE active params ("a3b", "a10b") first; falls back to dense total params
 * ("35b", "9b"); Infinity when neither is present (unlabelled = worst-case).
 */
function modelSpeedRank(idOrName) {
  const s = idOrName.toLowerCase();
  const activeMatch = /a(\d+(?:\.\d+)?)b(?![a-z0-9])/.exec(s);
  if (activeMatch) return Number.parseFloat(activeMatch[1]);
  const totalMatch = /(\d+(?:\.\d+)?)b(?![a-z0-9])/.exec(s);
  if (totalMatch) return Number.parseFloat(totalMatch[1]);
  return Number.POSITIVE_INFINITY;
}

function fail(message) {
  console.error(`\n[dogfood-preflight] FAILED\n\n${message}\n`);
  process.exit(1);
}

async function main() {
  // 1. Probe LM Studio's OpenAI-compat model list.
  const modelsUrl = `${LMSTUDIO_URL.replace(/\/+$/, "")}/v1/models`;
  const headers = {};
  if (LMSTUDIO_KEY !== "") headers["Authorization"] = `Bearer ${LMSTUDIO_KEY}`;

  let resp;
  try {
    const controller = new AbortController();
    const timer = setTimeout(() => {
      controller.abort();
    }, 10_000);
    try {
      resp = await fetch(modelsUrl, { headers, signal: controller.signal });
    } finally {
      clearTimeout(timer);
    }
  } catch (err) {
    fail(
      `Could not reach LM Studio at ${modelsUrl} ` +
        `(${err instanceof Error ? err.message : String(err)}).\n` +
        `Start LM Studio and load at least one general chat model + one ` +
        `embedding model, or set LMCHAT_DOGFOOD_LMSTUDIO_URL to the right host.`,
    );
    return;
  }

  if (!resp.ok) {
    fail(
      `LM Studio at ${modelsUrl} returned HTTP ${resp.status}. Confirm the ` +
        `server is running and, if LMCHAT_DOGFOOD_LMSTUDIO_KEY is set, that it ` +
        `matches LM Studio's configured API key.`,
    );
    return;
  }

  let body;
  try {
    body = await resp.json();
  } catch {
    fail(
      `LM Studio at ${modelsUrl} returned a non-JSON body — is this really ` +
        `an LM Studio server?`,
    );
    return;
  }

  const list = Array.isArray(body?.data) ? body.data : [];
  const ids = list
    .map((m) => (m && typeof m === "object" ? String(m.id ?? "") : ""))
    .filter((id) => id !== "");
  const embeddingIds = ids.filter((id) => id.toLowerCase().includes("embed"));
  const generalIds = ids.filter((id) => !id.toLowerCase().includes("embed"));

  if (generalIds.length === 0) {
    fail(
      `LM Studio reports ${ids.length} loaded model(s) but NONE are a ` +
        `general chat model (non-embedding). Load at least one general LLM ` +
        `in LM Studio and re-run.`,
    );
    return;
  }
  if (embeddingIds.length === 0) {
    fail(
      `LM Studio reports ${generalIds.length} general model(s) but NO ` +
        `embedding model. Load an embedding model (e.g. ` +
        `text-embedding-nomic-embed-text-v1.5) in LM Studio and re-run — ` +
        `memory recall / RAG journeys need it.`,
    );
    return;
  }

  // Fast/slow pick over the general fleet, for the summary line only.
  let fastest = generalIds[0];
  let fastestRank = modelSpeedRank(fastest);
  let slowest = generalIds[0];
  let slowestRank = modelSpeedRank(slowest);
  for (const id of generalIds.slice(1)) {
    const rank = modelSpeedRank(id);
    if (rank < fastestRank) {
      fastest = id;
      fastestRank = rank;
    }
    if (rank > slowestRank) {
      slowest = id;
      slowestRank = rank;
    }
  }

  // 2. web/dist must exist — the dogfood backend serves the built SPA.
  const distDir = join(WEB_ROOT, "dist");
  if (!existsSync(distDir)) {
    fail(
      `web/dist is missing — the dogfood backend serves the BUILT SPA, not ` +
        `the dev server. Run \`npm run build\` (inside web/) and re-run.`,
    );
    return;
  }

  console.log(
    `[dogfood-preflight] OK — ${generalIds.length} general model(s), ` +
      `${embeddingIds.length} embedding model(s) reachable at ${LMSTUDIO_URL}. ` +
      `fastest=${fastest} (rank ${fastestRank}), slowest=${slowest} (rank ${slowestRank}).`,
  );
}

await main();
