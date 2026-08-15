/**
 * Live-backend fixture for lm-chat Playwright e2e tests.
 *
 * Live-backend Playwright fixture: spins up the FastAPI server + an LM Studio stub.
 *
 * On worker startup:
 *   1. Find a free TCP port for the FastAPI backend.
 *   2. Find a free TCP port for the stub LM Studio server.
 *   3. Start a minimal HTTP stub that replays a fixed SSE stream (the
 *      LM Studio upstream).  The backend's LM_CHAT_LM_STUDIO_BASE_URL
 *      is pointed at this stub so no real LM Studio instance is needed.
 *   4. Spawn `uv run uvicorn lmchat.app:app` with an ephemeral SQLite DB.
 *   5. Poll /healthz until 200 (up to 30 s).
 *   6. Register a test user via POST /api/auth/register.
 *   7. Yield { baseURL, username, password }.
 *   8. On teardown: kill uvicorn, stop the stub server, remove the DB.
 *
 * The fixture is a Playwright `test` extension via `test.extend()`.
 * Import `{ test, expect }` from this module in every live spec.
 */

import { test as base } from "@playwright/test";
import { spawn, spawnSync, ChildProcess } from "child_process";
import { createServer, Server } from "http";
import { rm } from "fs/promises";
import { existsSync, writeFileSync, createWriteStream } from "fs";
import { join, dirname } from "path";
import { fileURLToPath } from "url";

// ---------------------------------------------------------------------------
// Live-dogfood real-upstream mode (LMCHAT_REAL_UPSTREAM=1)
// ---------------------------------------------------------------------------
// When set, this fixture drives the backend against the operator's REAL LM
// Studio instead of the in-process stub, at --log-level info, teeing the
// uvicorn log to a file so grey-box journeys can assert on fail-soft background
// operations (auto-memory distillation / titles / follow-ups) that are INVISIBLE
// in the UI when they fail. Every change is gated on this flag, so the normal
// stub-based e2e-live suite is byte-for-byte unchanged. See
// tests/e2e-dogfood/ + the DESIGN doc.
const REAL_UPSTREAM = process.env["LMCHAT_REAL_UPSTREAM"] === "1";
// URL/key resolve from LMCHAT_DOGFOOD_* first, then the SAME LM_STUDIO_* vars
// the backend reads from `.env.local` (the project's LM Studio SSOT) — so
// `make dogfood-live` sources `.env.local` and everything just works, no key
// passed inline and no key ever committed (`.env.local` is gitignored).
const REAL_LMSTUDIO_URL =
  process.env["LMCHAT_DOGFOOD_LMSTUDIO_URL"] ??
  process.env["LM_STUDIO_BASE_URL"] ??
  "http://localhost:1234";
const REAL_LMSTUDIO_KEY =
  process.env["LMCHAT_DOGFOOD_LMSTUDIO_KEY"] ??
  process.env["LM_STUDIO_API_KEY"] ??
  "";

// ---------------------------------------------------------------------------
// Port-finding helper
// ---------------------------------------------------------------------------

function findFreePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const srv = createServer();
    srv.listen(0, "127.0.0.1", () => {
      const addr = srv.address();
      if (!addr || typeof addr === "string") {
        reject(new Error("Could not get server address"));
        return;
      }
      const port = addr.port;
      srv.close(() => { resolve(port); });
    });
  });
}

// ---------------------------------------------------------------------------
// Minimal LM Studio stub server
// ---------------------------------------------------------------------------

/**
 * Build a minimal SSE response body that matches the LM Studio native wire
 * format as parsed by lmchat/lmstudio/native.py:
 *
 *   chat.start  → data JSON has "response_id"
 *   message.delta → data JSON has "content" (NOT "delta" — native.py line 208:
 *                   kwargs["content"] = payload.get("content"))
 *   chat.end    → data JSON has "response_id"
 *
 * BUG FIX (SA-2026-001): The original fixture used "delta" as the JSON key
 * in message.delta events. The backend native.py adapter reads "content", not
 * "delta". This caused the streaming service to accumulate empty content and
 * write empty assistant messages to the DB, breaking all streaming tests.
 */
