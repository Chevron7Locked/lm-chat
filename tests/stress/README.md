# P14 stress-test harness — operator runbook

Local exhaustive load test against the operator's real LM Studio.
Required to pass before every deployment; wired into `make validate-deploy`.

This document explains the prerequisites, the two operating modes
(baseline vs gated), how to read the output, how to attach Jaeger for
failure debugging, and how to add a new scenario.

The harness is a closed set of ten scenarios by design; if anything below
seems to conflict with how the harness actually behaves, that's a bug —
file it.

---

## Prerequisites

1. **LM Studio running** at `LM_STUDIO_BASE_URL` (default `http://localhost:1234`)
   with **at least one model loaded**. The harness uses whichever model
   reports `state == "loaded"` first; if none are loaded, warm-up is
   skipped and the streaming-heavy scenarios will time out.

2. **`.env.local` sourced.** The orchestrator reads `LM_STUDIO_API_KEY`
   to talk to LM Studio. Source the file before the make target:

       set -a && source .env.local && set +a
       make stress-baseline

3. **`uv sync --dev` has been run** so the harness deps are installed:
   `locust>=2.44`, `faker>=13`, `psutil>=5.9`,
   `opentelemetry-{sdk,exporter-otlp,instrumentation-fastapi,instrumentation-httpx}`.

4. **No process is bound to port 8765.** The orchestrator boots a
   private lm-chat backend on that port; override with
   `STRESS_LMCHAT_PORT=<n>` if it collides.

5. *(optional, S5 fidelity)* **Toxiproxy on PATH** for true TCP-level
   fault injection. Falls back to an in-process httpx fault transport
   when unavailable — same assertions, lower fidelity. Install via
   `brew install toxiproxy` and ensure `toxiproxy-cli` is reachable.

6. *(optional, --with-tracing)* **Docker available** for the Jaeger
   compose stack. Without docker the orchestrator prints a one-line
   warning and continues without tracing.

---

## First-time setup — capture the baseline

The baseline run measures **your hardware's** p50/p95/p99 under the
ten stress scenarios, then locks the result for future regression
checks. Run this once per hardware fingerprint (CPU + RAM + LM Studio
model id; see `target/stress/baseline.json` for the captured fields):

    set -a && source .env.local && set +a
    make stress-baseline

Roughly 15–20 minutes of wall clock against a single loaded model. The
run writes two files:

- `target/stress/baseline.json` — every baseline run overwrites this.
- `tests/stress/baseline.locked.json` — written ONLY on the first
  successful baseline. **Commit this file** so future runs gate
  against the same numbers. If you need to refresh the baseline (new
  hardware, intentionally regressed perf), delete it and re-run.

The orchestrator exits 0 on success; the SLO report is at
`target/stress/slo_report.json`.

---

## Regular use — gated run

After `baseline.locked.json` exists, the default invocation is the
gated run:

    set -a && source .env.local && set +a
    make stress-test

Gates:

- **Hard ceilings.** p95 < 60 s, p99 < 120 s, TTFT p99 < 10 s,
  observed error budget < 0.001, peak RSS < 4 GiB, peak FD < 1024,
  peak DB conns < 50.
- **Baseline drift.** Observed p95/p99/TTFT p99 must be ≤ 1.5× the
  locked baseline. A breach exits non-zero and blocks
  `make validate-deploy`.

Useful one-liners:

    make stress-test S=1                       # only scenario 1
    make stress-test S=5                       # only the chaos scenario
    make stress-test ARGS="--warmup 0"         # skip the warm-up loop
    make stress-test ARGS="--with-tracing"     # boot Jaeger + OTel
    make stress-test ARGS="--pool-size 60"     # smaller user pool

The full scenario menu (`SCENARIOS` in `tests/stress/run_stress.py`):

| S  | tag | users | spawn/s | duration | focus                                |
|----|-----|------:|--------:|---------:|--------------------------------------|
| 1  | s1  |   200 |      20 |       90 | concurrent SSE streams               |
| 2  | s2  |    50 |      10 |       60 | R-15 incognito × share × memory     |
| 3  | s3  |    30 |       5 |       60 | concurrent CRUD on a shared chat     |
| 4  | s4  |     8 |       1 |       90 | session revocation mid-stream        |
| 5  | s5  |    20 |       5 |      130 | LM Studio failure timeline (chaos)   |
| 6  | s6  |    30 |      10 |       60 | Hypothesis adversarial inputs        |
| 7  | s7  |    25 |       5 |       60 | quota-window rollover                |
| 8  | s8  |    50 |      10 |       60 | 50 isolated users                    |
| 9  | s9  |    50 |      10 |       60 | DB integrity under contention        |
| 10 | s10 |     1 |       1 |       30 | rate-limit boundary detection        |

