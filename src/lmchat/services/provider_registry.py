# SPDX-License-Identifier: Apache-2.0
"""ProviderRegistry — live name→ChatProvider map.

Workstream A4 of the multi-provider / MCP foundation plan
(docs/audit/2026-06-18-multiprovider-mcp-foundation-plan.md).

Holds:
- the singleton LM Studio adapter (name "lmstudio") which is built by the
  lifespan and injected at construction time.
- one OpenAICompatProvider per ENABLED row in provider_configs (built from
  the ProviderConfigService).

``refresh()`` rebuilds the cloud-provider slice from the DB; call it after
any admin add/update/delete so the live dispatch path sees the change
immediately without a restart.

This registry does NOT touch StreamingService, models_service, lmstudio_adapter,
or any LM Studio behavior.  It is purely an additive catalog layer (A4) that the
streaming dispatch layer (a later workstream) will consult.
"""
from __future__ import annotations

import asyncio

import httpx

from lmchat.logging import get_logger
from lmchat.providers.openai_compat import OpenAICompatProvider
from lmchat.services.provider_config_service import ProviderConfigService

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ProviderRegistry:
    """Live name→ChatProvider map.

    Built during the FastAPI lifespan (after ``lm_streaming_client``);
    stored at ``app.state.provider_registry``.

    Args:
        lmstudio_provider: The singleton LM Studio adapter (already built
            by the lifespan; injected so the registry does not own its
            lifecycle).
        config_service:    ``ProviderConfigService`` for reading the
            ``provider_configs`` DB table.
        http_client:       Shared ``httpx.AsyncClient`` injected by the
            lifespan; used for all OpenAICompatProvider instances (they
            do NOT own the client lifecycle).
    """

    _LMSTUDIO_NAME: str = "lmstudio"

    def __init__(
        self,
        *,
        lmstudio_provider: object,
        config_service: ProviderConfigService,
        http_client: httpx.AsyncClient,
    ) -> None:
        self._lmstudio = lmstudio_provider
        self._config_service = config_service
        self._http_client = http_client
        # Serialise concurrent refresh() calls so interleaved DB reads cannot
        # produce a torn write to self._providers (admin-burst fix).
        self._refresh_lock = asyncio.Lock()
        # name → provider; seeded with the LM Studio adapter.
        self._providers: dict[str, object] = {
            self._LMSTUDIO_NAME: lmstudio_provider,
        }

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, name: str) -> object | None:
        """Return the provider for *name*, or ``None`` if not registered.

        Args:
            name: Provider slug (e.g. ``"lmstudio"``, ``"openai"``).

        Returns:
            The provider instance or ``None``.
        """
        return self._providers.get(name)

    def names(self) -> list[str]:
        """Return all registered provider names in sorted order.

        Returns:
            Sorted list of provider name strings.
        """
        return sorted(self._providers.keys())

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    async def refresh(self) -> None:
        """Rebuild the cloud-provider slice from the ``provider_configs`` DB.

        Fetches all ENABLED provider configs, creates an
        :class:`~lmchat.providers.openai_compat.OpenAICompatProvider`
        for each, and replaces the cloud-provider entries in the internal
        map.  The LM Studio entry (``"lmstudio"``) is never touched.

        Errors from individual provider construction are logged at WARNING
        and skipped; the registry continues with the remaining providers.

        Concurrent calls are serialised via ``_refresh_lock`` so that
        interleaved DB reads cannot produce a torn write to
        ``self._providers`` under admin PUT/DELETE bursts.

        Call after any admin add/update/delete on provider_configs.
        """
        async with self._refresh_lock:
            configs = await self._config_service.list_all()

            # Rebuild cloud providers; keep the lmstudio anchor.
            new_providers: dict[str, object] = {
                self._LMSTUDIO_NAME: self._lmstudio,
            }

            for cfg in configs:
                if not cfg.enabled:
                    log.debug(
                        "provider_registry.refresh.skipping_disabled",
                        provider=cfg.provider,
                    )
                    continue

                # Fetch internal view to get the decrypted api_key.
                try:
                    internal = await self._config_service.get(cfg.provider)
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "provider_registry.refresh.get_failed",
                        provider=cfg.provider,
                        error=str(exc),
                    )
                    continue

                if internal is None:
                    # Row vanished between list_all and get — race; skip.
                    log.warning(
                        "provider_registry.refresh.row_vanished",
                        provider=cfg.provider,
                    )
                    continue

                try:
                    provider = OpenAICompatProvider(
                        name=internal.provider,
                        base_url=internal.base_url,
                        api_key=internal.api_key,
                        http_client=self._http_client,
                        extra_headers=internal.extra_headers or {},
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "provider_registry.refresh.build_failed",
                        provider=cfg.provider,
                        error=str(exc),
                    )
                    continue

                new_providers[cfg.provider] = provider
                log.info(
                    "provider_registry.refresh.registered",
                    provider=cfg.provider,
                    base_url=cfg.base_url,
                )

            # Atomic dict swap — the lock ensures only one rebuild completes
            # at a time; readers outside the lock see either the old or the
            # new dict, never a partially-built intermediate.
            self._providers = new_providers
            log.info(
                "provider_registry.refresh.done",
                providers=list(new_providers.keys()),
            )

    async def reconfigure_http_client(self, http_client: httpx.AsyncClient) -> None:
        """Rebind the shared HTTP client on the registry and all cloud providers.

        Called inside ``rewire_singletons`` (the sixth singleton re-point) when
        an admin saves new LM Studio settings and ``rewire_singletons`` replaces
        ``app.state.http_client``.  Without this call, cloud providers built by
        :meth:`refresh` continue to hold a reference to the old
        (eventually-closed) client, causing ``RuntimeError: Cannot send a
        request, as the client has been closed.`` on the next
        ``/api/providers/status`` probe.

        Mirrors the pattern used for ``web_search_service.reconfigure()``
        (mirrors ``web_search_service.reconfigure()`` pattern).

        This method acquires ``_refresh_lock`` before mutating
        ``self._http_client`` and iterating ``self._providers``.  This
        serialises it with :meth:`refresh`, which also holds ``_refresh_lock``
        while reading ``self._http_client`` and replacing ``self._providers``.

        Lock-ordering guarantee: ``rewire_lock → _refresh_lock``.  The caller
        (``rewire_singletons``) holds ``rewire_lock``; this method then acquires
        ``_refresh_lock`` inside it.  :meth:`refresh` holds only
        ``_refresh_lock`` and never touches ``rewire_lock``, so the order is
        never reversed and no deadlock is possible.

        Whichever of ``reconfigure_http_client`` and ``refresh`` runs last
        leaves the registry in a fully consistent state:
        - If ``refresh`` runs first, it builds providers from the old client;
          ``reconfigure_http_client`` then updates ``self._http_client`` AND
          rebinds those providers to the new client.
        - If ``reconfigure_http_client`` runs first, it updates
          ``self._http_client``; ``refresh`` then reads the already-updated
          client and builds new providers against it directly.

        The LM Studio anchor (``_LMSTUDIO_NAME``) is skipped: its adapter is
        rebound directly by ``rewire_singletons`` via a shared object reference.
        Only :class:`~lmchat.providers.openai_compat.OpenAICompatProvider`
        cloud instances need to be updated here.

        Args:
            http_client: The new shared :class:`httpx.AsyncClient` created by
                ``rewire_singletons``.
        """
        async with self._refresh_lock:
            self._http_client = http_client
            for name, provider in self._providers.items():
                if name == self._LMSTUDIO_NAME:
                    # LM Studio adapter is rebound directly by rewire_singletons.
                    continue
                if isinstance(provider, OpenAICompatProvider):
                    provider.set_http_client(http_client)
                    log.debug(
                        "provider_registry.reconfigure_http_client.rebound",
                        provider=name,
                    )