function buildSseFixture(content: string): string {
  const start = JSON.stringify({ type: "chat.start", response_id: "stub-resp-1" });
  // Use "content" key — matches native.py: kwargs["content"] = payload.get("content")
  const delta = JSON.stringify({ type: "message.delta", content });
  // chat.end also carries response_id for finalization.
  const end = JSON.stringify({ type: "chat.end", response_id: "stub-resp-1" });
  return (
    `event: chat.start\ndata: ${start}\n\n` +
    `event: message.delta\ndata: ${delta}\n\n` +
    `event: chat.end\ndata: ${end}\n\n`
  );
}

/**
 * @param port        TCP port to listen on.
 * @param captureFile Absolute path where the most-recent upstream chat
 *   request body (what the BACKEND sends to LM Studio's /api/v1/chat — i.e.
 *   AFTER RAG augmentation + pinned-memory injection + previous_response_id
 *   threading) is written as JSON. Tests read this to assert on the real
 *   wire payload the model would see. This is the ONLY place RAG/memory
 *   context is observable: augmentation happens inside the backend's
 *   stream_chat, between the FE→backend request and the backend→LM Studio
 *   request — so intercepting the FE fetch would miss it entirely.
 */
function startStubServer(port: number, captureFile: string): Promise<Server> {
  return new Promise((resolve, reject) => {
    const server = createServer((req, res) => {
      // POST /api/v1/chat → SSE fixture (native LM Studio chat path).
      if (req.method === "POST" && req.url?.startsWith("/api/v1/chat")) {
        // Capture the full upstream request body so tests can assert on the
        // RAG / pinned-memory / chain context the backend injected.
        let chatBody = "";
        req.on("data", (chunk: Buffer) => { chatBody += chunk.toString(); });
        req.on("end", () => {
          try {
            writeFileSync(captureFile, chatBody);
          } catch { /* best-effort capture — never fail the stream */ }
          res.writeHead(200, {
            "Content-Type": "text/event-stream",
            "Cache-Control": "no-cache",
            Connection: "keep-alive",
          });
          res.write(buildSseFixture("Hello from stub LM Studio."));
          res.end();
        });
        return;
      }
      // GET /api/v1/models → deterministic stub model list.
      // Returns one loaded model so the SPA's model selector is populated and
      // streaming requests include a valid model field (required by the backend).
      // Key is "models" (not "data") per the LM Studio native /api/v1/models
      // wire format read by models_service.py line 196: data.get("models", []).
      //
      // WIRE-SHAPE FIX (2026-06-19): the native /api/v1/models response carries
      // `loaded_instances` as an ARRAY of {id, config} instance objects — NOT
      // an integer. models_service._probe_upstream (line 669) extracts
      // `loaded_instance_ids` from `loaded_instances[*].id`, then normalises
      // `loaded_instances` to len() for the FE. The prior stub returned an int
      // (loaded_instances: 1), so `loaded_instance_ids` came out EMPTY and
      // resolve_to_loaded_or_fallback judged "no LLM loaded" → every stream
      // 'upstream_unavailable'-erroed (broke flow-01 streaming + all RAG/memory
      // flows). Return the real array shape so a wire instance id resolves.
      if (req.method === "GET" && req.url?.startsWith("/api/v1/models")) {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            models: [
              {
                key: "stub-model-e2e",
                type: "llm",
                displayName: "Stub E2E Model",
                architecture: "llama",
                // quantization is the shape models_service.QuantizationInfo
                // validates: {name: str, bits_per_weight?: int}.  The prior
                // string form ("Q4_K_M") failed Pydantic validation, dropping
                // the model silently and breaking every slash-command flow
                // test that needed the model selector populated.
                quantization: { name: "Q4_K_M", bits_per_weight: 4 },
                maxContextLength: 4096,
                // Array of {id, config} — the native wire shape. The id becomes
                // the wire model id resolve_to_loaded_or_fallback returns.
                loaded_instances: [
                  { id: "stub-model-e2e:0", config: { context_length: 4096 } },
                ],
                sizeBytes: 0,
                paramsString: "7B",
                // capabilities is required by ModelInfo.model_validate().
                // Omitting this caused the backend to skip the model silently,
                // returning an empty list to the frontend and breaking the
                // defaultModel fallback chain (SA-2026-003 fix).
                capabilities: {
                  vision: false,
                  trained_for_tool_use: false,
                },
              },
              // Stub embedding model — required for document upload + RAG
              // chunking (documents_service.py resolves the embedding model
              // by filtering loaded models for type === "embedding").
              {
                key: "stub-embed-e2e",
                type: "embedding",
                displayName: "Stub E2E Embedding Model",
                architecture: "bert",
                quantization: { name: "Q8_0", bits_per_weight: 8 },
                maxContextLength: 512,
                loaded_instances: [
                  { id: "stub-embed-e2e:0", config: { context_length: 512 } },
                ],
                sizeBytes: 0,
                paramsString: "33M",
                capabilities: {
                  vision: false,
                  trained_for_tool_use: false,
                },
              },
              // Stub UNLOADED model — required for admin lifecycle tests
              // (flow-22a + 22b) that need a "Load" button to be visible.
              // An EMPTY loaded_instances array surfaces in AdminModels.tsx as a
              // row with the Load action enabled (loaded_instance_ids == []).
              {
                key: "stub-unloaded-e2e",
                type: "llm",
                displayName: "Stub E2E Unloaded Model",
                architecture: "llama",
                quantization: { name: "Q5_K_M", bits_per_weight: 5 },
                maxContextLength: 4096,
                loaded_instances: [],
                sizeBytes: 0,
                paramsString: "7B",
                capabilities: {
                  vision: false,
                  trained_for_tool_use: false,
                },
              },
            ],
          })
        );
        return;
      }
      // POST /v1/chat/completions (compat endpoint)
      if (req.method === "POST" && req.url?.startsWith("/v1/chat/completions")) {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(
          JSON.stringify({
            id: "stub-1",
            choices: [{ message: { role: "assistant", content: "Hello from stub." } }],
          })
        );
        return;
      }
      // POST /v1/embeddings — stub for document upload RAG chunking.
      // Returns a single 4-dimensional unit-vector embedding so the
      // documents_service.upload_document() embedding step succeeds without
      // a real LM Studio embedding model loaded.
      // Shape mirrors the OpenAI-compat embeddings wire format that
      // EmbeddingClient._extract_embeddings() expects:
      //   { "data": [{ "index": 0, "embedding": [0.1, 0.0, 0.0, 0.0] }] }
      //
      // PATH FIX (2026-06-19): EmbeddingClient (src/lmchat/embedding/client.py
      // line 49-51) posts to the OpenAI-compat "/v1/embeddings" path — NOT the
      // native "/api/v1/embeddings" path. The native path "does not exist and
      // returns 404" per the client's own comment. The original stub only
      // matched "/api/v1/embeddings", so every embedding call 404'd →
      // EmbeddingUpstreamError → 500 on document upload. This silently broke
      // ALL document-RAG e2e tests (flow-12 PDF, flow-27/28 RAG). Match both
      // paths so the stub serves whichever the client uses.
      if (
        req.method === "POST" &&
        (req.url?.startsWith("/v1/embeddings") ||
          req.url?.startsWith("/api/v1/embeddings"))
      ) {
        let body = "";
        req.on("data", (chunk: Buffer) => { body += chunk.toString(); });
        req.on("end", () => {
          let inputCount = 1;
          try {
            const parsed = JSON.parse(body) as { input?: unknown };
            const inp = parsed.input;
            if (Array.isArray(inp)) inputCount = Math.max(1, inp.length);
          } catch { /* keep inputCount=1 */ }
          const data = Array.from({ length: inputCount }, (_, i) => ({
            index: i,
            embedding: [0.1, 0.0, 0.0, 0.0],
          }));
          res.writeHead(200, { "Content-Type": "application/json" });
          res.end(JSON.stringify({ data, model: "stub-embed-e2e" }));
        });
        return;
      }
      // Fallback 404
      res.writeHead(404);
      res.end();
    });

    server.listen(port, "127.0.0.1", () => { resolve(server); });
    server.on("error", reject);
  });
}

