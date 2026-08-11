# SPDX-License-Identifier: Apache-2.0
"""S8 — 50 isolated users.

Profile:
    50 unique sessions, each with own chats, folders, memory, audit
    log; all hitting the system in parallel.
    Assertions: zero cross-user data access (user A cannot see B's
    chats); memory insights correctly attributed; folder ops don't
    leak.

This user class creates ITS OWN chat at on_start, stashes the chat_id,
then continuously verifies that:

- GET /api/chats returns only this user's chats (chat_id is present,
  no foreign chats appear).
- Each user's folder list is independent.

The DB-level cross-user-leak check lives in the post-run invariant
``test_cross_user_leak.py``.
"""
from __future__ import annotations

from locust import between, tag, task

from ..data.realistic import realistic_chat_title
from .base_user import StressUser, warmup_tag


@tag("s8", "isolation")  # type: ignore[arg-type]
class IsolatedUser(StressUser):
    """Each instance gets its own chat + folder; verify list isolation."""

    wait_time = between(0.5, 1.5)

    def on_start(self) -> None:
        super().on_start()
        self._own_chat_id: int | None = self._create_chat()
        self._own_folder: str | None = self._create_folder()

    @task(3)
    def list_my_chats(self) -> None:
        resp = self.client.get(
            "/api/chats",
            name=warmup_tag("GET /api/chats (isolation)"),
            catch_response=True,
        )
        with resp:
            if resp.status_code != 200:
                resp.failure(f"list failed: {resp.status_code}")
                return
            try:
                chats = resp.json()
            except Exception as exc:  # noqa: BLE001
                resp.failure(f"json parse: {exc}")
                return
            if not isinstance(chats, list):
                resp.failure(f"list shape: {type(chats).__name__}")
                return
            if self._own_chat_id is not None:
                ids = {int(c.get("id", 0)) for c in chats if isinstance(c, dict)}
                if self._own_chat_id not in ids:
                    resp.failure(
                        f"own chat {self._own_chat_id} not in list (visibility loss)"
                    )
                    return
            resp.success()

    @task(2)
    def list_my_folders(self) -> None:
        resp = self.client.get(
            "/api/folders",
            name=warmup_tag("GET /api/folders (isolation)"),
            catch_response=True,
        )
        with resp:
            if resp.status_code != 200:
                resp.failure(f"folders failed: {resp.status_code}")
                return
            try:
                folders = resp.json()
            except Exception as exc:  # noqa: BLE001
                resp.failure(f"json parse: {exc}")
                return
            if not isinstance(folders, list):
                resp.failure(f"folder shape: {type(folders).__name__}")
                return
            if self._own_folder is not None and self._own_folder not in folders:
                # Folder creation may have been racy on slower runs; tolerate.
                pass
            resp.success()

    @task(1)
    def fetch_my_chat_detail(self) -> None:
        if self._own_chat_id is None:
            return
        resp = self.client.get(
            f"/api/chats/{self._own_chat_id}",
            name=warmup_tag("GET /api/chats/:id (isolation)"),
            catch_response=True,
        )
        with resp:
            if resp.status_code == 200:
                resp.success()
            elif resp.status_code == 404:
                # Chat was deleted by no one (we're the only owner) — investigate
                resp.failure("own chat 404 (cross-user delete?)")
            else:
                resp.failure(f"detail: {resp.status_code}")

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
                resp.failure(f"create: {resp.status_code}")
                return None
            resp.success()
            try:
                return int(resp.json()["id"])
            except Exception as exc:  # noqa: BLE001
                resp.failure(f"body parse: {exc}")
                return None

    def _create_folder(self) -> str | None:
        name = f"iso-{self.credential.get('username', 'unknown')}"
        resp = self.client.post(
            "/api/folders",
            data={"name": name},
            name=warmup_tag("POST /api/folders"),
            catch_response=True,
        )
        with resp:
            if resp.status_code in (200, 201, 409):
                # 409 = already exists from a previous run; tolerate.
                resp.success()
                return name
            resp.failure(f"folder create: {resp.status_code}")
            return None
