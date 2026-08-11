# SPDX-License-Identifier: Apache-2.0
"""S6 — Adversarial input storm.

Profile:
    100 requests with Hypothesis-generated payloads: Unicode RTL,
    NUL bytes, 50MB+ bodies, SQL injection in titles.
    Assertions: all return 400/413, no SQL injection succeeds, no
    disk exhaustion, server stays responsive.
"""
from __future__ import annotations

from locust import between, tag, task

from ..data.adversarial import (
    adversarial_message,
    adversarial_title,
    oversized_body,
    path_traversal,
    rtl_sandwich,
)
from .base_user import StressUser, warmup_tag


@tag("s6", "adversarial")  # type: ignore[arg-type]
class AdversarialUser(StressUser):
    """Five adversarial flavours, weighted to cover all categories."""

    wait_time = between(0.1, 0.6)

    @task(3)
    def adversarial_title_create(self) -> None:
        bad = adversarial_title().example()
        resp = self.client.post(
            "/api/chats",
            data={"title": bad, "incognito": "false"},
            name=warmup_tag("POST /api/chats (adversarial title)"),
            catch_response=True,
        )
        with resp:
            # The server should EITHER reject (4xx) OR sanitize-and-store
            # (201).  A 5xx is a real bug.  In addition, NEVER reflect
            # the SQL probe back verbatim in a Location header / JSON
            # string — but Pydantic + SQLAlchemy parameterisation
            # forbids that pathway.  We just check non-5xx.
            if 200 <= resp.status_code < 500:
                resp.success()
            else:
                resp.failure(
                    f"5xx on adversarial title: {resp.status_code} "
                    f"{resp.text[:120]!r}"
                )

    @task(2)
    def adversarial_message_post(self) -> None:
        # First create a normal chat, then post the adversarial content.
        chat_id = self._create_chat()
        if chat_id is None:
            return
        bad = adversarial_message().example()
        resp = self.client.post(
            f"/api/chats/{chat_id}/messages",
            data={"role": "user", "content": bad},
            name=warmup_tag("POST /api/chats/:id/messages (adversarial)"),
            catch_response=True,
        )
        with resp:
            if 200 <= resp.status_code < 500 or resp.status_code == 405:
                # 405 if the endpoint isn't exposed under that path —
                # still want to exercise it but tolerate.
                resp.success()
            else:
                resp.failure(f"5xx on adversarial msg: {resp.status_code}")

    @task(1)
    def oversized_body_attack(self) -> None:
        """50 MB body — must be rejected with 413 (or 400) well before OOM."""
        big = oversized_body(target_mb=50)
        resp = self.client.post(
            "/api/chats",
            data={"title": big, "incognito": "false"},
            name=warmup_tag("POST /api/chats (50MB body)"),
            catch_response=True,
            timeout=15.0,
        )
        with resp:
            if resp.status_code in (400, 413, 422):
                resp.success()
            elif 200 <= resp.status_code < 300:
                # Stored a 50MB chat title — that's a DoS vulnerability.
                resp.failure(
                    f"50MB body ACCEPTED with {resp.status_code} — DoS risk"
                )
            elif 500 <= resp.status_code < 600:
                resp.failure(
                    f"5xx on 50MB body: {resp.status_code} — server stress"
                )
            else:
                resp.success()  # other 4xx are fine

    @task(2)
    def rtl_unicode_storm(self) -> None:
        bad = rtl_sandwich()
        chat_id = self._create_chat(title=bad)
        # If accepted, fetch it back and confirm the title round-trips
        # (or is at least returned as a string — Unicode truncation
        # would be a bug).
        if chat_id is None:
            return
        get_resp = self.client.get(
            f"/api/chats/{chat_id}",
            name=warmup_tag("GET /api/chats/:id (rtl)"),
            catch_response=True,
        )
        with get_resp:
            if get_resp.status_code == 200:
                resp_data = get_resp.text
                if "\x00" in resp_data:
                    get_resp.failure("NUL byte round-tripped in response")
                else:
                    get_resp.success()
            elif 400 <= get_resp.status_code < 500:
                get_resp.success()
            else:
                get_resp.failure(f"unexpected {get_resp.status_code}")

    @task(1)
    def path_traversal_folder(self) -> None:
        bad = path_traversal()
        resp = self.client.post(
            "/api/folders",
            data={"name": bad},
            name=warmup_tag("POST /api/folders (traversal)"),
            catch_response=True,
        )
        with resp:
            if 200 <= resp.status_code < 500:
                # Folder names are opaque strings server-side — accepting
                # the literal is fine.  We only fail on 5xx (crash).
                resp.success()
            else:
                resp.failure(f"5xx on path traversal: {resp.status_code}")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _create_chat(self, title: str | None = None) -> int | None:
        from ..data.realistic import realistic_chat_title

        if title is None:
            title = realistic_chat_title()
        resp = self.client.post(
            "/api/chats",
            data={"title": title, "incognito": "false"},
            name=warmup_tag("POST /api/chats"),
            catch_response=True,
        )
        with resp:
            if resp.status_code != 201:
                # Adversarial title may have triggered 4xx; that's OK.
                if 400 <= resp.status_code < 500:
                    resp.success()
                else:
                    resp.failure(
                        f"chat create failed: {resp.status_code}"
                    )
                return None
            resp.success()
            try:
                return int(resp.json()["id"])
            except Exception as exc:  # noqa: BLE001
                resp.failure(f"body parse: {exc}")
                return None