// ---------------------------------------------------------------------------
// Backend (uvicorn) spawn helpers
// ---------------------------------------------------------------------------

// ESM-compatible __dirname substitute.
const __filename = fileURLToPath(import.meta.url);
const _thisDir = dirname(__filename);

/**
 * Absolute path to the repo root.
 * _fixtures.ts lives at: web/tests/e2e-live/_fixtures.ts
 * So repo root = _thisDir / ".." / ".." / ".."
 */
const REPO_ROOT = join(_thisDir, "..", "..", "..");

async function waitForHealthz(baseURL: string, timeoutMs = 30_000): Promise<void> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const resp = await fetch(`${baseURL}/healthz`);
      if (resp.ok) return;
    } catch {
      // Server not up yet — keep polling.
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error(`Backend at ${baseURL} did not become healthy within ${String(timeoutMs)}ms`);
}

/**
 * Seed a test user directly into the SQLite DB using a low-cost scrypt hash.
 *
 * The HTTP /api/auth/register endpoint uses production scrypt (N=2^17,
 * ~128 MiB) which exceeds OpenSSL's default maxmem on macOS in this
 * environment.  Direct DB seeding with N=2^10 (low-cost, same format) is
 * the same strategy used by the pytest suite (see tests/services/
 * test_auth_service.py, _create_user_low_cost).
 *
 * The Python one-liner runs inside the repo venv via `uv run python`.
 *
 * @param isAdmin - When true, inserts the row with is_admin=1 instead of 0.
 */
