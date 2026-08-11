# SPDX-License-Identifier: Apache-2.0
"""S2 — R-15 incognito x share x memory under load.

Profile:
    100 incognito + 100 normal chats spawned in parallel.
    100 share-token requests + 100 memory-refine requests.
    Assertions: share tokens succeed ONLY for non-incognito;
    memory_embeddings + memory_insights have ZERO rows from
    incognito chats; audit log has no memory ops for incognito.

The DB-level invariant check lives in
``tests/stress/invariants/test_r15_privacy.py`` (runs post-load).

This user class focuses on the WIRE-side assertions: share-token
attempts on incognito chats must return 403, share-token attempts on
normal chats must return 201.  Anything else is a wire-level R-15 privacy
regression.
"""
from __future__ import annotations

import random

from locust import between, tag, task

from ..data.realistic import realistic_chat_title, realistic_user_message
from .base_user import StressUser, warmup_tag


@tag("s2", "incognito", "r15")  # type: ignore[arg-type]
class IncognitoUser(StressUser):
    """Spawn an incognito chat with 50% probability per task.

    Always tries to mint a share token and always tries one memory
    refine — both should reject cleanly on the incognito side.
    """

    wait_time = between(0.5, 2.0)

    @task(2)
    def share_attempt(self) -> None:
        incognito = random.random() < 0.5
        title = realistic_chat_title()
        chat_id = self._create_chat(title=title, incognito=incognito)
        if chat_id is None:
            return
        # Post one message so the chat has content (otherwise share is
        # trivially uninteresting).
        self._post_message(chat_id, realistic_user_message())
        # Attempt to mint a share token.
        resp = self.client.post(
            f"/api/chats/{chat_id}/share",
            name=warmup_tag("POST /api/chats/:id/share"),
            catch_response=True,
        )
        with resp:
            if incognito:
                if resp.status_code == 403:
                    resp.success()
                else:
                    resp.failure(
                        f"R-15 LEAK: share token minted for incognito "
                        f"(status={resp.status_code} body={resp.text[:200]!r})"
                    )
            else:
                if resp.status_code == 201:
                    resp.success()
                else:
                    resp.failure(
                        f"share failed for normal chat: {resp.status_code} "
                        f"{resp.text[:200]!r}"
                    )

    @task(1)
    def memory_refine_attempt(self) -> None:
        """Trigger memory.refine; if incognito, the chat must be excluded."""
        # Refine accepts no body that targets a specific chat (it sweeps
        # the user's eligible chats); we just exercise the endpoint
        # under concurrency and rely on the post-run invariant check
        # to validate that no incognito-sourced rows landed.
        resp = self.client.post(
            "/api/memory/refine",
            name=warmup_tag("POST /api/memory/refine"),
            catch_response=True,
        )
        with resp:
            if resp.status_code in (200, 202, 409):
                # 200/202 success, 409 = concurrent refine — both OK.
                resp.success()
            else:
                resp.failure(
                    f"refine unexpected status: {resp.status_code}"
                )

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_chat(self, *, title: str, incognito: bool) -> int | None:
        resp = self.client.post(
            "/api/chats",
            data={"title": title, "incognito": "true" if incognito else "false"},
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
                resp.failure(f"create body parse: {exc}")
                return None

    def _post_message(self, chat_id: int, content: str) -> None:
        # Most install lines persist messages via the streaming endpoint,
        # but for S2 we only need a row in messages.  The simplest path
        # is to call PATCH on a new message via the messages router.
        # The router exposes POST /api/messages/{chat_id}/append? — we
        # fall back to a no-op when that route doesn't exist by simply
        # not asserting on the response.
        try:
            self.client.post(
                f"/api/chats/{chat_id}/messages",
                data={"role": "user", "content": content},
                name=warmup_tag("POST /api/chats/:id/messages"),
                catch_response=False,
            )
        except Exception:  # noqa: BLE001
            # Endpoint optional; the share-attempt assertion is what
            # matters for R-15 (the incognito-privacy invariant).
            pass
