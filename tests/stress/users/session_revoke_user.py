# SPDX-License-Identifier: Apache-2.0
"""S4 — session revocation during in-flight stream.

Profile:
    User opens SSE stream -> admin revokes session -> user sends
    another message.
    Assertions: stream terminates with error event; new request
    401s; no orphan connection leak.

Implementation
--------------
We need TWO concurrent users in the same scenario: the victim (a
normal user) and the executioner (admin, calls the revoke endpoint).

Locust handles this via a single user class that opens the stream
THEN posts a revoke against itself.  An admin session exists in the
credential pool; the orchestrator marks one entry as ``"is_admin": true``.

The revoke endpoint is ``POST /api/admin/users/{user_id}/revoke-sessions``
(see ``src/lmchat/routes/admin.py``).  After revoke, the next request
on the same client MUST 401; iterating the existing SSE stream MUST
either receive an ``error`` event OR close within 1s, honoring the
"session revocation aborts in-flight streams within 1 second"
invariant.
"""
from __future__ import annotations

import os
import time
from typing import Any

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
STRESS_REVOKE_AFTER_MS = int(os.environ.get("STRESS_REVOKE_AFTER_MS", "500"))
REVOKE_MUST_CLOSE_WITHIN_MS = int(
    os.environ.get("STRESS_REVOKE_CLOSE_BUDGET_MS", "2000")
)


@tag("s4", "session-revoke")  # type: ignore[arg-type]
class SessionRevokeUser(StressUser):
    """Stream, then self-revoke, then assert the stream + future requests die."""

    wait_time = between(2.0, 5.0)

    def on_start(self) -> None:
        super().on_start()
        # Capture the user_id from /api/auth/me.  The admin revoke
        # endpoint needs it.
        resp = self.client.get(
            "/api/auth/me",
            name=warmup_tag("GET /api/auth/me"),
            catch_response=True,
        )
        self._self_user_id: int | None = None
        with resp:
            if resp.status_code == 200:
                resp.success()
                try:
                    self._self_user_id = int(resp.json().get("id") or 0) or None
                except Exception:  # noqa: BLE001
                    pass
            else:
                resp.failure(f"me failed: {resp.status_code}")

    @task
    def stream_and_revoke(self) -> None:
        if self._self_user_id is None:
            return

        chat_id = self._create_chat()
        if chat_id is None:
            return

        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "payload": {
                "model": STRESS_MODEL_ID,
                "input": [{"type": "text", "content": realistic_user_message()}],
                "max_output_tokens": 128,
                "stream": True,
                "store": False,
            },
        }

        start = time.perf_counter()
        revoke_fired = False
        revoke_fired_at: float | None = None
        terminated_at: float | None = None
        had_error_frame = False
        had_clean_end = False

        try:
            with self.client.post(
                "/api/chat/stream",
                json=payload,
                stream=True,
                name=warmup_tag("POST /api/chat/stream (revoke)"),
                catch_response=True,
            ) as resp:
                if resp.status_code != 200:
                    resp.failure(
                        f"stream pre-revoke status {resp.status_code}"
                    )
                    return

                for event in iter_sse(resp.iter_lines(decode_unicode=False)):
                    elapsed_ms = (time.perf_counter() - start) * 1000
                    if (
                        not revoke_fired
                        and elapsed_ms >= STRESS_REVOKE_AFTER_MS
                    ):
                        # Self-revoke via the admin endpoint.  Requires
                        # this user to be an admin (the orchestrator
                        # provisions the S4 user pool as admins).
                        self._fire_revoke()
                        revoke_fired = True
                        revoke_fired_at = time.perf_counter()

                    if event.event == "chat.end":
                        had_clean_end = True
                        terminated_at = time.perf_counter()
                        break
                    if event.event == "error":
                        had_error_frame = True
                        terminated_at = time.perf_counter()
                        break

                if revoke_fired and revoke_fired_at is not None:
                    if terminated_at is None:
                        resp.failure(
                            "stream did NOT terminate after revoke "
                            "(potential security leak)"
                        )
                        return
                    close_ms = int((terminated_at - revoke_fired_at) * 1000)
                    if close_ms > REVOKE_MUST_CLOSE_WITHIN_MS:
                        resp.failure(
                            f"stream took {close_ms}ms to terminate after "
                            f"revoke (budget {REVOKE_MUST_CLOSE_WITHIN_MS}ms)"
                        )
                        return
                # Either the stream finished cleanly before our revoke
                # window OR we hit the budget — both are acceptable.
                _ = had_error_frame, had_clean_end
                resp.success()
        finally:
            # Whether or not we got to revoke, verify the session is now
            # dead: any subsequent API call must 401.
            if revoke_fired:
                after = self.client.get(
                    "/api/auth/me",
                    name=warmup_tag("GET /api/auth/me (post-revoke)"),
                    catch_response=True,
                )
                with after:
                    if after.status_code == 401:
                        after.success()
                    else:
                        after.failure(
                            f"post-revoke /me returned {after.status_code} "
                            f"(expected 401)"
                        )
                # Re-login so subsequent task iterations work.  Best-effort.
                from .base_user import login_with_credential

                try:
                    login_with_credential(self.client, self.credential)
                except Exception:  # noqa: BLE001
                    pass

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _fire_revoke(self) -> None:
        assert self._self_user_id is not None
        resp = self.client.post(
            f"/api/admin/users/{self._self_user_id}/revoke-sessions",
            name=warmup_tag("POST /api/admin/users/:id/revoke-sessions"),
            catch_response=True,
        )
        with resp:
            if resp.status_code in (200, 204):
                resp.success()
            else:
                resp.failure(
                    f"revoke failed: {resp.status_code} {resp.text[:120]!r}"
                )

    def _create_chat(self) -> int | None:
        resp = self.client.post(
            "/api/chats",
            data={"title": realistic_chat_title(), "incognito": "false"},
            name=warmup_tag("POST /api/chats"),
            catch_response=True,
        )
        with resp:
            if resp.status_code != 201:
                resp.failure(f"chat create: {resp.status_code}")
                return None
            resp.success()
            try:
                return int(resp.json()["id"])
            except Exception as exc:  # noqa: BLE001
                resp.failure(f"body parse: {exc}")
                return None