function seedTestUserViaDb(
  dbPath: string,
  username: string,
  password: string,
  isAdmin = false
): void {
  // Python inline script: hashes with low-cost scrypt and INSERTs into users.
  // Use raw string concatenation (not a JS template literal) to prevent
  // JS from interpreting Python's ${...} f-string placeholders.
  const adminFlag = isAdmin ? "1" : "0";
  const script = [
    "import hashlib, base64, secrets, sqlite3, sys",
    "db = sys.argv[1]; username = sys.argv[2]; pw = sys.argv[3]; is_admin = int(sys.argv[4])",
    "salt = secrets.token_bytes(16)",
    "h = hashlib.scrypt(pw.encode(), salt=salt, n=2**10, r=8, p=1, dklen=32)",
    "salt_b64 = base64.b64encode(salt).decode()",
    "hash_b64 = base64.b64encode(h).decode()",
    'pw_hash = "scrypt$1024$8$1$" + salt_b64 + "$" + hash_b64',
    "con = sqlite3.connect(db)",
    "cur = con.cursor()",
    'cur.execute("INSERT OR IGNORE INTO users (username, password_hash, is_admin) VALUES (?,?,?)", (username, pw_hash, is_admin))',
    "con.commit(); con.close()",
    'print("seeded")',
  ].join("\n");

  const result = spawnSync(
    "uv",
    ["run", "python", "-c", script, dbPath, username, password, adminFlag],
    { cwd: REPO_ROOT, encoding: "utf-8" }
  );
  if (result.status !== 0) {
    throw new Error(
      `Failed to seed test user via DB: ${result.stderr}`
    );
  }
}

// ---------------------------------------------------------------------------
// Fixture types
// ---------------------------------------------------------------------------

