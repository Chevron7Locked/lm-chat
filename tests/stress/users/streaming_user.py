# SPDX-License-Identifier: Apache-2.0
"""S1 — 200 concurrent SSE streams.

Profile:
    200 virtual users, each opens one SSE stream against
    /api/chats/{id}/stream, 60s duration.
    Assertions: all 200 complete with 200, content-type text/event-stream,
    zero 5xx, zero response_id collisions, p95 stream time < 30s.

Implementation
--------------
Each user logs in -> creates a chat -> issues POST /api/chat/stream and
iterates the SSE stream to completion.  The endpoint takes a JSON body
``{chat_id, payload}`` where payload is a CanonicalChatRequest; we
populate ``model`` from STRESS_MODEL_ID (default the loaded model).

The Locust event name includes ``stream`` so the SLO reporter can
filter on it.  Response time is the total stream completion duration.
TTFT (time to first byte) is recorded as a separate event
``stream-ttft``.
"""
from __future__ import annotations

import os
import time
from typing import Any

from locust import LoadTestShape, between, tag, task

from ..data.realistic import realistic_chat_title, realistic_user_message
from ._sse import SSEParseError, iter_sse
from .base_user import StressUser, warmup_tag

# STRESS_MODEL_ID is resolved at orchestrator startup via _probe_lm_studio_model()
# and exported into os.environ BEFORE Locust workers boot. No default: a
# missed export means the orchestrator was bypassed (e.g. someone running
# `locust -f locustfile.py` directly), and we want a loud failure rather
# than a 400 model_not_found mid-load.
STRESS_MODEL_ID = os.environ.get("STRESS_MODEL_ID", "")
assert STRESS_MODEL_ID, (
    "STRESS_MODEL_ID is unset. Run via `python -m tests.stress.run_stress` "
    "(or `make stress-baseline` / `make stress-test`), which probes "
    "GET /api/v0/models on LM Studio and exports the loaded model id."
)
STRESS_MAX_TOKENS = int(os.environ.get("STRESS_MAX_TOKENS", "64"))


def _stream_payload(chat_id: int, message: str) -> dict[str, Any]:
    """Build the wire body for POST /api/chat/stream."""
    return {
        "chat_id": chat_id,
        "payload": {
            "model": STRESS_MODEL_ID,
            "input": [{"type": "text", "content": message}],
            # Keep generation short so the test occupies LM Studio for
            # roughly the documented 10-minute budget rather than burning
            # the model for unlimited tokens.
            "max_output_tokens": STRESS_MAX_TOKENS,
            "stream": True,
            "store": False,
        },
    }


@tag("s1", "streaming")  # type: ignore[arg-type]
class StreamingUser(StressUser):
    """Open one SSE stream per task iteration; measure completion + TTFT."""

    wait_time = between(0.5, 1.5)

    def _create_chat(self) -> int | None:
        """Create a new chat; return its id, or None on failure."""
        resp = self.client.post(
            "/api/chats",
            data={
                "title": realistic_chat_title(),
                "incognito": "false",
            },
            name=warmup_tag("POST /api/chats"),
            catch_response=True,
        )
        with resp:
            if resp.status_code != 201:
                resp.failure(
                    f"chat create failed: {resp.status_code} {resp.text[:200]!r}"
                )
                return None
            resp.success()
            try:
                return int(resp.json()["id"])
            except Exception as exc:  # noqa: BLE001
                resp.failure(f"chat create body parse: {exc}")
                return None

    @task
    def stream_once(self) -> None:
        chat_id = self._create_chat()
        if chat_id is None:
            return

        message = realistic_user_message()
        body = _stream_payload(chat_id, message)
        event_name = warmup_tag("POST /api/chat/stream")
        ttft_name = warmup_tag("stream-ttft")

        start = time.perf_counter()
        first_token_ts: float | None = None
        bytes_seen = 0
        response_ids: set[str] = set()
        chat_end_seen = False
        had_5xx = False

        try:
            with self.client.post(
                "/api/chat/stream",
                json=body,
                stream=True,
                name=event_name,
                catch_response=True,
            ) as resp:
                if resp.status_code >= 500:
                    had_5xx = True
                    resp.failure(f"stream returned 5xx: {resp.status_code}")
                    return
                if resp.status_code != 200:
                    resp.failure(
                        f"stream returned {resp.status_code}: {resp.text[:200]!r}"
                    )
                    return
                ctype = resp.headers.get("content-type", "")
                if "text/event-stream" not in ctype:
                    resp.failure(f"bad content-type: {ctype!r}")
                    return

                try:
                    for event in iter_sse(
                        resp.iter_lines(decode_unicode=False)
                    ):
                        if first_token_ts is None:
                            first_token_ts = time.perf_counter()
                            self._fire_event(
                                ttft_name,
                                int((first_token_ts - start) * 1000),
                            )
                        bytes_seen += len(event.raw_data)
                        if event.event == "chat.end":
                            chat_end_seen = True
                            data = event.data
                            if isinstance(data, dict):
                                rid = data.get("response_id")
                                if isinstance(rid, str):
                                    response_ids.add(rid)
                            break
                        if event.event == "error":
                            resp.failure(
                                f"stream error frame: {event.raw_data[:200]!r}"
                            )
                            return
                except SSEParseError as exc:
                    resp.failure(f"SSE parse error: {exc}")
                    return

                if not chat_end_seen:
                    resp.failure("stream ended without chat.end")
                    return
                resp.success()
        finally:
            elapsed_ms = int((time.perf_counter() - start) * 1000)
            # Stash the response_id on the user for the orchestrator
            # to scrape (response_id-collision assertion).
            if response_ids:
                _collected = getattr(self, "_collected_response_ids", set())
                _collected.update(response_ids)
                self._collected_response_ids = _collected
            # Locust already recorded the POST event via catch_response.
            # We mirror to a friendly "stream-complete" event so the
            # reporter sees an aggregate that excludes the connection
            # establishment time.
            if not had_5xx and chat_end_seen:
                self._fire_event(
                    warmup_tag("stream-complete"),
                    elapsed_ms,
                    bytes_seen,
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fire_event(
        self,
        name: str,
        response_time_ms: int,
        response_length: int = 0,
    ) -> None:
        """Fire a synthetic Locust event for sub-measurements."""
        self.environment.events.request.fire(
            request_type="SSE",
            name=name,
            response_time=response_time_ms,
            response_length=response_length,
            exception=None,
            context={},
        )


class StreamingShape(LoadTestShape):
    """Ramp to 200 users over 30s, hold for 60s, ramp down.

    Used when the admin invokes the harness with
    ``--shape streaming`` (default is the orchestrator's standard
    spawn rate).  Lives here for self-contained scenario runs.
    """

    target_users = int(os.environ.get("STRESS_S1_USERS", "200"))
    ramp_up_sec = int(os.environ.get("STRESS_S1_RAMP_UP", "30"))
    hold_sec = int(os.environ.get("STRESS_S1_HOLD", "60"))
    ramp_down_sec = int(os.environ.get("STRESS_S1_RAMP_DOWN", "10"))

    def tick(self) -> tuple[int, float] | None:
        elapsed = self.get_run_time()
        if elapsed < self.ramp_up_sec:
            user_count = int(
                self.target_users * (elapsed / self.ramp_up_sec)
            )
            return max(1, user_count), self.target_users / self.ramp_up_sec
        if elapsed < self.ramp_up_sec + self.hold_sec:
            return self.target_users, self.target_users
        if elapsed < self.ramp_up_sec + self.hold_sec + self.ramp_down_sec:
            return self.target_users, self.target_users
        return None
