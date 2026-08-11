# SPDX-License-Identifier: Apache-2.0
"""S9 — Database integrity under contention.

Profile:
    100 concurrent writes (chats, messages, folders) + 100 concurrent
    reads.
    Assertions: no FK violations, no lost writes, audit log
    monotonically increasing.

This user class spams a mix of writes + reads.  The integrity-side
asserts (FK violations, monotonic audit log, lost-write detection)
live in the post-run invariants
(``test_fk_violations.py``, ``test_audit_log_monotonic.py``).
"""
from __future__ import annotations

import random

from locust import between, tag, task

from ..data.realistic import realistic_chat_title, realistic_user_message
from .base_user import StressUser, warmup_tag


@tag("s9", "db-contention")  # type: ignore[arg-type]
class DbContentionUser(StressUser):
    """Mixed read/write hammer for DB integrity invariants."""

    wait_time = between(0.05, 0.2)

    def on_start(self) -> None:
        super().on_start()
        self._chat_ids: list[int] = []

    @task(3)
    def create_chat(self) -> None:
        resp = self.client.post(
            "/api/chats",
            data={
                "title": realistic_chat_title(),
                "incognito": "false",
            },
            name=warmup_tag("POST /api/chats (contention)"),
            catch_response=True,
        )
        with resp:
            if resp.status_code != 201:
                resp.failure(f"create: {resp.status_code}")
                return
            resp.success()
            try:
                chat_id = int(resp.json()["id"])
                self._chat_ids.append(chat_id)
                if len(self._chat_ids) > 50:
                    self._chat_ids.pop(0)
            except Exception:  # noqa: BLE001
                pass

    @task(2)
    def post_message(self) -> None:
        if not self._chat_ids:
            return
        chat_id = random.choice(self._chat_ids)
        self.client.post(
            f"/api/chats/{chat_id}/messages",
            data={
                "role": "user",
                "content": realistic_user_message(),
            },
            name=warmup_tag("POST /api/chats/:id/messages (contention)"),
            catch_response=False,
        )

    @task(2)
    def list_chats(self) -> None:
        resp = self.client.get(
            "/api/chats",
            name=warmup_tag("GET /api/chats (contention)"),
            catch_response=True,
        )
        with resp:
            if resp.status_code == 200:
                resp.success()
            else:
                resp.failure(f"list: {resp.status_code}")

    @task(1)
    def delete_chat(self) -> None:
        if not self._chat_ids:
            return
        chat_id = self._chat_ids.pop()
        resp = self.client.delete(
            f"/api/chats/{chat_id}",
            name=warmup_tag("DELETE /api/chats/:id (contention)"),
            catch_response=True,
        )
        with resp:
            if resp.status_code in (204, 200, 404):
                resp.success()
            else:
                resp.failure(f"delete: {resp.status_code}")

    @task(2)
    def folder_op(self) -> None:
        name = f"fld-{random.randint(0, 100)}"
        resp = self.client.post(
            "/api/folders",
            data={"name": name},
            name=warmup_tag("POST /api/folders (contention)"),
            catch_response=True,
        )
        with resp:
            if resp.status_code in (200, 201, 409):
                resp.success()
            else:
                resp.failure(f"folder: {resp.status_code}")