// Split test-scoped vs worker-scoped fixtures: Playwright's extend<T, W>
// takes two separate type parameters, and every fixture below except
// cleanState is registered with `{ scope: "worker" }` — declaring them all
// under one type parameter (as this file used to) types every fixture as
// test-scoped, which doesn't match the { scope: "worker" } passed at each
// registration site below.
type LiveBackendWorkerFixtures = {
  /** The base URL for the live FastAPI backend (e.g. http://127.0.0.1:59123). */
  backendURL: string;
  /** Absolute path to the ephemeral SQLite DB for the worker. */
  dbPath: string;
  /**
   * Absolute path to the per-worker capture file holding the most-recent
   * upstream LM Studio chat request body (the backend→LM Studio payload,
   * AFTER RAG augmentation + pinned-memory injection + chain threading).
   * The stub LM Studio writes it on each /api/v1/chat POST; tests read it to
   * assert on the real context the model would see.
   */
  chatCaptureFile: string;
  /**
   * Real-upstream (LMCHAT_REAL_UPSTREAM=1) only: absolute path to the tee'd
   * uvicorn log for the worker's backend. Grey-box dogfood journeys grep it for
   * fail-soft background-op outcomes that never surface in the UI. The file does
   * not exist in stub mode.
   */
  backendLogPath: string;
  /** Pre-registered test username (non-admin). */
  testUsername: string;
  /** Pre-registered test password (non-admin). */
  testPassword: string;
  /** Pre-registered admin test username (is_admin=true). */
  adminUsername: string;
  /** Pre-registered admin test password. */
  adminPassword: string;
};

type LiveBackendTestFixtures = {
  /**
   * Auto-fixture: truncates per-test transient tables (chats, sub_sessions,
   * sub_session_messages, messages, documents, pinned_insights, shares,
   * folders, prompts) BEFORE each
   * test runs.  Preserves the `users` table so seeded testUsername /
   * adminUsername stay valid.  Without this every test sees the
   * accumulated state of every prior test in the worker, which causes
   * the a11y sweep's "empty page" assertions to fail and the
   * page-render-smoke "no console errors" checks to trip on chat-list
   * fetches from prior tests.
   */
  cleanState: undefined;
};

// ---------------------------------------------------------------------------
// Fixture implementation
// ---------------------------------------------------------------------------

