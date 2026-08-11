# SPDX-License-Identifier: Apache-2.0
"""Thin wrapper around the toxiproxy HTTP API.

Toxiproxy (https://github.com/Shopify/toxiproxy) is a TCP-layer fault
injector.  It listens on its admin port (default 8474) and accepts
proxy + toxic CRUD over HTTP.

We use it to sit between lm-chat and LM Studio:

    lm-chat ---> toxiproxy:18434 ---> LM Studio:1234

Set ``LM_STUDIO_BASE_URL=http://127.0.0.1:18434`` on the lm-chat
process; toxiproxy forwards to ``localhost:1234`` and applies any
toxics we've registered.

If ``toxiproxy-cli`` is not on PATH, ``available()`` returns False and
the orchestrator falls back to the httpx MockTransport backend.
Failures during proxy creation also fall back.
"""
from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any

import httpx

from .failure_modes import FailureKind, FailureSpec

log = logging.getLogger("stress.chaos.toxiproxy")

TOXIPROXY_ADMIN_DEFAULT = "http://127.0.0.1:8474"
PROXY_LISTEN_DEFAULT = "127.0.0.1:18434"
PROXY_UPSTREAM_DEFAULT = "127.0.0.1:1234"
PROXY_NAME = "lm_studio_chaos"


@dataclass
class ToxiproxyClient:
    """Toxiproxy admin-API client.

    Args:
        admin_url: Base URL of the toxiproxy admin API.
        proxy_name: The proxy registered for our test (default ``lm_studio_chaos``).
    """

    admin_url: str = TOXIPROXY_ADMIN_DEFAULT
    proxy_name: str = PROXY_NAME

    def __post_init__(self) -> None:
        self._http = httpx.Client(base_url=self.admin_url, timeout=5.0)

    # ------------------------------------------------------------------
    # Availability probe
    # ------------------------------------------------------------------

    @staticmethod
    def cli_available() -> bool:
        """Return True iff ``toxiproxy-cli`` is on PATH AND responds."""
        if not shutil.which("toxiproxy-cli"):
            return False
        try:
            res = subprocess.run(  # noqa: S603  - hardcoded executable
                ["toxiproxy-cli", "list"],
                capture_output=True,
                timeout=3,
                check=False,
            )
            return res.returncode == 0
        except (subprocess.TimeoutExpired, OSError):
            return False

    def server_reachable(self) -> bool:
        """Return True iff the admin HTTP endpoint responds 2xx."""
        try:
            resp = self._http.get("/version")
            return resp.status_code == 200
        except httpx.HTTPError:
            return False

    # ------------------------------------------------------------------
    # Lifecycle — create/destroy our proxy
    # ------------------------------------------------------------------

    def ensure_proxy(
        self,
        listen: str = PROXY_LISTEN_DEFAULT,
        upstream: str = PROXY_UPSTREAM_DEFAULT,
    ) -> None:
        """Create-or-replace the proxy we use for the LM Studio path."""
        # Delete any prior proxy with the same name (ignore 404).
        self._http.delete(f"/proxies/{self.proxy_name}")
        # Create fresh.
        resp = self._http.post(
            "/proxies",
            json={
                "name": self.proxy_name,
                "listen": listen,
                "upstream": upstream,
                "enabled": True,
            },
        )
        resp.raise_for_status()
        log.info(
            "toxiproxy: proxy %s listen=%s upstream=%s",
            self.proxy_name, listen, upstream,
        )

    def destroy_proxy(self) -> None:
        """Best-effort proxy removal at run end."""
        try:
            self._http.delete(f"/proxies/{self.proxy_name}")
        except httpx.HTTPError as exc:  # pragma: no cover - cleanup
            log.warning("toxiproxy destroy failed: %s", exc)

    # ------------------------------------------------------------------
    # Toxic application (one at a time — we clear before applying next)
    # ------------------------------------------------------------------

    def apply(self, spec: FailureSpec) -> None:
        """Replace any active toxics with the one described by ``spec``.

        FailureKind.NONE clears all toxics (recover phase).
        """
        # Clear existing toxics first — toxiproxy enumerates them under
        # /proxies/<name>/toxics; delete by name.
        existing = self._http.get(f"/proxies/{self.proxy_name}/toxics")
        if existing.status_code == 200:
            for toxic in existing.json():
                tname = toxic.get("name")
                if tname:
                    self._http.delete(
                        f"/proxies/{self.proxy_name}/toxics/{tname}"
                    )

        if spec.kind == FailureKind.NONE:
            return

        payload: dict[str, Any]
        if spec.kind == FailureKind.LATENCY:
            payload = {
                "name": "latency",
                "type": "latency",
                "stream": "downstream",
                "toxicity": 1.0,
                "attributes": {"latency": spec.latency_ms, "jitter": 0},
            }
        elif spec.kind == FailureKind.BANDWIDTH:
            payload = {
                "name": "bandwidth",
                "type": "bandwidth",
                "stream": "downstream",
                "toxicity": 1.0,
                "attributes": {"rate": spec.bandwidth_kbps},
            }
        elif spec.kind == FailureKind.DOWN:
            # toxiproxy "timeout" with 0 = close immediately; we ALSO
            # disable the proxy so new connections refuse outright.
            self._http.post(
                f"/proxies/{self.proxy_name}",
                json={"enabled": False},
            )
            return
        elif spec.kind == FailureKind.SERVER_ERROR:
            # toxiproxy can't actually emit application-layer 502s; the
            # closest equivalent is "reset_peer" + the client SEES a
            # connection reset which httpx maps to a transport error.
            payload = {
                "name": "reset_peer",
                "type": "reset_peer",
                "stream": "downstream",
                "toxicity": 1.0,
                "attributes": {"timeout": 0},
            }
        elif spec.kind == FailureKind.MID_STREAM_DISCONNECT:
            payload = {
                "name": "limit_data",
                "type": "limit_data",
                "stream": "downstream",
                "toxicity": 1.0,
                "attributes": {"bytes": spec.body_bytes_before_drop},
            }
        else:  # pragma: no cover - defensive
            raise ValueError(f"unsupported FailureKind: {spec.kind}")

        # Re-enable proxy if it was disabled previously.
        self._http.post(f"/proxies/{self.proxy_name}", json={"enabled": True})
        resp = self._http.post(
            f"/proxies/{self.proxy_name}/toxics",
            json=payload,
        )
        resp.raise_for_status()
        log.info("toxiproxy applied %s: %s", spec.kind.value, payload)

    def close(self) -> None:
        """Release the admin HTTP client."""
        self._http.close()