---

## Reading SLO reports

The orchestrator emits two report files:

- `target/stress/slo_report.json` — full JSON report (summary +
  per-scenario block + gate outcomes).
- `target/stress/junit.xml` — JUnit XML mirror for CI ingestion.

Top-level shape of `slo_report.json`:

    {
      "ok": true,
      "summary": {
        "p50_ms": ..., "p95_ms": ..., "p99_ms": ...,
        "ttft_p99_ms": ...,
        "total_requests": ...,
        "total_failures": ...,
        "error_budget_observed": 0.0...,
        "peak_rss_mb": ..., "peak_fd_count": ..., "peak_db_conns": ...
      },
      "per_scenario": {
        "S1-stream-iter": {"p50_ms": "...", "p95_ms": "...", ...},
        ...
      },
      "gate_outcomes": [
        {"name": "ceiling::p95_ms", "ok": true, "detail": "..."},
        {"name": "baseline::p99_ms", "ok": false,
         "detail": "BREACH: observed=... > ... (1.5x baseline ...)"}
      ]
    }

`per_scenario` keys are the Locust request names that each user class
emits (e.g. `stream-iter`, `crud-create`, `incognito-share-attempt`).
TTFT events use names beginning with `stream-ttft` and are reported in
the dedicated `ttft_p99_ms` summary field; the regular `p95_ms` /
`p99_ms` summaries exclude them so that "slow first byte" and "slow
full response" never get conflated.

Triage flowchart for a failing run:

1. Look at `gate_outcomes` in `slo_report.json` and find the first
   `ok: false` row. The `detail` string says *what* broke and by *how
   much*.
2. If a hard ceiling tripped, the run was unhealthy regardless of
   baseline drift. Check `summary.error_budget_observed` first
   (genuinely failed requests almost always dominate); then RSS / FD.
3. If only the baseline gate tripped, look at `per_scenario` to see
   *which* scenario regressed. Streaming regressions usually point at
   LM Studio model load or memory pressure on the host.
4. The invariants (post-load DB correctness) report through pytest's
   normal exit code, not the SLO JSON. Re-run with
   `STRESS_DATABASE_URL=sqlite+aiosqlite:///$PWD/target/stress/lmchat.db
   uv run pytest -m stress_invariant tests/stress/invariants -v` to
   reproduce a single invariant failure against the populated DB.

---

## Attaching Jaeger for failure debugging

For deep instrumentation when a scenario fails or regresses, run with
`--with-tracing`:

    set -a && source .env.local && set +a
    make stress-test ARGS="--with-tracing"

The orchestrator boots `tests/stress/tracing/compose-jaeger.yml`
(Jaeger all-in-one), sets `LM_CHAT_OTEL_ENABLED=true` on the lm-chat
backend, and exports OTLP spans to `localhost:4318`. While the run is
in flight the UI is at <http://localhost:16686>. Filter by service
name `lm-chat` and the request path or trace-id you care about.

The compose stack is torn down (`docker compose down -v`) when the
orchestrator exits, regardless of pass/fail. If you want to keep the
spans for offline triage, bring up the stack manually beforehand:

    docker compose -f tests/stress/tracing/compose-jaeger.yml up -d
    make stress-test ARGS="--with-tracing"
    # ... investigate at http://localhost:16686 ...
    docker compose -f tests/stress/tracing/compose-jaeger.yml down -v

---

## Adding a new scenario

The harness is a closed set of ten scenarios by design. Adding an
eleventh is a deliberate decision, not a routine change — coordinate it
with whoever owns the stress harness. That said, *during development*
the template is:

1. **Write a new user class** at
   `tests/stress/users/<name>_user.py` extending `StressUserBase`
   (which encapsulates login, cookie jar, and the SSE iterator).
   Tag every Locust task with the scenario tag:

       from locust import task, tag

       class MyNewUser(StressUserBase):
           wait_time = between(0.1, 1.0)

           @tag("sN")  # whichever scenario id
           @task
           def do_the_thing(self) -> None:
               # ... self.client.post(...), self.iter_sse(...), etc.