export const test = base.extend<LiveBackendTestFixtures, LiveBackendWorkerFixtures>({
  backendURL: [
    // eslint-disable-next-line no-empty-pattern
    async ({}, use) => {
      const backendPort = await findFreePort();
      const stubPort = await findFreePort();
      const _dbPath = `/tmp/lmchat-e2e-${String(backendPort)}.db`;
      const baseURL = `http://127.0.0.1:${String(backendPort)}`;
      // Per-worker capture file for the upstream LM Studio chat request body
      // (keyed by backend port so the chatCaptureFile fixture can reconstruct
      // it the same way dbPath does). Written by the stub on each /api/v1/chat.
      const _captureFile = `/tmp/lmchat-e2e-chat-${String(backendPort)}.json`;
      // Real-upstream (dogfood) only: uvicorn log tee, so grey-box journeys can
      // assert on fail-soft background ops (distill/title/followups) that are
      // invisible in the UI. Unused in stub mode.
      const _logPath = `/tmp/lmchat-dogfood-${String(backendPort)}.log`;

      // 1. Start the stub LM Studio server — SKIPPED in real-upstream mode,
      // where the backend talks to the operator's actual LM Studio instead.
      const stubServer: Server | null = REAL_UPSTREAM
        ? null
        : await startStubServer(stubPort, _captureFile);

      // 2. Spawn uvicorn.
      const env: NodeJS.ProcessEnv = {
        ...process.env,
        DATABASE_URL: `sqlite+aiosqlite:///${_dbPath}`,
        LM_CHAT_SECRET: "e2e-test-secret-32bytes-entropy!!",
        LM_CHAT_ENV: "test",
        // Real upstream → the operator's LM Studio (+ key); stub → the local
        // replay server on stubPort.
        LM_STUDIO_BASE_URL: REAL_UPSTREAM
          ? REAL_LMSTUDIO_URL
          : `http://127.0.0.1:${String(stubPort)}`,
        ...(REAL_UPSTREAM && REAL_LMSTUDIO_KEY
          ? { LM_STUDIO_API_KEY: REAL_LMSTUDIO_KEY }
          : {}),
        // Disable TOTP requirement for e2e tests.
        LM_CHAT_TOTP_REQUIRED: "false",
        // Raise login rate limit for e2e: the full suite issues 20+ logins
        // across a11y, ab-compare, css-vars, and legacy-parity tests.  The
        // default burst=10 exhausts mid-suite, causing loginAndWait to timeout
        // on waitForURL when the 429 keeps the SPA on /login.
        // See web/tests/e2e-live/legacy-parity.spec.ts DEFERRED.md §6.
        LM_CHAT_LOGIN_RATE_LIMIT_PER_MIN: "1000",
        LM_CHAT_LOGIN_RATE_LIMIT_PER_IP_PER_MIN: "1000",
      };

      const uvicorn: ChildProcess = spawn(
        "uv",
        [
          "run",
          "uvicorn",
          "lmchat.app:app",
          "--host",
          "127.0.0.1",
          "--port",
          String(backendPort),
          "--log-level",
          // Real-upstream grey-box assertions need INFO (stream.distill.done et
          // al. log at info); the stub suite stays quiet at warning.
          REAL_UPSTREAM ? "info" : "warning",
        ],
        {
          cwd: REPO_ROOT,
          env,
          stdio: "pipe",
        }
      );

      // 2a. Real-upstream: tee uvicorn's log to a file for grey-box assertions.
      // This ALSO drains the stdout/stderr pipes — necessary at info level so a
      // chatty backend can't fill the pipe buffer and block.
      if (REAL_UPSTREAM) {
        const logStream = createWriteStream(_logPath, { flags: "w" });
        uvicorn.stdout?.pipe(logStream);
        uvicorn.stderr?.pipe(logStream);
      }

      // 3. Wait for the backend to become healthy.
      try {
        await waitForHealthz(baseURL);
      } catch (err) {
        uvicorn.kill("SIGTERM");
        if (stubServer) await new Promise<void>((r) => stubServer.close(() => { r(); }));
        throw err;
      }

      // 3a. Seed a placeholder user so /api/auth/setup_status returns
      // needs_setup=false on the FIRST request after the backend boots.
      // Without this, tests that don't explicitly request testUsername /
      // adminUsername (a11y sweep, slash-command flows that navigate to
      // /login first) see the WelcomeWizard surface instead of the
      // standard sign-in form — Login's getByLabel("Username") then
      // can't find the field and the test times out.  The testUsername
      // and adminUsername fixtures still seed their own rows for tests
      // that need to authenticate.
      try {
        seedTestUserViaDb(_dbPath, "setup_bypass", "bypass_seed_pw");
      } catch (err) {
        uvicorn.kill("SIGTERM");
        if (stubServer) await new Promise<void>((r) => stubServer.close(() => { r(); }));
        throw err;
      }

      // 4. Yield the base URL.
      await use(baseURL);

      // 5. Teardown.
      uvicorn.kill("SIGTERM");
      await new Promise<void>((r) => setTimeout(r, 500));
      if (stubServer) await new Promise<void>((r) => stubServer.close(() => { r(); }));
      if (existsSync(_dbPath)) {
        await rm(_dbPath, { force: true });
      }
      if (existsSync(_captureFile)) {
        await rm(_captureFile, { force: true });
      }
      // Keep the dogfood uvicorn log on disk for post-run inspection; it is
      // per-port and overwritten (flags:"w") on the next run, so it never
      // accumulates. Journeys read it via the backendLogPath fixture.
    },
    { scope: "worker" },
  ],

  // Real-upstream only: absolute path to the tee'd uvicorn log for this worker's
  // backend. Grey-box dogfood journeys grep it for fail-soft background-op
  // outcomes (stream.distill.done / *_oob_failed / generate_title.upstream_error)
  // that never surface in the UI. In stub mode the file does not exist.
  backendLogPath: [
    async ({ backendURL }, use) => {
      const port = new URL(backendURL).port;
      await use(`/tmp/lmchat-dogfood-${port}.log`);
    },
    { scope: "worker" },
  ],

  dbPath: [
    async ({ backendURL }, use) => {
      // Reconstruct the dbPath from backendURL port (matches backendURL fixture).
      const port = new URL(backendURL).port;
      await use(`/tmp/lmchat-e2e-${port}.db`);
    },
    { scope: "worker" },
  ],

  chatCaptureFile: [
    async ({ backendURL }, use) => {
      // Reconstruct the capture-file path from the backend port (matches the
      // backendURL fixture's _captureFile). The stub LM Studio writes the most
      // recent upstream chat request body here on each /api/v1/chat POST.
      const port = new URL(backendURL).port;
      await use(`/tmp/lmchat-e2e-chat-${port}.json`);
    },
    { scope: "worker" },
  ],

  testUsername: [
    async ({ dbPath }, use) => {
      const username = "e2euser";
      const password = "e2epassword123";
      // Seed directly into the SQLite DB with low-cost scrypt (N=2^10).
      // Using direct DB insertion bypasses the HTTP /api/auth/register
      // endpoint which uses production scrypt (N=2^17, ~128 MiB) that
      // exceeds OpenSSL maxmem in this environment.  This mirrors the
      // _create_user_low_cost helper in tests/services/test_auth_service.py.
      seedTestUserViaDb(dbPath, username, password);
      await use(username);
    },
    { scope: "worker" },
  ],

  testPassword: [
    // eslint-disable-next-line no-empty-pattern
    async ({}, use) => {
      await use("e2epassword123");
    },
    { scope: "worker" },
  ],

  adminUsername: [
    async ({ dbPath }, use) => {
      const username = "e2eadmin";
      const password = "e2eadminpassword123";
      // Seed directly into the SQLite DB with is_admin=1.
      seedTestUserViaDb(dbPath, username, password, true);
      await use(username);
    },
    { scope: "worker" },
  ],

  adminPassword: [
    // eslint-disable-next-line no-empty-pattern
    async ({}, use) => {
      await use("e2eadminpassword123");
    },
    { scope: "worker" },
  ],

  // Auto-fixture: clears per-test transient tables before each test.
  // The previous behaviour shared all DB state across the worker, so
  // test N saw state from tests 1..N-1.  This made a11y "empty page"
  // assertions fail and console-error sweeps trip on chats fetched
  // by prior tests that no longer exist for the current user.
  cleanState: [
    async ({ dbPath }, use) => {
      // Order matters: child tables BEFORE parents. `document_chunks` MUST be
      // cleared with (in fact, before) `documents` — otherwise the orphaned
      // chunks from a prior test survive, and the next upload that reuses a
      // recycled documents.id collides on the
      // (document_id, ordinal) UNIQUE index → 500 on upload (the failure that
      // made flow-27b/flow-28 flake when run after a sibling RAG test in the
      // same worker). Deleting document_chunks also fires the FTS5 delete
      // trigger, keeping document_chunks_fts in sync. `projects` is cleared so
      // project-scoped RAG/memory isolation tests start from a clean slate.
      // `sub_session_messages`/`sub_sessions` MUST be cleared too (and before
      // `chats`, their parent) — otherwise a prior spec's sub-sessions survive
      // this truncation, and because `chats` DOES reset (ids restart at 1) the
      // next spec's recycled chat_id inherits the orphans. That made j7's
      // `GET /chats/{id}/sub-sessions` count report 3 (its own + two leaked from
      // j3's research turn + summary) instead of 1 — a misleading diagnostic and
      // a fragile status assertion, root-caused via instrumented dogfood traces
      // 2026-08-14 (NOT a product bug: the app creates one session per turn).
      const tables = [
        "document_chunks", "sub_session_messages", "sub_sessions",
        "chats", "messages", "documents", "projects",
        "pinned_insights", "shares", "folders", "prompts", "model_overrides",
      ];
      const script = [
        "import sqlite3, sys",
        "db = sys.argv[1]; tables = sys.argv[2].split(',')",
        "con = sqlite3.connect(db); cur = con.cursor()",
        "for t in tables:",
        "    try: cur.execute(f'DELETE FROM {t}')",
        "    except sqlite3.OperationalError: pass  # table may not exist on a fresh schema",
        "con.commit(); con.close()",
      ].join("\n");
      spawnSync(
        "uv",
        ["run", "python", "-c", script, dbPath, tables.join(",")],
        { cwd: REPO_ROOT, encoding: "utf-8" }
      );
      await use(undefined);
    },
    { auto: true },
  ],
});

export { expect } from "@playwright/test";
