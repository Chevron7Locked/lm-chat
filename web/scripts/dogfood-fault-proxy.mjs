#!/usr/bin/env node
/**
 * dogfood-fault-proxy — a thin, one-shot fault-injection reverse proxy
 * sitting between the lm-chat backend and the operator's REAL LM Studio,
 * used ONLY by j20-upstream-500-salvage.spec.ts (defect 8: an upstream
 * HTTP 500 from LM Studio killing an in-flight stream with
 * salvaged_kind: None — the answer lost entirely, not salvaged).
 *
 * WHY THIS EXISTS: the dogfood-live gate talks to a REAL LM Studio, which
 * has no supported fault-injection surface — there is no "return HTTP 500
 * on the next request" knob — and this app must never manage LM Studio's
 * own process/state (operator directive: no LM Studio state management).
 * So the fault is injected at the NETWORK layer instead: every request
 * passes through to the real LM Studio completely untouched (byte-for-byte
 * forwarded — real behavior for every dogfood journey that runs through
 * this proxy), except for exactly ONE armed request, which gets a
 * synthetic error status instead of ever reaching LM Studio.
 *
 * `make dogfood-live-fault` starts this BEFORE the backend and points
 * LMCHAT_DOGFOOD_LMSTUDIO_URL at it instead of directly at LM Studio, so
 * that one isolated run — preflight included — goes through the proxy
 * transparently. The routine `dogfood-live` target does NOT use this proxy
 * at all; it is a separate, opt-in, on-demand target so the blast radius of
 * a bug in this ~150-line script can never affect the main gate.
 *
 * Control channel (same port, reserved path prefix so it can never collide
 * with a real LM Studio route):
 *   POST /__dogfood_fault__/arm     {status?: number} — arm ONE-SHOT: the
 *                                    next POST request (any path other than
 *                                    this control prefix) gets `status`
 *                                    (default 500) plus a small JSON error
 *                                    body, WITHOUT ever reaching the real
 *                                    upstream. Auto-disarms immediately
 *                                    after firing (or on a second arm).
 *   POST /__dogfood_fault__/disarm  — cancel an armed-but-not-yet-fired fault.
 *   GET  /__dogfood_fault__/status  — {armed: bool, fired: number}
 *
 * Usage:
 *   DOGFOOD_FAULT_PROXY_TARGET=http://localhost:1234 \
 *   DOGFOOD_FAULT_PROXY_PORT=18234 \
 *     node web/scripts/dogfood-fault-proxy.mjs
 */
import http from "node:http";
import { URL } from "node:url";

const TARGET = process.env["DOGFOOD_FAULT_PROXY_TARGET"] ?? "http://localhost:1234";
const PORT = Number(process.env["DOGFOOD_FAULT_PROXY_PORT"] ?? "18234");
const CONTROL_PREFIX = "/__dogfood_fault__";

const targetUrl = new URL(TARGET);

let armed = false;
let armedStatus = 500;
let firedCount = 0;

function log(msg) {
  console.log(`[dogfood-fault-proxy] ${msg}`);
}

function readBody(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      resolve(Buffer.concat(chunks));
    });
    req.on("error", reject);
  });
}

async function handleControl(req, res, path) {
  if (path === `${CONTROL_PREFIX}/arm` && req.method === "POST") {
    const body = await readBody(req);
    let requestedStatus = 500;
    if (body.length > 0) {
      try {
        const parsed = JSON.parse(body.toString("utf-8"));
        if (typeof parsed.status === "number") requestedStatus = parsed.status;
      } catch {
        // best-effort — fall back to the default status.
      }
    }
    armed = true;
    armedStatus = requestedStatus;
    log(`ARMED — next POST returns HTTP ${String(armedStatus)} instead of reaching ${TARGET}`);
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ armed: true, status: armedStatus }));
    return true;
  }
  if (path === `${CONTROL_PREFIX}/disarm` && req.method === "POST") {
    const wasArmed = armed;
    armed = false;
    log(wasArmed ? "DISARMED (no fault fired)" : "disarm called while not armed (no-op)");
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ armed: false }));
    return true;
  }
  if (path === `${CONTROL_PREFIX}/status` && req.method === "GET") {
    res.writeHead(200, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ armed, fired: firedCount }));
    return true;
  }
  return false;
}

const server = http.createServer((req, res) => {
  void (async () => {
    const path = req.url ?? "/";
    if (path.startsWith(CONTROL_PREFIX)) {
      const handled = await handleControl(req, res, path);
      if (!handled) {
        res.writeHead(404);
        res.end();
      }
      return;
    }

    if (armed && req.method === "POST") {
      // Consume the request body so the client's own write doesn't hang,
      // but never forward it — the fault fires BEFORE the real upstream is
      // ever contacted, matching "LM Studio returned an error on the very
      // first call of this turn" (the salvaged_kind: None scenario — zero
      // content had streamed yet when the failure hit).
      await readBody(req).catch(() => undefined);
      armed = false;
      firedCount += 1;
      log(
        `FAULT FIRED (#${String(firedCount)}) — returning HTTP ${String(armedStatus)} for ` +
          `${req.method} ${path} without contacting ${TARGET}`,
      );
      res.writeHead(armedStatus, { "Content-Type": "application/json" });
      res.end(
        JSON.stringify({
          error: {
            message: "dogfood-fault-proxy: injected upstream failure",
            type: "injected_fault",
          },
        }),
      );
      return;
    }

    // Transparent passthrough — everything else, byte-for-byte, including
    // SSE/chunked streaming responses (piped, not buffered).
    const upstreamReq = http.request(
      {
        protocol: targetUrl.protocol,
        hostname: targetUrl.hostname,
        port: targetUrl.port || (targetUrl.protocol === "https:" ? 443 : 80),
        method: req.method,
        path,
        headers: { ...req.headers, host: targetUrl.host },
      },
      (upstreamRes) => {
        res.writeHead(upstreamRes.statusCode ?? 502, upstreamRes.headers);
        upstreamRes.pipe(res);
      },
    );
    upstreamReq.on("error", (err) => {
      log(`upstream request error: ${err.message}`);
      if (!res.headersSent) {
        res.writeHead(502, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ error: { message: String(err.message) } }));
      } else {
        res.end();
      }
    });
    req.pipe(upstreamReq);
  })().catch((err) => {
    log(`handler error: ${err instanceof Error ? err.message : String(err)}`);
    if (!res.headersSent) {
      res.writeHead(500);
    }
    res.end();
  });
});

server.listen(PORT, "127.0.0.1", () => {
  log(`listening on http://127.0.0.1:${String(PORT)} → forwarding to ${TARGET}`);
});

function shutdown() {
  log("shutting down");
  server.close(() => {
    process.exit(0);
  });
  // Force-exit if close() hangs (e.g. a keep-alive upstream connection
  // still open) — this is a short-lived helper process, not a service.
  setTimeout(() => {
    process.exit(0);
  }, 2000).unref();
}
process.on("SIGTERM", shutdown);
process.on("SIGINT", shutdown);
