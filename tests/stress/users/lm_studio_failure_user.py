# SPDX-License-Identifier: Apache-2.0
"""S5 — LM Studio failure modes (toxiproxy or httpx-mock driven).

Profile:
    Mid-test, toxiproxy injects: 502 for 30s -> 60s slow for 30s ->
    drop-all for 30s -> recover.
    Assertions: graceful 503 with Retry-After header; 30s timeout
    policy honored; connection pool recovers.

The chaos engine timeline is driven by the orchestrator
(``run_stress.py`` starts a background task that walks
``S5_TIMELINE`` via the active chaos backend).  This user class issues
streaming requests throughout the run and tags each response by which
phase was active.

The user's job is to keep firing — the assertion that the server
degrades gracefully (no crash, no pool exhaustion) belongs to the
orchestrator and the post-run RSS/FD invariants.
"""
from __future__ import annotations

import os
import time

from locust import between, tag, task

from ..data.realistic import realistic_chat_title, realistic_user_message
from ._sse import iter_sse
from .base_user import StressUser, warmup_tag

# See tests/stress/users/streaming_user.py for the model-resolution
# contract: orchestrator probes LM Studio and exports STRESS_MODEL_ID.
STRESS_MODEL_ID = os.environ.get("STRESS_MODEL_ID", "")
assert STRESS_MODEL_ID, (
    "STRESS_MODEL_ID is unset. Run via `python -m tests.stress.run_stress` "
    "(or `make stress-baseline` / `make stress-test`)."
)


@tag("s5", "lm-studio-failure")  # type: ignore[arg-type]
class LmStudioFailureUser(StressUser):
    """Fire streaming + non-streaming requests through the chaos backend."""

    wait_time = between(0.5, 2.0)

    @task(2)
    def stream_against_chaos(self) -> None:
        chat_id = self._create_chat()
        if chat_id is None:
            return
        body = {
            "chat_id": chat_id,
            "payload": {
                "model": STRESS_MODEL_ID,
                "input": [
                    {"type": "text", "content": realistic_user_message()}
                ],
                "max_output_tokens": 32,
                "stream": True,
                "store": False,
            },
        }
        start = time.perf_counter()
        with self.client.post(
            "/api/chat/stream",
            json=body,
            stream=True,
            name=warmup_tag("POST /api/chat/stream (chaos)"),
            catch_response=True,
            timeout=35.0,
        ) as resp:
            # During failure-injected windows, accept:
            #   200 + clean SSE OR error frame within budget
            #   502 / 503 / 504 with sensible body
            #   200 + premature EOF (truncated stream)
            if resp.status_code == 503:
                # Server told us to back off — that's the graceful path.
                ra = resp.headers.get("retry-after")
                if not ra:
                    resp.failure("503 without Retry-After header")
                    return
                resp.success()
                return
            if 500 <= resp.status_code < 600:
                # Other 5xx — still acceptable (the upstream IS down);
                # the orchestrator's RSS/FD check is the real assertion.
                resp.success()
                return
            if resp.status_code != 200:
                resp.failure(f"unexpected {resp.status_code}")
                return
            # 200: drain whatever the upstream produces, accept early EOF.
            try:
                for _event in iter_sse(resp.iter_lines(decode_unicode=False)):
                    if time.perf_counter() - start > 30.0:
                        break
            except Exception:  # noqa: BLE001
                # Any framing-level exception is fine here — the chaos
                # backend may have torn the stream.
                pass
            resp.success()

    @task(1)
    def metadata_against_chaos(self) -> None:
        """Cheap GET request to verify non-streaming paths also degrade well."""
        with self.client.get(
            "/api/chats",
            name=warmup_tag("GET /api/chats (chaos)"),
            catch_response=True,
            timeout=15.0,
        ) as resp:
            if resp.status_code in (200, 503):
                resp.success()
            elif 500 <= resp.status_code < 600:
                resp.success()  # tolerated
            else:
                resp.failure(f"unexpected {resp.status_code}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_chat(self) -> int | None:
        resp = self.client.post(
            "/api/chats",
            data={"title": realistic_chat_title(), "incognito": "false"},
            name=warmup_tag("POST /api/chats"),
            catch_response=True,
        )
        with resp:
            if resp.status_code != 201:
                resp.failure(f"create failed: {resp.status_code}")
                return None
            resp.success()
            try:
                return int(resp.json()["id"])
            except Exception as exc:  # noqa: BLE001
                resp.failure(f"body parse: {exc}")
                return None