2. **Re-export it from `tests/stress/locustfile.py`** so Locust's
   tag-based selection sees it.

3. **Append a `ScenarioSpec`** to the `SCENARIOS` list in
   `tests/stress/run_stress.py`, giving it the next free `sid`,
   matching `tag`, and the load profile.

4. **(Optional) Add a post-load invariant** under
   `tests/stress/invariants/test_<thing>.py`, decorated
   `@pytest.mark.stress_invariant`, asserting the DB-level
   correctness condition that load could violate (FK consistency,
   cross-user leak, audit-log monotonicity, etc.).

5. **Verify locally** with `make stress-test S=<sid>` before running
   the full suite. New scenarios should pass against the locked
   baseline before they ship; if the new load profile changes the
   aggregate p95/p99, refresh the baseline in the same change.

---

## Troubleshooting

- **`lm-chat /healthz did not return 200`** — the private backend on
  port 8765 didn't come up. Check `target/stress/` for any stale
  state; look at lm-chat startup logs by re-running the orchestrator
  with `LM_CHAT_LOG_LEVEL=DEBUG` env.
- **`register stress_u0001 failed: 403`** — `LM_CHAT_SETUP_TOKEN`
  mismatch. The orchestrator passes `stress-setup-token` by default;
  if you've set the env to something else, either unset it for the
  stress run or set it back.
- **`chaos backend: httpx-mock (toxiproxy unavailable)`** — informational.
  S5 still runs; assertions are unchanged but fidelity is lower
  (in-process fault injection only). Install Toxiproxy for full
  TCP-level chaos.
- **All scenarios time out simultaneously** — LM Studio probably
  doesn't have a model loaded, or the loaded model isn't responding
  inside the 60 s per-request timeout. Confirm with
  `curl -s -H "Authorization: Bearer $LM_STUDIO_API_KEY" $LM_STUDIO_BASE_URL/api/v0/models | jq '.data[] | select(.state=="loaded") | .id'`.
- **`OSError: address already in use` on 8765** — leftover backend
  from a crashed run. `lsof -i:8765` to find it; `kill` it. The
  orchestrator's contextmanager tears down on normal exit but a
  SIGKILL'd parent leaks the child.
- **`port 8765 already in use`** — the orchestrator's startup guard
  caught a pre-bound process. Same fix as above:
  `lsof -i:8765 -t | xargs kill`. The guard exists because without
  it the freshly-spawned uvicorn fails to bind silently, the stale
  one answers `/healthz`, and registrations 422 with confusing
  errors that look like they belong to the new run.
- **`lm-chat did not bind port within 45s; see target/stress/lmchat_backend.log`**
  — uvicorn died at boot. The orchestrator now redirects its stdout
  + stderr to `target/stress/lmchat_backend.log` (previously
  DEVNULL'd them). `tail -200 target/stress/lmchat_backend.log` shows
  the failure mode (missing env, port collision, schema migration
  error, etc.).

---

## Files at a glance

    tests/stress/
      run_stress.py             — top-level orchestrator (this is the entrypoint)
      locustfile.py             — Locust user-class registry
      users/                    — 10 user classes (one per scenario) + SSE helper + base class
      invariants/               — 6 post-load DB invariants
      reporters/
        slo_report.py           — Locust CSV -> SLO summary JSON
        junit_xml.py            — slo_report.json -> junit.xml for CI
      chaos/
        toxiproxy_client.py     — toxiproxy CLI driver (preferred backend)
        httpx_fault_transport.py — in-process fallback when Toxiproxy absent
        failure_modes.py        — S5 timeline definition
      tracing/
        otel_setup.py           — OpenTelemetry SDK init (LM_CHAT_OTEL_ENABLED gate)
        compose-jaeger.yml      — Jaeger all-in-one for --with-tracing
      data/
        realistic.py            — Faker-backed plausible-looking inputs
        adversarial.py          — Hypothesis-backed nasty inputs (S6)
        build_corpus.py         — regenerates corpus/transcripts.jsonl
      corpus/
        transcripts.jsonl       — frozen corpus (committed)
      baseline.locked.json      — committed perf baseline (created on first --baseline run)
