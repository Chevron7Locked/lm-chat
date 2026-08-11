# SPDX-License-Identifier: Apache-2.0
"""FastAPI application factory + lifespan for lm-chat.

Lifespan ordering (startup, innermost → outermost execution order)
------------------------------------------------------------------
1.  ``configure_logging`` — must run before any log is emitted.
2.  ``get_settings()`` — validate all required env vars; ``os._exit(78)``
    (EX_CONFIG) if any field fails validation.  ``os._exit`` is used rather
    than ``sys.exit`` because ``SystemExit`` raised inside an async lifespan
    gets wrapped by anyio into a ``BaseExceptionGroup``, which causes Python
    to exit with code 1.  ``os._exit`` calls the C-level ``_exit()`` directly
    so the observable exit code is exactly 78.
3.  ``await ensure_schema_ready(engine)`` — run Alembic migrations if
    needed; validate fingerprint.  Must complete before routes can serve
    requests (the auth endpoints write to DB immediately).
4.  Build the params + models services (ParamsService, ModelsService,
    LmstudioAdapter) and attach to app.state. ModelsService.refresh() is
    triggered non-blocking via ``asyncio.create_task``; startup does NOT
    block on it.
5.  Build the embedding + memory services (EmbeddingClient, MemoryService, QualityModeService)
    and ReindexStatusHolder; attach to app.state.
6.  Build the chat services (ChatService, MessageService) and chat_locks dict;
    probe ``pg_trgm`` presence once; attach all to app.state.
7.  Build the streaming services (StreamingService, stream_buckets InMemoryBucketStore).
    Start the draft-reaper background task (asyncio.create_task).
8.  ``asyncio.create_task(_periodic_session_cleanup)`` — background task
    that calls ``session_store.cleanup()`` every 5 minutes.  Cancelled
    cleanly on shutdown.

Middleware ordering — ``add_middleware`` is inner-first; the LAST call is
the OUTERMOST wrapper on the request path:
    PrometheusMiddleware      (inner — records request count inside request_id scope)
    LoginRateLimitMiddleware  (applied to POST /api/auth/login only)
    RequestContextMiddleware  (outer — binds request_id before everything else)

The ``/healthz`` endpoint lives in ``_meta.py`` alongside ``/readyz``.

Periodic session cleanup
------------------------
``_periodic_session_cleanup(store)`` loops forever, calling
``store.cleanup()`` every ``_CLEANUP_INTERVAL_SECONDS`` (300 s = 5 min).
The ``create_task`` result is stored on ``app.state.cleanup_task`` so the
shutdown hook can cancel it.  On cancellation the ``asyncio.CancelledError``
propagates out of the sleep, the loop exits, and the task ends cleanly.
No external monitoring is needed — the structured log emits a WARNING with
the deleted-row count after each cleanup run (via ``session_store.cleanup``).

Shutdown ordering (reverse of startup)
--------------------------------------
1. Cancel reaper_task; await.
2. Cancel cleanup_task; await.
3. Cancel models_refresh_task (if still running); await.
4. Cancel reindex_task (if still running); await.
5. Close http_client (aclose).
6. dispose_engine().

``chat_locks`` and ``stream_buckets`` are process-local objects — no teardown
needed; GC handles them.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Final
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError
from sqlalchemy import text

from lmchat import __version__
from lmchat.config import Settings, get_settings
from lmchat.db.engine import async_dispose_engine, get_engine
from lmchat.db.startup import ensure_schema_ready
from lmchat.embedding.client import EmbeddingClient
from lmchat.logging import configure_logging, get_logger
from lmchat.mcp.host import McpHost
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.middleware.auth import AuthMiddleware
from lmchat.middleware.cors import CorsMiddleware
from lmchat.middleware.logging import RequestLoggingMiddleware
from lmchat.middleware.metrics import PrometheusMiddleware
from lmchat.middleware.quota import QuotaMiddleware
from lmchat.middleware.rate_limit import LoginRateLimitMiddleware
from lmchat.middleware.request_context import RequestContextMiddleware
from lmchat.middleware.security import SecurityMiddleware
from lmchat.routes._meta import router as meta_router
from lmchat.routes.ab_compare import router as ab_compare_router
from lmchat.routes.admin import router as admin_router
from lmchat.routes.analytics import router as analytics_router
from lmchat.routes.app_settings_routes import router as app_settings_router
from lmchat.routes.auth import router as auth_router
from lmchat.routes.chats import router as chats_router
from lmchat.routes.documents import router as documents_router
from lmchat.routes.folders import router as folders_router
from lmchat.routes.integrations import router as integrations_router
from lmchat.routes.lm_studio_settings import router as lm_studio_settings_router
from lmchat.routes.lmstudio_health import router as lmstudio_health_router
from lmchat.routes.mcp_store import router as mcp_store_router
from lmchat.routes.memory import ReindexStatusHolder
from lmchat.routes.memory import router as memory_router
from lmchat.routes.messages import router as messages_router
from lmchat.routes.models import router as models_router
from lmchat.routes.params import router as params_router
from lmchat.routes.preset_models_settings import router as preset_models_settings_router
from lmchat.routes.projects import router as projects_router
from lmchat.routes.prompt_library import router as prompt_library_router
from lmchat.routes.providers import router as providers_router
from lmchat.routes.quotas import router as quotas_router
from lmchat.routes.search import router as search_router
from lmchat.routes.share import router as share_router
from lmchat.routes.spa import router as spa_router
from lmchat.routes.spa import serve_spa_for_request
from lmchat.routes.streaming import router as streaming_router
from lmchat.routes.web_search import router as web_search_router
from lmchat.services._stream_reaper import run_reaper
from lmchat.services.ab_compare_service import AbCompareService
from lmchat.services.analytics_service import AnalyticsService
from lmchat.services.app_settings_service import (
    resolve_searxng_url as _resolve_searxng_url,
)
from lmchat.services.app_settings_service import (
    resolve_web_search_provider as _resolve_web_search_provider,
)
from lmchat.services.background_tasks import run_daily_purge, run_incognito_ttl_purge
from lmchat.services.chat_service import ChatService
from lmchat.services.folder_service import FolderService
from lmchat.services.integrations_service import IntegrationsService
from lmchat.services.lm_studio_overrides_service import LmStudioOverridesService
from lmchat.services.lmstudio_adapter import CHAT_LIMITS, CHAT_TIMEOUT, LmstudioAdapter
from lmchat.services.lmstudio_streaming_client import LmstudioStreamingClient
from lmchat.services.mcp_server_store import McpServerStore
from lmchat.services.memory_service import MemoryService
from lmchat.services.message_service import MessageService
from lmchat.services.models_service import ModelsService
from lmchat.services.params_service import ParamsService
from lmchat.services.preset_models_service import PresetModelsService
from lmchat.services.projects_service import ProjectsService
from lmchat.services.prompt_library_service import PromptLibraryService
from lmchat.services.provider_config_service import ProviderConfigService
from lmchat.services.provider_registry import ProviderRegistry
from lmchat.services.quality_modes import QualityModeService
from lmchat.services.streaming_service import StreamingService
from lmchat.services.web_search_service import WebSearchService
from lmchat.session.sqlite_store import SQLiteSessionStore
from lmchat.utils.task_lifetime import spawn_background_task

log = get_logger(__name__)

# How often the background cleanup task runs (seconds).
_CLEANUP_INTERVAL_SECONDS: Final[int] = 300  # 5 minutes


# ---------------------------------------------------------------------------
# Background cleanup coroutine
# ---------------------------------------------------------------------------


async def _periodic_session_cleanup(store: SQLiteSessionStore) -> None:
    """Call ``store.cleanup()`` every :data:`_CLEANUP_INTERVAL_SECONDS`.

    Loops indefinitely until cancelled.  Structured-log events are emitted
    by :meth:`~lmchat.session.sqlite_store.SQLiteSessionStore.cleanup`
    (WARNING level, with ``deleted_count``).

    Args:
        store: The session store whose expired rows should be purged.
    """
    while True:
        try:
            await asyncio.sleep(_CLEANUP_INTERVAL_SECONDS)
            await store.cleanup()
        except asyncio.CancelledError:
            # Shutdown in progress — exit the loop cleanly.
            log.info("session_cleanup_task cancelled — shutting down")
            return
        except Exception as exc:  # noqa: BLE001
            # Non-fatal: log and keep going.  A transient DB error should
            # not kill the cleanup loop permanently.
            log.warning("session_cleanup error (non-fatal)", error=str(exc))


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """FastAPI lifespan: startup validation, schema, cleanup task, teardown."""
    # 1. Configure logging first — everything else emits logs.
    configure_logging(level="INFO")

    # 2. Validate settings — exit(78) on misconfiguration.
    try:
        settings: Settings = get_settings()
    except ValidationError as exc:
        # Log each missing/invalid field on its own line so admins
        # know exactly which env var to set.
        log.error("startup config invalid — required env vars missing or invalid")
        for err in exc.errors():
            loc = ".".join(str(segment) for segment in err.get("loc", ()))
            log.error(
                "required env var missing or invalid",
                field=loc,
                message=err.get("msg", ""),
            )
        # os._exit(78) bypasses Python's normal exception machinery.
        # sys.exit(78) inside an async lifespan raises SystemExit which
        # anyio wraps in a BaseExceptionGroup, causing the process to exit
        # with code 1 rather than 78.  os._exit() calls the C-level _exit()
        # directly, guaranteeing EX_CONFIG is the observable exit code.
        os._exit(78)  # EX_CONFIG (sysexits.h)

    # Re-configure with the admin's chosen log level now that settings
    # loaded successfully.
    configure_logging(level=settings.lm_chat_log_level)

    # 3. Schema readiness — must complete before routes are reachable.
    engine = get_engine()
    app.state.engine = engine
    await ensure_schema_ready(engine)

    # 3a. Resolve admin-tier LM Studio config BEFORE constructing the http
    # client.  Env values are reference-only
    # and never auto-applied; the http client uses the SAVED admin default
    # (empty if none) so a fresh deploy boots with no upstream probe and no
    # cache leak to per-user model lists.
    from lmchat.services.lm_studio_overrides_service import (
        LmStudioOverridesService as _LmStudioOverridesService_boot,
    )

    _boot_overrides_svc = _LmStudioOverridesService_boot(engine=engine, settings=settings)
    # Prune api_key envelopes that can't be decrypted with the current
    # LM_CHAT_SECRET (dev restarts with a fresh secret would otherwise
    # leave the saved blob unusable, resolve() would silently report
    # api_key_set=false, and LM Studio would return 401 to every
    # request — symptoms the admin sees as "LM Studio not connected"
    # with no actionable signal). Cleared keys force a re-save in
    # Settings → LM Studio; URL/model survive.
    _pruned = await _boot_overrides_svc.prune_unusable_api_keys()
    if _pruned:
        log.warning(
            "lifespan.lm_studio_api_keys_pruned",
            count=_pruned,
            hint="LM_CHAT_SECRET rotation detected — re-enter the API key in Settings.",
        )
    # Surface the prune event so the FE can show a banner directing the
    # admin to re-save in Settings → LM Studio. Without this, the only
    # signal is a BE log line + silent 401s on every LM Studio probe —
    # symptoms the admin sees as "LM Studio not connected" with no
    # actionable hint. Cleared once the admin re-saves via the existing
    # rewire_singletons path.
    app.state.lm_studio_key_pruned = bool(_pruned)
    # Initialize auth-failed flag; updated by periodic refresh.
    app.state.lm_studio_auth_failed = False
    _boot_admin = await _boot_overrides_svc.resolve_admin_tier_only()
    _boot_base_url = _boot_admin.base_url  # "" when no admin default saved
    _boot_api_key = _boot_admin.api_key

    # Bootstrap admin_default from env on first boot.
    # When no DB tier has saved an api_key AND the env provides one,
    # probe-gate the env key against LM Studio before persisting.
    if not _boot_api_key and settings.lm_studio_api_key:
        try:
            # Build candidate — use env base_url only if no tier has one.
            _candidate_base_url = (
                settings.lm_studio_base_url if not _boot_base_url else _boot_base_url
            )
            _candidate_api_key = settings.lm_studio_api_key
            # Validate base_url: non-empty, http(s) — never a filesystem path.
            _parsed = urlparse(_candidate_base_url)
            if not _candidate_base_url:
                log.warning(
                    "lifespan.env_bootstrap_skipped_empty_url",
                    hint="LM_STUDIO_BASE_URL is empty — cannot bootstrap.",
                )
            elif _parsed.scheme not in ("http", "https"):
                log.warning(
                    "lifespan.env_bootstrap_skipped_invalid_scheme",
                    scheme=_parsed.scheme,
                    hint="Only http:// and https:// are valid LM Studio targets.",
                )
            elif not _candidate_api_key:
                log.warning(
                    "lifespan.env_bootstrap_skipped_empty_key",
                    hint="LM_STUDIO_API_KEY is empty — nothing to bootstrap.",
                )
            else:
                # Probe LM Studio with the candidate BEFORE persisting.
                _probe_result = await _boot_overrides_svc.probe(
                    base_url=_candidate_base_url,
                    api_key=_candidate_api_key,
                )
                if _probe_result.ok:
                    _seeded = await _boot_overrides_svc.seed_admin_default_from_env(
                        base_url=_candidate_base_url,
                        api_key=_candidate_api_key,
                    )
                    if _seeded:
                        log.info(
                            "lifespan.env_bootstrap_seeded",
                            base_url=_candidate_base_url,
                            hint=(
                                "Admin default seeded from env vars after"
                                " successful LM Studio probe."
                            ),
                        )
                        # Re-resolve so the http_client uses the seeded values.
                        _boot_admin = await _boot_overrides_svc.resolve_admin_tier_only()
                        _boot_base_url = _boot_admin.base_url
                        _boot_api_key = _boot_admin.api_key
                else:
                    # Probe failed (401, connection error, etc.) — do NOT seed.
                    # Set app.state flag so the FE shows an auth-failed banner.
                    app.state.lm_studio_auth_failed = True
                    log.warning(
                        "lifespan.env_bootstrap_probe_failed",
                        base_url=_candidate_base_url,
                        error=_probe_result.error,
                        hint=(
                            "LM Studio env-provided key probed but the upstream "
                            "returned non-200. Admin default NOT seeded. "
                            "The admin must re-enter the API key in Settings."
                        ),
                    )
        except Exception as _boot_exc:  # noqa: BLE001
            log.warning(
                "lifespan.env_bootstrap_error",
                error=str(_boot_exc),
                hint="Bootstrap skipped due to unexpected error.",
            )

    # 4. Upstream HTTP client for LM Studio.
    # A single shared AsyncClient is used by all upstream-facing services (models probe)
    # and the adapter (streaming).  Auth headers are set here; the client
    # uses base_url-relative paths for safety.
    # When base_url is empty (no admin default), httpx still constructs a
    # valid client; subsequent probes hit absolute-URL paths that resolve
    # to nothing and fail-fast.  Cache stays empty until admin saves a
    # default (which triggers rewire_singletons + refresh).
    http_client = httpx.AsyncClient(
        base_url=_boot_base_url,
        headers={"Authorization": f"Bearer {_boot_api_key}"} if _boot_api_key else {},
        timeout=CHAT_TIMEOUT,
        limits=CHAT_LIMITS,
    )
    # Also expose as app.state.http for backwards compatibility with any
    # code that references app.state.http.
    app.state.http = http_client
    app.state.http_client = http_client

    # 5. Params + models services — build AFTER schema is ready.
    params_service = ParamsService()
    app.state.params_service = params_service

    models_service = ModelsService(
        http_client=http_client,
        base_url=_boot_base_url,
        long_op_timeout_seconds=settings.lm_chat_lmstudio_long_op_timeout_seconds,
        # Probe-completion hook clears a model's rejected params
        # when a refresh shows it newly (re)loaded.
        params_service=params_service,
        # Short TTL so resolve_to_loaded_or_fallback re-probes quickly when a
        # model is unloaded externally (user action in LM Studio UI or idle-TTL
        # eviction). Default 5 s via Settings.lm_chat_loaded_models_ttl_seconds.
        loaded_models_ttl=float(settings.lm_chat_loaded_models_ttl_seconds),
    )
    app.state.models_service = models_service

    # Wire the forced-reprobe auth-cleared callback so
    # that a successful forced reprobe clears app.state.lm_studio_auth_failed,
    # which the FE banner endpoint reads.
    def _on_forced_reprobe_auth_cleared() -> None:
        app.state.lm_studio_auth_failed = False

    models_service.set_auth_cleared_callback(_on_forced_reprobe_auth_cleared)

    lmstudio_adapter = LmstudioAdapter(
        http_client=http_client,
        base_url=_boot_base_url,
        params_service=params_service,
    )
    app.state.lmstudio_adapter = lmstudio_adapter

    # Kick off the periodic model-cache refresh loop. Initial warm-up is
    # non-blocking — failures must NOT block startup — and then the loop
    # re-probes every `models_cache_refresh_interval_seconds` (default 30 min)
    # so loaded/unloaded models surface in the dropdown without the user
    # hitting Refresh manually. Each iteration logs at WARN on failure and
    # keeps going; only CancelledError exits the loop.
    refresh_interval = settings.lm_chat_models_cache_refresh_interval_seconds

    async def _periodic_models_refresh() -> None:
        while True:
            try:
                await models_service.refresh()
                # Mirror auth-failed flag on app.state so
                # the FE banner endpoint can read it without importing ModelsService.
                app.state.lm_studio_auth_failed = models_service.auth_failed
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "lifespan.models_refresh_failed",
                    error=str(exc),
                    note="Model cache stale; will retry on next interval",
                )
            try:
                await asyncio.sleep(refresh_interval)
            except asyncio.CancelledError:
                raise

    models_refresh_task = asyncio.create_task(
        _periodic_models_refresh(), name="models_cache_periodic_refresh"
    )
    app.state.models_refresh_task = models_refresh_task

    # 5. Embedding + memory services — EmbeddingClient, MemoryService, QualityModeService.
    # EmbeddingClient is stateless on the shared http_client; constructed
    # after the params/models services since it shares the same http_client.
    embedding_client = EmbeddingClient(
        http_client=http_client,
        base_url=_boot_base_url,
        # Resolve every embed call's model_id to its LOADED INSTANCE wire id
        # (e.g. ``…-v1.5@q8_0``) just before the upstream POST. Centralizes the
        # fix for "Invalid model identifier" 400s across ALL embed call sites
        # (retrieval query re-embed, memory recall, document/memory indexing).
        wire_id_resolver=models_service.resolve_embedding_wire_id,
    )
    app.state.embedding_client = embedding_client

    memory_service = MemoryService(
        engine=engine,
        embedding_client=embedding_client,
        models_service=models_service,
    )
    app.state.memory_service = memory_service

    quality_mode_service = QualityModeService(
        adapter=lmstudio_adapter,
        embedding_client=embedding_client,
        models_service=models_service,
        engine=engine,
    )
    app.state.quality_mode_service = quality_mode_service

    # ReindexStatusHolder — plain Python object; no I/O at construction.
    reindex_status_holder = ReindexStatusHolder()
    app.state.reindex_status_holder = reindex_status_holder
    # Initialize reindex_task sentinel to None so shutdown logic is safe
    # even when no reindex was ever kicked.
    app.state.reindex_task = None

    log.info("lifespan.p3_services_ready")

    # 6. Chat services — ChatService, MessageService, per-chat lock dict.
    # chat_locks is a plain dict; per-chat asyncio.Lock objects are created
    # lazily by chat_service.compact() / chat_service.delete() via setdefault.
    # The same dict is shared with the streaming service to serialize
    # compaction against an active stream on the same chat.
    chat_locks: dict[int, asyncio.Lock] = {}
    app.state.chat_locks = chat_locks

    chat_service = ChatService(
        engine=engine,
        memory_service=memory_service,
        models_service=models_service,
        chat_locks=chat_locks,
        aux_model_timeout_sec=settings.lm_chat_aux_model_timeout_sec,
    )
    app.state.chat_service = chat_service

    message_service = MessageService(
        engine=engine,
        memory_service=memory_service,
    )
    app.state.message_service = message_service

    # Probe pg_trgm presence once at startup.
    # Cached on app.state so per-request search reads it without re-querying.
    # On a non-Postgres engine, has_pg_trgm is always False.
    has_pg_trgm: bool = False
    if engine.dialect.name == "postgresql":
        try:
            async with engine.connect() as conn:
                has_pg_trgm = bool(
                    await conn.scalar(text("SELECT 1 FROM pg_extension WHERE extname = 'pg_trgm'"))
                )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "lifespan.pg_trgm_probe_failed",
                error=str(exc),
                note="Falling back to ILIKE search on Postgres",
            )
    app.state.has_pg_trgm = has_pg_trgm

    log.info(
        "lifespan.p4_services_ready",
        has_pg_trgm=has_pg_trgm,
    )

    # 7. Streaming services — StreamingService, stream_buckets, draft-reaper task.
    # stream_buckets is a per-user InMemoryBucketStore for the stream rate
    # limit (POST /api/chat/stream).  It is an InMemoryBucketStore directly
    # on app.state (not a dict of locks — the BucketStore manages its own
    # internal dict per key).
    stream_buckets = InMemoryBucketStore()
    app.state.stream_buckets = stream_buckets

    lm_streaming_client = LmstudioStreamingClient(adapter=lmstudio_adapter)
    app.state.lm_streaming_client = lm_streaming_client

    # ProviderConfigService + ProviderRegistry.
    # Built after lm_streaming_client so the registry can reference the same
    # shared http_client.  The registry holds the lmstudio_adapter as the
    # "lmstudio" entry; cloud providers are loaded from provider_configs rows.
    provider_config_service = ProviderConfigService(engine=engine)
    app.state.provider_config_service = provider_config_service

    mcp_server_store = McpServerStore(engine=engine)
    app.state.mcp_server_store = mcp_server_store

    provider_registry = ProviderRegistry(
        lmstudio_provider=lmstudio_adapter,
        config_service=provider_config_service,
        http_client=http_client,
    )
    # Populate the registry with any already-saved cloud providers.
    await provider_registry.refresh()
    app.state.provider_registry = provider_registry

    log.info("lifespan.a4_provider_registry_ready")

    # Model catalog merge service.  Wraps models_service + registry to
    # serve a unified list[ModelInfo] (LM Studio + cloud) for GET /api/models.
    # Built immediately after the registry so any code that reads
    # app.state.model_catalog can rely on both dependencies being live.
    from lmchat.services.model_catalog import ModelCatalogService  # noqa: PLC0415

    model_catalog = ModelCatalogService(
        models_svc=models_service,
        registry=provider_registry,
        config_svc=provider_config_service,
    )
    app.state.model_catalog = model_catalog
    log.info("lifespan.w1_model_catalog_ready")

    # ProjectsService needs to exist before
    # StreamingService so the stream-time project_prompt injection
    # has a service to call. Constructed here (instead of later
    # alongside FolderService) for the dependency ordering; the
    # app.state.projects_service rebind below is idempotent and
    # the second binding at line ~530 is a no-op overwrite of the
    # same instance.
    projects_service = ProjectsService(engine=engine)
    app.state.projects_service = projects_service

    streaming_service = StreamingService(
        engine=engine,
        lm_client=lm_streaming_client,
        memory_service=memory_service,
        chat_locks=chat_locks,
        idle_timeout_sec=settings.lm_chat_stream_idle_timeout_sec,
        aux_model_timeout_sec=settings.lm_chat_aux_model_timeout_sec,
        # Pass embedding_client + models_service so the RAG hook can
        # call rag_service.augment_prompt when chat.settings.rag_enabled.
        embedding_client=embedding_client,
        models_service=models_service,
        # Stream-time project_prompt injection.
        projects_service=projects_service,
        # Provider registry for replay-mode context wiring.
        # provider_registry is built earlier in this lifespan (before
        # StreamingService), so the ordering is already correct.
        provider_registry=provider_registry,
        # Quality modes (self-consistency / chain-of-verification).
        # quality_mode_service is built earlier in this lifespan (before
        # StreamingService), so the ordering is already correct. Dispatch is
        # gated per-chat by chats.settings.{self_consistency,chain_of_verification}_enabled
        # and applies only on the LM Studio chain path.
        quality_mode_service=quality_mode_service,
    )
    app.state.streaming_service = streaming_service

    # Start the draft-reaper background task.
    reaper_task = asyncio.create_task(
        run_reaper(
            engine=engine,
            interval_sec=60,
            finalization_timeout_sec=settings.lm_chat_reaper_finalization_timeout_sec,
            draft_max_age_hours=settings.lm_chat_reaper_draft_max_age_hours,
        ),
        name="stream_reaper",
    )
    app.state.reaper_task = reaper_task

    log.info("lifespan.p5_services_ready")

    # admin_buckets — per-admin-user rate-limit store for admin routes.
    # Pattern mirrors stream_buckets: single InMemoryBucketStore instance;
    # the admin_rate_limit dependency reads it via app.state.admin_buckets.
    admin_buckets = InMemoryBucketStore()
    app.state.admin_buckets = admin_buckets

    log.info("lifespan.p6_admin_buckets_ready")

    # WebSearchService — SearXNG-first with DDG fallback.
    # Uses the shared http_client for SearXNG probes and queries.
    # Resolve provider/url from admin overrides first (fall back to
    # config defaults when no override is set).
    _ws_provider = await _resolve_web_search_provider(engine)
    _ws_url = await _resolve_searxng_url(engine)
    web_search_service = WebSearchService(
        provider=_ws_provider,
        searxng_url=_ws_url,
        http_client=http_client,
        brave_api_key=settings.lm_chat_brave_api_key,
    )
    app.state.web_search_service = web_search_service
    log.info(
        "lifespan.web_search_url",
        provider=_ws_provider,
        searxng_url=_ws_url,
    )
    # Probe SearXNG at startup; logs ERROR on failure but does not block startup.
    # spawn_background_task holds a strong ref so the probe can't be GC'd
    # mid-flight (bare create_task() is only weakly referenced by the loop).
    spawn_background_task(web_search_service.probe(), name="searxng_probe")

    # AbCompareService — thin orchestrator for two concurrent streams.
    # engine passed for post-stream token quota consumption.
    ab_compare_service = AbCompareService(lm_client=lm_streaming_client, engine=engine)
    app.state.ab_compare_service = ab_compare_service

    # AnalyticsService — read-only aggregates from audit_log + messages.
    analytics_service = AnalyticsService(engine=engine)
    app.state.analytics_service = analytics_service

    # PromptLibraryService — user-managed prompt presets.
    prompt_library_service = PromptLibraryService(engine=engine)
    app.state.prompt_library_service = prompt_library_service

    log.info("lifespan.p8c_services_ready")

    # Daily soft-deleted document purge background task.
    # Runs once per day; logs deleted row count.
    daily_purge_task = asyncio.create_task(
        run_daily_purge(engine),
        name="daily_document_purge",
    )
    app.state.daily_purge_task = daily_purge_task

    log.info("lifespan.p8d_purge_task_started")

    # Periodic incognito-chat TTL sweep.
    # Default 5 min cadence; override via LM_CHAT_INCOGNITO_PURGE_INTERVAL_SECONDS.
    incognito_purge_task = asyncio.create_task(
        run_incognito_ttl_purge(
            engine,
            interval_sec=settings.lm_chat_incognito_purge_interval_seconds,
        ),
        name="incognito_ttl_purge",
    )
    app.state.incognito_purge_task = incognito_purge_task

    log.info(
        "lifespan.p13i_incognito_purge_task_started",
        interval_sec=settings.lm_chat_incognito_purge_interval_seconds,
    )

    # IntegrationsService — MCP integrations list.
    # LM Studio does not expose MCP server enumeration over HTTP; this service
    # provides the workaround (env var OR DB table; DB wins when populated).
    # local_mcp_config=None disables file discovery (split-host or test);
    # otherwise the service uses its default Path.home()/".lmstudio"/"mcp.json".
    _integrations_kwargs: dict[str, Any] = {}
    if not settings.lm_chat_local_mcp_discovery_enabled:
        _integrations_kwargs["local_mcp_config"] = None
    integrations_service = IntegrationsService(
        engine=engine,
        env_default=settings.lm_chat_available_integrations,
        synthetic_enabled_by_default=(settings.lm_chat_default_integrations_enabled_by_default),
        **_integrations_kwargs,
    )
    app.state.integrations_service = integrations_service
    log.info(
        "lifespan.p12e_integrations_service_ready",
        env_default_count=len(settings.lm_chat_available_integrations),
    )

    # Native MCP host — Store-only execution. McpHost NEVER reads
    # ~/.lmstudio/mcp.json; its configured servers come SOLELY from
    # (a) the rehydration immediately below (mcp_server_store.
    # list_host_configs()) and (b) the runtime /api/mcp-store install
    # route (routes/mcp_store.py sets mcp_host._configs[slug] = ...).
    # mcp.json is a SEPARATE, LM-Studio-native concern: LM Studio runs
    # those servers itself, host-side, for its own native-mode tool
    # loop (untouched by LMChat); LMChat only reads that file to DISPLAY
    # LM Studio's servers in the native composer picker
    # (IntegrationsService, below — gated by
    # lm_chat_local_mcp_discovery_enabled). If McpHost ingested mcp.json
    # here, the container would try to RUN LM Studio's host-configured
    # servers (e.g. filesystem pointed at host home dirs, or a host-only
    # .mjs bridge on 127.0.0.1) — connect failures, since those hosts/
    # paths don't exist in the container. Always construct config_path=None.
    # Servers are NOT auto-connected at startup — connect lazily so a
    # missing npx or unavailable server never blocks boot (connect is
    # non-fatal).
    mcp_host = McpHost(
        config_path=None,
        call_timeout_sec=settings.lm_chat_mcp_tool_call_timeout_sec,
    )
    app.state.mcp_host = mcp_host
    log.info(
        "lifespan.b1_mcp_host_ready",
        configured_servers=len(mcp_host.configured_server_ids),
    )

    # Rehydrate store-installed servers into the host's config registry so
    # they survive restart and are connectable on demand (LAZY — no auto-connect).
    # Guard against an empty store (e.g. fresh install).
    # Wrapped in try/except: rehydration must NEVER block startup — a corrupt DB
    # row or unexpected error is logged and boot continues without MCP configs.
    try:
        _b4_credential_errors: dict[str, str] = {}
        _b4_host_configs = await mcp_server_store.list_host_configs(
            credential_errors=_b4_credential_errors
        )
        for _b4_cfg in _b4_host_configs:
            if _b4_cfg.server_id not in mcp_host._configs:
                from lmchat.mcp.host import McpServerConfig as _McpServerConfig  # noqa: PLC0415

                mcp_host._configs[_b4_cfg.server_id] = _McpServerConfig(
                    server_id=_b4_cfg.server_id,
                    transport=_b4_cfg.transport,
                    command=_b4_cfg.command,
                    args=_b4_cfg.args,
                    env=_b4_cfg.env,
                    url=_b4_cfg.url,
                    headers=_b4_cfg.headers,
                )
        if _b4_host_configs:
            log.info(
                "lifespan.b4_mcp_store_rehydrated",
                count=len(_b4_host_configs),
            )
        # Servers whose stored secret failed to decrypt are deliberately NOT
        # in _b4_host_configs (list_host_configs excludes them) — mark them
        # errored on the host so GET /api/mcp-store/servers surfaces
        # last_error instead of the server silently coming back keyless.
        for _b4_slug, _b4_cred_err in _b4_credential_errors.items():
            mcp_host.record_credential_error(_b4_slug, _b4_cred_err)
            log.error(
                "lifespan.b4_mcp_store_credential_error",
                slug=_b4_slug,
                error=_b4_cred_err,
            )
    except Exception as _b4_exc:  # noqa: BLE001
        log.error(
            "lifespan.b4_rehydrate_failed",
            error=str(_b4_exc),
        )

    # FolderService — CRUD on the admin's folder catalogue.
    # Combines user_prefs (user-named buckets) with chats.folder
    # (in-use folder values) to power the sidebar folder list.
    folder_service = FolderService(engine=engine)
    app.state.folder_service = folder_service
    log.info("lifespan.p13l_folder_service_ready")

    # PresetModelsService — per-preset model/provider defaults.
    # Reads/writes user_prefs.preset_models (migration 0031).
    # provider_registry is available at this point (built above).
    preset_models_svc = PresetModelsService(
        engine=engine,
        provider_registry=provider_registry,
    )
    app.state.preset_models_service = preset_models_svc
    log.info("lifespan.w5_preset_models_service_ready")

    # ProjectsService is now constructed EARLIER
    # in this lifespan (above the StreamingService block) because
    # the stream-time project_prompt injection depends on it.
    # The CRUD routes at /api/projects/* still resolve via the same
    # ``app.state.projects_service`` attribute.
    log.info("lifespan.projects_v1_service_ready")

    # LmStudioOverridesService — per-user + admin-default LM Studio
    # connection-parameter overrides + "Test connection" probe.
    # No background tasks; reads/writes the user_lm_studio_overrides and
    # server_lm_studio_default tables added in migration 0012.
    lm_studio_overrides_service = LmStudioOverridesService(
        engine=engine,
        settings=settings,
    )
    app.state.lm_studio_overrides_service = lm_studio_overrides_service
    log.info("lifespan.p13g_lm_studio_overrides_service_ready")

    # rewire_lock serializes concurrent admin saves.
    # Must be set before any route handler that calls rewire_singletons.
    # Lock ordering invariant: rewire_lock → _cache_lock (never reversed).
    app.state.rewire_lock = asyncio.Lock()

    # Boot rewire now superseded by the pre-construction resolve above — the
    # http_client + services already point at the saved admin default (or
    # empty when none).  No rewire needed unless a future code path resaves
    # mid-lifespan.

    # Start periodic session cleanup.
    session_store = SQLiteSessionStore(engine=engine)
    cleanup_task = asyncio.create_task(
        _periodic_session_cleanup(session_store),
        name="session_cleanup",
    )
    app.state.cleanup_task = cleanup_task
    app.state.session_store = session_store

    log.info("lifespan startup complete", version=__version__)

    try:
        yield
    finally:
        # Shutdown — reverse order: tasks first, then I/O, then engine.

        # Cancel the reaper task first (streaming background task).
        reaper_task = getattr(app.state, "reaper_task", None)
        if reaper_task is not None and not reaper_task.done():
            log.info("lifespan.cancelling_reaper_task")
            reaper_task.cancel()
            try:
                await reaper_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                # Swallow all non-CancelledError exceptions from the reaper
                # task during shutdown — the process is exiting and the
                # exception was already logged by the reaper itself.
                log.warning("lifespan.reaper_task_shutdown_error", error=str(exc))

        cleanup_task.cancel()
        try:
            await cleanup_task
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            # Swallow all non-CancelledError exceptions during shutdown —
            # the process is exiting — but log so a real regression on the
            # shutdown path doesn't go unnoticed (mirrors the reaper task).
            log.warning("lifespan.cleanup_task_shutdown_error", error=str(exc))

        # Cancel the daily purge task.
        daily_purge_task_state = getattr(app.state, "daily_purge_task", None)
        if daily_purge_task_state is not None and not daily_purge_task_state.done():
            daily_purge_task_state.cancel()
            try:
                await daily_purge_task_state
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                log.warning("lifespan.daily_purge_task_shutdown_error", error=str(exc))

        # Cancel the incognito TTL purge task.
        incognito_purge_task_state = getattr(app.state, "incognito_purge_task", None)
        if incognito_purge_task_state is not None and not incognito_purge_task_state.done():
            incognito_purge_task_state.cancel()
            try:
                await incognito_purge_task_state
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                log.warning("lifespan.incognito_purge_task_shutdown_error", error=str(exc))

        # Cancel the warm-up task if it's still running.
        if not models_refresh_task.done():
            models_refresh_task.cancel()
            try:
                await models_refresh_task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                log.warning("lifespan.models_refresh_task_shutdown_error", error=str(exc))

        # Cancel the reindex task if it's still running.
        reindex_task_state = getattr(app.state, "reindex_task", None)
        if reindex_task_state is not None and not reindex_task_state.done():
            log.info("lifespan.cancelling_reindex_task")
            reindex_task_state.cancel()
            try:
                await reindex_task_state
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001
                log.warning("lifespan.reindex_task_shutdown_error", error=str(exc))

        # Shut down all MCP server connections cleanly.
        mcp_host_state = getattr(app.state, "mcp_host", None)
        if mcp_host_state is not None:
            try:
                await mcp_host_state.shutdown()
            except Exception as exc:  # noqa: BLE001
                log.warning("lifespan.mcp_host_shutdown_error", error=str(exc))

        # Each of the two remaining teardown steps gets its own try/except
        # so a failure in one does not skip the other — a bare sequential
        # await chain here would mean an aclose() exception skips engine
        # disposal entirely, leaking an engine (and its connection pool)
        # per failed teardown.
        try:
            await http_client.aclose()
        except Exception as exc:  # noqa: BLE001
            log.warning("lifespan.http_client_close_shutdown_error", error=str(exc))

        # Use async_dispose_engine() from inside the
        # async lifespan coroutine to avoid MissingGreenlet errors.
        # dispose_engine() (sync) is still used by test fixtures.
        try:
            await async_dispose_engine()
        except Exception as exc:  # noqa: BLE001
            log.warning("lifespan.engine_dispose_shutdown_error", error=str(exc))

        log.info("lifespan shutdown complete")


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------


def _maybe_initialise_otel(app: FastAPI) -> None:
    """Opt-in OpenTelemetry initialisation for the stress-test harness.

    Triggered ONLY when ``LM_CHAT_OTEL_ENABLED=true`` is set in the
    environment.  The harness lives under ``tests/stress/`` so the
    import is wrapped in a try/except; when the dev deps are absent
    (e.g. on a production runtime install) the hook silently no-ops.

    Behaviour when the env var is unset OR the imports fail: ZERO
    side effect on the request path — no behavioural change when the
    env var is unset.
    """
    if os.environ.get("LM_CHAT_OTEL_ENABLED", "").lower() != "true":
        return
    try:
        # Local import keeps the prod boot path free of test-only deps.
        from tests.stress.tracing.otel_setup import (  # noqa: PLC0415
            initialise as _otel_initialise,
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("otel hook import failed (no-op)", error=str(exc))
        return
    try:
        if _otel_initialise(app):
            log.info("otel hook active")
    except Exception as exc:  # noqa: BLE001
        log.warning("otel hook init failed (no-op)", error=str(exc))


def create_app() -> FastAPI:
    """Build and return the FastAPI application.

    Middleware are added inner-first (the LAST ``add_middleware`` call is the
    OUTERMOST wrapper on the request path):

    1. ``PrometheusMiddleware`` — records request count + latency.  Added
       first so it is inside ``LoginRateLimitMiddleware`` and
       ``RequestContextMiddleware`` on the response path; the request_id is
       already bound when Prometheus fires its post-response hook.
    2. ``LoginRateLimitMiddleware`` — pure-ASGI rate-limiter for
       ``POST /api/auth/login``.  Added after Prometheus so Prometheus
       counts rate-limited 429 responses correctly (they do flow through
       Prometheus even though they never reach the route handler).
    3. ``RequestContextMiddleware`` — binds ``request_id`` before every
       inner layer.  Must be OUTERMOST.

    Returns:
        The configured :class:`~fastapi.FastAPI` application instance.
    """
    app = FastAPI(
        title="lm-chat",
        version=__version__,
        lifespan=lifespan,
        # /docs (Swagger UI) and /redoc are disabled: they would otherwise
        # register real routes that win over the SPA's 404-fallback route
        # (see _spa_404_handler below), shadowing the SPA at those paths.
        # Swagger's inline bootstrap script + CDN assets are also blocked
        # outright by the strict CSP (script-src 'self' 'nonce-…';
        # style-src 'self' — see middleware/security.py), so self-hosting
        # those assets would not make it functional either. `openapi_url`
        # is left at its default (`/openapi.json`) since that's a harmless
        # schema endpoint with no competing SPA route.
        docs_url=None,
        redoc_url=None,
    )

    # --------------------------------------------------------------------------
    # Middleware — added INNERMOST FIRST (LIFO); LAST call = OUTERMOST on request
    #
    # Intended stack (outer → inner on the request path):
    #   CORS → RequestContext → Auth → Security → RequestLogging → Prometheus →
    #   LoginRateLimit → (FastAPI routes)
    #
    # RequestContext must be OUTSIDE RequestLogging (logging reads request_id
    # contextvar bound by RequestContext).
    # Auth must be OUTSIDE RequestLogging (logging reads user_id set by Auth).
    # CORS is outermost (rejects pre-flight before any auth work).
    # --------------------------------------------------------------------------

    # Innermost: per-path login rate-limit (POST /api/auth/login only).
    app.add_middleware(LoginRateLimitMiddleware)
    # Inside RequestContext: Prometheus (records count + latency; request_id bound).
    app.add_middleware(PrometheusMiddleware)
    # Structured request logging — reads request_id + user_id from scope.
    app.add_middleware(RequestLoggingMiddleware)
    # Security headers + CSP nonce (HSTS, X-Frame-Options, etc.).
    app.add_middleware(SecurityMiddleware)
    # Session-cookie auth — resolves cookie, attaches request.state.user.
    # Pass fastapi_app=app so the middleware can read app.state.session_store
    # set by the lifespan (and by test fixtures).  Without this, the middleware
    # would fall back to get_engine() which may point to a different DB than
    # the session store used by route-level dependencies.
    # Per-user daily-request quota — registered BEFORE AuthMiddleware so
    # that it is INNER (runs after Auth on the request path).  add_middleware
    # is inner-first: the registration order here is innermost → outermost.
    # QuotaMiddleware reads request.state.user which is set by AuthMiddleware,
    # so QuotaMiddleware must be inner (closer to the routes) relative to Auth.
    app.add_middleware(QuotaMiddleware)
    # Session-cookie auth — resolves cookie, attaches request.state.user.
    # Pass fastapi_app=app so the middleware can read app.state.session_store
    # set by the lifespan (and by test fixtures).  Without this, the middleware
    # would fall back to get_engine() which may point to a different DB than
    # the session store used by route-level dependencies.
    app.add_middleware(AuthMiddleware, fastapi_app=app)
    # Bind request_id contextvar BEFORE logging (and before Auth).
    app.add_middleware(RequestContextMiddleware)
    # CORS — outermost so pre-flight rejections happen before any work.
    app.add_middleware(CorsMiddleware)

    # --------------------------------------------------------------------------
    # Routers — baseline, then feature-area additions.
    # --------------------------------------------------------------------------

    app.include_router(meta_router)  # /healthz, /readyz, /api/metrics
    app.include_router(auth_router)
    # Model + params routes
    app.include_router(models_router)
    app.include_router(params_router)
    # Memory routes
    app.include_router(memory_router)
    # Chat routes
    app.include_router(chats_router)
    app.include_router(messages_router)
    app.include_router(search_router)
    # Streaming routes
    app.include_router(streaming_router)
    # Admin routes
    app.include_router(admin_router)
    # Document routes (RAG pipeline)
    app.include_router(documents_router)
    # Web search + A/B compare routes
    app.include_router(web_search_router)
    app.include_router(ab_compare_router)
    # Analytics + prompt library routes
    app.include_router(analytics_router)
    app.include_router(prompt_library_router)
    # Per-user quota routes
    app.include_router(quotas_router)
    # MCP integrations list routes
    app.include_router(integrations_router)
    # Folder catalogue CRUD.
    app.include_router(folders_router)
    # Project CRUD routes (/api/projects/*).
    app.include_router(projects_router)
    # LM Studio config-override routes.
    app.include_router(lm_studio_settings_router)
    # Live reachability health probe for the topbar status badge.
    app.include_router(lmstudio_health_router)
    # App-level admin settings-override routes
    app.include_router(app_settings_router)
    # Per-preset model/provider default routes
    app.include_router(preset_models_settings_router)
    # Cloud-provider admin config routes
    app.include_router(providers_router)
    app.include_router(mcp_store_router)
    # Public read-only share-view routes (no auth required)
    app.include_router(share_router)

    # SPA shell: explicit routes + 404-fallback strategy.
    #
    # Strategy: register spa_router for GET / + explicit static files
    # (favicon.svg, manifest.webmanifest, sw.js).  For HTML5-history deep
    # links (/chats/123, /settings, etc.) we use a 404 exception handler
    # that returns the SPA shell for browser requests (Accept: text/html).
    # This avoids a /{path:path} catch-all that would shadow dynamically-
    # registered routes (e.g. test fixtures that inject routes after create_app).
    _web_dist = _resolve_web_dist()
    if _web_dist is not None:
        _assets_dir = _web_dist / "assets"
        if _assets_dir.exists():
            app.mount(
                "/assets",
                StaticFiles(directory=str(_assets_dir)),
                name="spa-assets",
            )
        _icons_dir = _web_dist / "icons"
        if _icons_dir.exists():
            app.mount(
                "/icons",
                StaticFiles(directory=str(_icons_dir)),
                name="spa-icons",
            )
        _fonts_dir = _web_dist / "fonts"
        if _fonts_dir.exists():
            app.mount(
                "/fonts",
                StaticFiles(directory=str(_fonts_dir)),
                name="spa-fonts",
            )

    # spa_router handles: GET /, GET /favicon.svg, GET /manifest.webmanifest,
    # GET /sw.js.  Deep-link paths are handled by the 404 handler below.
    app.include_router(spa_router)

    # 404 fallback for HTML5-history deep links.
    # ``serve_spa_for_request`` checks Accept header: browser navigations
    # get the SPA shell (200); JSON-only API calls get 404 JSON unchanged.
    from fastapi.exception_handlers import http_exception_handler
    from starlette.exceptions import HTTPException as StarletteHTTPException

    async def _spa_404_handler(request: Request, exc: Exception) -> Response:
        # Only intercept 404s for the SPA fallback; let all other HTTP
        # exceptions propagate through FastAPI's default handler.
        if isinstance(exc, StarletteHTTPException) and exc.status_code != 404:
            return await http_exception_handler(request, exc)
        return serve_spa_for_request(request)

    app.add_exception_handler(
        StarletteHTTPException,
        _spa_404_handler,  # type: ignore[arg-type]
    )

    # /healthz is now defined in routes/_meta.py.

    # Stress-harness opt-in OpenTelemetry initialisation.  No-op
    # when LM_CHAT_OTEL_ENABLED != "true" so the production path is
    # behaviour-identical to the pre-instrumentation baseline.
    _maybe_initialise_otel(app)

    _install_auth_response_docs(app)

    return app


def _install_auth_response_docs(app: FastAPI) -> None:
    """Document the 401/403 responses the auth dependencies raise.

    ``require_user`` raises 401 on an unauthenticated request; ``require_admin``
    raises 403 for a non-admin (401 if anonymous). FastAPI cannot infer these
    from a dependency that raises ``HTTPException``, so the generated OpenAPI
    omits them — and unauthenticated DAST fuzzing (L5) then reports an
    *undocumented* 401/403 on every protected operation. Rather than annotate
    ~44 routes by hand, install a custom ``openapi()`` that walks each route's
    dependency tree for the auth guards and injects the matching responses.
    Accurate (only auth-protected operations get them) and DRY.
    """
    from fastapi.openapi.utils import get_openapi
    from fastapi.routing import APIRoute

    from lmchat.routes._dependencies import require_admin, require_user

    def _calls_in_dependant(dependant: object) -> set[object]:
        calls: set[object] = set()
        stack = [dependant]
        while stack:
            dep = stack.pop()
            call = getattr(dep, "call", None)
            if call is not None:
                calls.add(call)
            stack.extend(getattr(dep, "dependencies", []))
        return calls

    _401 = {"description": "Authentication required."}
    _403 = {"description": "Admin privileges required."}

    def custom_openapi() -> dict[str, object]:
        if app.openapi_schema:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            openapi_version=app.openapi_version,
            description=app.description,
            routes=app.routes,
        )
        paths: dict = schema.get("paths", {})
        for route in app.routes:
            if not isinstance(route, APIRoute):
                continue
            calls = _calls_in_dependant(route.dependant)
            needs_403 = require_admin in calls
            needs_401 = needs_403 or require_user in calls
            if not needs_401:
                continue
            path_item = paths.get(route.path_format)
            if not path_item:
                continue
            for method in route.methods or []:
                op = path_item.get(method.lower())
                if not isinstance(op, dict):
                    continue
                responses = op.setdefault("responses", {})
                responses.setdefault("401", _401)
                if needs_403:
                    responses.setdefault("403", _403)
        app.openapi_schema = schema
        return schema

    app.openapi = custom_openapi  # type: ignore[method-assign]


def _resolve_web_dist() -> Path | None:
    """Return the ``web/dist`` path relative to the installed package root.

    Searches up from the routes/ dir to find the repo root, then appends
    ``web/dist``.  Returns ``None`` if the directory does not exist (e.g. in
    a production install where the frontend is served by a CDN).
    """
    # __file__ = src/lmchat/app.py → .parent = src/lmchat → .parent = src
    # → .parent = repo root (lm-chat-v1)
    candidate = Path(__file__).parent.parent.parent / "web" / "dist"
    if candidate.exists():
        return candidate
    return None


app = create_app()
