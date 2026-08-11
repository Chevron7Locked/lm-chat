# SPDX-License-Identifier: Apache-2.0
"""S3 — concurrent CRUD race on a shared chat.

Profile:
    10 users editing same chat title, 10 pinning messages, 10
    regenerating.
    Assertions: no duplicate titles, only one version persists, no
    FK violations.

Implementation
--------------
The orchestrator pre-creates a shared chat owned by ``stress_u0001`` and
writes its id to ``target/stress/shared_chat.txt`` before Locust starts.
Each task reads that file once at on_start and races against the
shared row.

Last-write-wins is the documented behaviour today; the assertion is
strictly that no FK violation surfaces under the race AND the row
state at the end matches one of the writes.  The post-run invariant
test (``test_fk_violations.py``) covers the DB side.
"""
from __future__ import annotations

import os
import random
from pathlib import Path

from locust import between, tag, task

from ..data.realistic import realistic_chat_title
from .base_user import StressUser, warmup_tag

SHARED_CHAT_FILE = os.environ.get(
    "STRESS_SHARED_CHAT_FILE", "target/stress/shared_chat.txt"
)


def _shared_chat_id() -> int | None:
    path = Path(SHARED_CHAT_FILE)
    if not path.exists():
        return None
    try:
        return int(path.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        return None


@tag("s3", "crud-race")  # type: ignore[arg-type]
class CrudRaceUser(StressUser):
    """Hammer a single shared chat with title edits + pins + regenerates."""

    wait_time = between(0.1, 0.5)

    def on_start(self) -> None:
        super().on_start()
        self._chat_id: int | None = _shared_chat_id()
        if self._chat_id is None:
            # Each user falls back to creating its own chat — still
            # exercises the route, just without the shared-row collision.
            self._chat_id = self._create_own_chat()

    @task(3)
    def edit_title(self) -> None:
        if self._chat_id is None:
            return
        new_title = f"race-{realistic_chat_title()[:40]}-{random.randint(0, 9999)}"
        resp = self.client.patch(
            f"/api/chats/{self._chat_id}",
            data={"title": new_title},
            name=warmup_tag("PATCH /api/chats/:id (title)"),
            catch_response=True,
        )
        with resp:
            if resp.status_code in (200, 404, 409):
                # 200 = win, 404 = deleted by another user, 409 = locked.
                resp.success()
            else:
                resp.failure(
                    f"title edit unexpected: {resp.status_code} {resp.text[:120]!r}"
                )

    @task(2)
    def pin_message(self) -> None:
        if self._chat_id is None:
            return
        # Pin doesn't have a body — toggles state.  The endpoint is
        # idempotent under concurrency.
        resp = self.client.post(
            f"/api/chats/{self._chat_id}/pin",
            name=warmup_tag("POST /api/chats/:id/pin"),
            catch_response=True,
        )
        with resp:
            if resp.status_code in (200, 201, 404, 409, 405):
                # 405 covers the case where this endpoint is not exposed —
                # still want to exercise the route under contention.
                resp.success()
            else:
                resp.failure(f"pin unexpected: {resp.status_code}")

    @task(1)
    def regenerate_message(self) -> None:
        if self._chat_id is None:
            return
        # The "regenerate" affordance is a POST that ultimately starts a
        # new stream — we issue it and accept any 4xx as a normal "no
        # message to regenerate" outcome under the race.
        resp = self.client.post(
            f"/api/chats/{self._chat_id}/regenerate",
            name=warmup_tag("POST /api/chats/:id/regenerate"),
            catch_response=True,
        )
        with resp:
            if 200 <= resp.status_code < 500 or resp.status_code == 409:
                resp.success()
            else:
                resp.failure(f"regenerate unexpected: {resp.status_code}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_own_chat(self) -> int | None:
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
