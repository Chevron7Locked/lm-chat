# SPDX-License-Identifier: Apache-2.0
"""Shared FastAPI dependencies for lm-chat routes.

All route modules import their authentication + engine dependencies
from here — no module constructs these inline.

Dependency tree
---------------
``get_engine_dep`` → ``app.state.engine`` set by the lifespan (falls back to
    the process-global ``db.engine.get_engine()`` for tests that bypass the
    lifespan).
``get_session_store_dep`` → the ``SessionStore`` instance attached to
    ``app.state.session_store`` by the lifespan. There is NO fallback path:
    the lifespan-owned store is the single canonical instance and the same
    one the periodic cleanup task uses. Tests that bypass the lifespan
    MUST inject a store via ``app.dependency_overrides`` (see
    ``tests/routes/conftest.py``); calling this dependency without a
    lifespan-attached store raises ``RuntimeError`` rather than silently
    creating a second instance.
``get_current_user_dep`` → bridges ``auth_service.get_current_user`` into a
    FastAPI ``Depends``-compatible callable that takes a ``Request``.
``require_user`` → wraps ``get_current_user_dep``; raises HTTP 401 if the
    user is ``None``.
``require_admin`` → wraps ``require_user``; raises HTTP 403 if the user is
    not an admin.
``get_params_service_dep`` → returns ``app.state.params_service``; raises
    ``RuntimeError`` if unset.
``get_models_service_dep`` → returns ``app.state.models_service``; raises
    ``RuntimeError`` if unset.
``get_lmstudio_adapter_dep`` → returns ``app.state.lmstudio_adapter``; raises
    ``RuntimeError`` if unset.
``get_memory_service_dep`` → returns ``app.state.memory_service``; raises
    ``RuntimeError`` if unset.
``get_quality_mode_service_dep`` → returns ``app.state.quality_mode_service``;
    raises ``RuntimeError`` if unset.
``get_embedding_client_dep`` → returns ``app.state.embedding_client``; raises
    ``RuntimeError`` if unset.
``get_reindex_status_holder_dep`` → returns ``app.state.reindex_status_holder``;
    raises ``RuntimeError`` if unset.
``get_chat_service_dep`` → returns ``app.state.chat_service``; raises
    ``RuntimeError`` if unset.
``get_message_service_dep`` → returns ``app.state.message_service``; raises
    ``RuntimeError`` if unset.
``get_chat_locks_dep`` → returns ``app.state.chat_locks``; raises
    ``RuntimeError`` if unset.
``get_streaming_service_dep`` → returns ``app.state.streaming_service``; raises
    ``RuntimeError`` if unset.
``stream_rate_limit`` → FastAPI dependency enforcing the per-user stream rate
    limit (30/min by default).  Returns ``None`` on success; raises HTTP 429
    with ``Retry-After`` on exhaustion.
``admin_rate_limit`` → FastAPI dependency enforcing the per-admin-user rate
    limit on admin routes (30/min by default via
    ``lm_chat_admin_rate_limit_per_min``).  Returns ``None`` on success;
    raises HTTP 429 with ``Retry-After`` on exhaustion.
"""
from __future__ import annotations

import math
from typing import TYPE_CHECKING, Final

from fastapi import Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.config import get_settings
from lmchat.db.engine import get_engine
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.services.auth_service import User, get_current_user
from lmchat.session.base import SessionStore

if TYPE_CHECKING:
    import asyncio

    from lmchat.embedding.client import EmbeddingClient
    from lmchat.routes.memory import ReindexStatusHolder
    from lmchat.services.chat_service import ChatService
    from lmchat.services.integrations_service import IntegrationsService
    from lmchat.services.lmstudio_adapter import LmstudioAdapter
    from lmchat.services.memory_service import MemoryService
    from lmchat.services.message_service import MessageService
    from lmchat.services.models_service import ModelsService
    from lmchat.services.params_service import ParamsService
    from lmchat.services.quality_modes import QualityModeService
    from lmchat.services.streaming_service import StreamingService

_SECONDS_PER_MINUTE: Final[float] = 60.0

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_HTTP_401: Final[int] = 401
_HTTP_403: Final[int] = 403


# ---------------------------------------------------------------------------
# Session store dependency
# ---------------------------------------------------------------------------


def get_session_store_dep(request: Request) -> SessionStore:
    """Return the application-attached ``SessionStore`` instance.

    Reads ``request.app.state.session_store`` set by the lifespan in
    ``app.py``. There is no fallback: every route that depends on a
    session store gets the SAME instance the periodic cleanup task owns.
    This is the single-store invariant.

    Tests that construct an app WITHOUT running the lifespan must inject
    a store via ``app.dependency_overrides[get_session_store_dep] = ...``;
    otherwise this dependency raises ``RuntimeError``. The loud failure is
    deliberate — silently creating a second store would break the
    invariant.

    Args:
        request: The incoming FastAPI ``Request``. Used only to access
                 ``app.state``.

    Returns:
        The :class:`~lmchat.session.base.SessionStore` attached to
        ``app.state.session_store`` by the lifespan.

    Raises:
        RuntimeError: If ``app.state.session_store`` is unset. Indicates
            the lifespan did not run AND no test override was registered.
    """
    state_store = getattr(request.app.state, "session_store", None)
    if state_store is None:
        raise RuntimeError(
            "app.state.session_store is unset — the FastAPI lifespan did not "
            "run, and no dependency_overrides entry exists for "
            "get_session_store_dep. Tests bypassing the lifespan must register "
            "an override; production code paths must use the lifespan."
        )
    return state_store


# Backwards-compatibility alias for any caller still using the
# pre-r2 dependency name. The new name is the canonical one.
get_default_session_store_dep = get_session_store_dep


# ---------------------------------------------------------------------------
# Engine dependency
# ---------------------------------------------------------------------------


def get_engine_dep(request: Request) -> AsyncEngine:
    """Return the ``AsyncEngine`` for this application instance.

    Reads ``request.app.state.engine`` set by the lifespan in ``app.py``.
    Falls back to the process-global :func:`~lmchat.db.engine.get_engine`
    singleton only when ``app.state.engine`` is unset (e.g. tests that
    bypass the lifespan and inject an engine via ``dependency_overrides``).

    Using ``app.state.engine`` prevents cross-contamination when multiple
    live server instances share the same process (e.g. ``tests/integration``
    and ``tests/e2e_http`` session-scoped fixtures running concurrently).
    The global ``get_engine()`` singleton is overwritten each time a new
    lifespan boots, so a second live server always displaces the first's
    engine — making ``get_engine_dep()`` return the wrong DB for the
    original server's in-flight requests.

    Returns:
        The :class:`~sqlalchemy.ext.asyncio.AsyncEngine` attached to
        ``app.state.engine`` by the lifespan, or the global singleton
        as a fallback.
    """
    state_engine = getattr(getattr(request.app, "state", None), "engine", None)
    if state_engine is not None:
        return state_engine
    return get_engine()


# ---------------------------------------------------------------------------
# Current-user dependency
# ---------------------------------------------------------------------------


async def get_current_user_dep(
    request: Request,
    store: SessionStore = Depends(get_default_session_store_dep),
    engine: AsyncEngine = Depends(get_engine_dep),
) -> User | None:
    """Bridge ``auth_service.get_current_user`` into a FastAPI dependency.

    Resolves the authenticated user from the ``lmchat_session`` cookie or
    ``Authorization: Bearer <token>`` header.  Returns ``None`` if
    unauthenticated rather than raising, so callers that want an
    optional user can use this directly; callers that require auth wrap
    with :func:`require_user`.

    Args:
        request: The incoming FastAPI ``Request``.
        store:   The session store dependency.
        engine:  The engine dependency.

    Returns:
        The authenticated :class:`~lmchat.services.auth_service.User`, or
        ``None`` if the request carries no valid session.
    """
    return await get_current_user(request, session_store=store, engine=engine)


# ---------------------------------------------------------------------------
# require_user / require_admin guards
# ---------------------------------------------------------------------------


async def require_user(
    user: User | None = Depends(get_current_user_dep),
) -> User:
    """Raise HTTP 401 if the request is unauthenticated.

    Use this as a ``Depends`` on routes that require a logged-in user.

    Args:
        user: The optional user from :func:`get_current_user_dep`.

    Returns:
        The authenticated :class:`~lmchat.services.auth_service.User`.

    Raises:
        HTTPException: 401 Unauthorized if ``user`` is ``None``.
    """
    if user is None:
        raise HTTPException(
            status_code=_HTTP_401,
            detail="authentication required",
        )
    return user


async def require_admin(
    user: User = Depends(require_user),
) -> User:
    """Raise HTTP 403 if the authenticated user is not an admin.

    Use this as a ``Depends`` on routes that require admin privileges.
    Because it wraps :func:`require_user`, unauthenticated requests
    still receive a 401 (not 403).

    Placeholder: enforces the is_admin gate from the User schema.
    Security-headers + audit-log integration is added on top of this check
    via ``middleware/auth.py``; the gate logic itself is complete.

    Args:
        user: The authenticated user from :func:`require_user`.

    Returns:
        The authenticated admin :class:`~lmchat.services.auth_service.User`.

    Raises:
        HTTPException: 403 Forbidden if ``user.is_admin`` is ``False``.
    """
    if not user.is_admin:
        raise HTTPException(
            status_code=_HTTP_403,
            detail="admin privileges required",
        )
    return user


# ---------------------------------------------------------------------------
# Params / models / LM Studio adapter service dependencies
# ---------------------------------------------------------------------------


def get_params_service_dep(request: Request) -> ParamsService:
    """Return the ``ParamsService`` attached to ``app.state.params_service``.

    Reads the instance stored by the lifespan in ``app.py``.  Raises
    ``RuntimeError`` loudly if unset — the silent fallback (creating a second
    instance) would break the single-instance invariant and lose cache state.
    Tests bypassing the lifespan must inject via ``app.dependency_overrides``.

    Args:
        request: The incoming FastAPI ``Request`` (used to access
                 ``request.app.state``).

    Returns:
        The singleton :class:`~lmchat.services.params_service.ParamsService`.

    Raises:
        RuntimeError: If ``app.state.params_service`` is unset.
    """
    svc = getattr(request.app.state, "params_service", None)
    if svc is None:
        raise RuntimeError(
            "app.state.params_service is unset — the FastAPI lifespan did not "
            "run, and no dependency_overrides entry exists for "
            "get_params_service_dep. Tests bypassing the lifespan must register "
            "an override; production code paths must use the lifespan."
        )
    return svc  # type: ignore[return-value]


def get_models_service_dep(request: Request) -> ModelsService:
    """Return the ``ModelsService`` attached to ``app.state.models_service``.

    Same loud-failure pattern as :func:`get_params_service_dep`.

    Args:
        request: The incoming FastAPI ``Request``.

    Returns:
        The singleton :class:`~lmchat.services.models_service.ModelsService`.

    Raises:
        RuntimeError: If ``app.state.models_service`` is unset.
    """
    svc = getattr(request.app.state, "models_service", None)
    if svc is None:
        raise RuntimeError(
            "app.state.models_service is unset — the FastAPI lifespan did not "
            "run, and no dependency_overrides entry exists for "
            "get_models_service_dep."
        )
    return svc  # type: ignore[return-value]


def get_lmstudio_adapter_dep(request: Request) -> LmstudioAdapter:
    """Return the ``LmstudioAdapter`` attached to ``app.state.lmstudio_adapter``.

    Same loud-failure pattern as :func:`get_params_service_dep`.

    Args:
        request: The incoming FastAPI ``Request``.

    Returns:
        The singleton :class:`~lmchat.services.lmstudio_adapter.LmstudioAdapter`.

    Raises:
        RuntimeError: If ``app.state.lmstudio_adapter`` is unset.
    """
    svc = getattr(request.app.state, "lmstudio_adapter", None)
    if svc is None:
        raise RuntimeError(
            "app.state.lmstudio_adapter is unset — the FastAPI lifespan did not "
            "run, and no dependency_overrides entry exists for "
            "get_lmstudio_adapter_dep."
        )
    return svc  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Memory / RAG service dependencies
# ---------------------------------------------------------------------------


def get_memory_service_dep(request: Request) -> MemoryService:
    """Return the ``MemoryService`` attached to ``app.state.memory_service``.

    Same loud-failure pattern as :func:`get_params_service_dep`.

    Args:
        request: The incoming FastAPI ``Request``.

    Returns:
        The singleton :class:`~lmchat.services.memory_service.MemoryService`.

    Raises:
        RuntimeError: If ``app.state.memory_service`` is unset.
    """
    svc = getattr(request.app.state, "memory_service", None)
    if svc is None:
        raise RuntimeError(
            "app.state.memory_service is unset — the FastAPI lifespan did not "
            "run, and no dependency_overrides entry exists for "
            "get_memory_service_dep."
        )
    return svc  # type: ignore[return-value]


def get_quality_mode_service_dep(request: Request) -> QualityModeService:
    """Return the ``QualityModeService`` attached to ``app.state.quality_mode_service``.

    Same loud-failure pattern as :func:`get_params_service_dep`.

    Args:
        request: The incoming FastAPI ``Request``.

    Returns:
        The singleton
        :class:`~lmchat.services.quality_modes.QualityModeService`.

    Raises:
        RuntimeError: If ``app.state.quality_mode_service`` is unset.
    """
    svc = getattr(request.app.state, "quality_mode_service", None)
    if svc is None:
        raise RuntimeError(
            "app.state.quality_mode_service is unset — the FastAPI lifespan did not "
            "run, and no dependency_overrides entry exists for "
            "get_quality_mode_service_dep."
        )
    return svc  # type: ignore[return-value]


def get_embedding_client_dep(request: Request) -> EmbeddingClient:
    """Return the ``EmbeddingClient`` attached to ``app.state.embedding_client``.

    Same loud-failure pattern as :func:`get_params_service_dep`.

    Args:
        request: The incoming FastAPI ``Request``.

    Returns:
        The singleton :class:`~lmchat.embedding.client.EmbeddingClient`.

    Raises:
        RuntimeError: If ``app.state.embedding_client`` is unset.
    """
    client = getattr(request.app.state, "embedding_client", None)
    if client is None:
        raise RuntimeError(
            "app.state.embedding_client is unset — the FastAPI lifespan did not "
            "run, and no dependency_overrides entry exists for "
            "get_embedding_client_dep."
        )
    return client  # type: ignore[return-value]


def get_reindex_status_holder_dep(request: Request) -> ReindexStatusHolder:
    """Return the ``ReindexStatusHolder`` from ``app.state.reindex_status_holder``.

    Same loud-failure pattern as :func:`get_params_service_dep`.

    Args:
        request: The incoming FastAPI ``Request``.

    Returns:
        The :class:`~lmchat.routes.memory.ReindexStatusHolder` attached by
        the lifespan.

    Raises:
        RuntimeError: If ``app.state.reindex_status_holder`` is unset.
    """
    holder = getattr(request.app.state, "reindex_status_holder", None)
    if holder is None:
        raise RuntimeError(
            "app.state.reindex_status_holder is unset — the FastAPI lifespan did not "
            "run, and no dependency_overrides entry exists for "
            "get_reindex_status_holder_dep."
        )
    return holder  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Chat service dependencies
# ---------------------------------------------------------------------------


def get_chat_service_dep(request: Request) -> ChatService:
    """Return the ``ChatService`` attached to ``app.state.chat_service``.

    Same loud-failure pattern as :func:`get_params_service_dep`.

    Args:
        request: The incoming FastAPI ``Request``.

    Returns:
        The singleton :class:`~lmchat.services.chat_service.ChatService`.

    Raises:
        RuntimeError: If ``app.state.chat_service`` is unset.
    """
    svc = getattr(request.app.state, "chat_service", None)
    if svc is None:
        raise RuntimeError(
            "app.state.chat_service is unset — the FastAPI lifespan did not "
            "run, and no dependency_overrides entry exists for "
            "get_chat_service_dep."
        )
    return svc  # type: ignore[return-value]


def get_message_service_dep(request: Request) -> MessageService:
    """Return the ``MessageService`` attached to ``app.state.message_service``.

    Same loud-failure pattern as :func:`get_params_service_dep`.

    Args:
        request: The incoming FastAPI ``Request``.

    Returns:
        The singleton :class:`~lmchat.services.message_service.MessageService`.

    Raises:
        RuntimeError: If ``app.state.message_service`` is unset.
    """
    svc = getattr(request.app.state, "message_service", None)
    if svc is None:
        raise RuntimeError(
            "app.state.message_service is unset — the FastAPI lifespan did not "
            "run, and no dependency_overrides entry exists for "
            "get_message_service_dep."
        )
    return svc  # type: ignore[return-value]


def get_chat_locks_dep(request: Request) -> dict[int, asyncio.Lock]:
    """Return the per-chat lock dict from ``app.state.chat_locks``.

    The dict is shared between ``ChatService`` (compact) and the streaming
    service so both hold the same per-chat mutex.  It is populated lazily
    via ``dict.setdefault`` inside the service — this dep only validates
    the dict was registered at startup.

    Args:
        request: The incoming FastAPI ``Request``.

    Returns:
        The ``dict[int, asyncio.Lock]`` attached by the lifespan.

    Raises:
        RuntimeError: If ``app.state.chat_locks`` is unset.
    """
    locks = getattr(request.app.state, "chat_locks", None)
    if locks is None:
        raise RuntimeError(
            "app.state.chat_locks is unset — the FastAPI lifespan did not "
            "run, and no dependency_overrides entry exists for "
            "get_chat_locks_dep."
        )
    return locks  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# Streaming service dependencies
# ---------------------------------------------------------------------------


def get_streaming_service_dep(request: Request) -> StreamingService:
    """Return the ``StreamingService`` attached to ``app.state.streaming_service``.

    Same loud-failure pattern as :func:`get_params_service_dep`.

    Args:
        request: The incoming FastAPI ``Request``.

    Returns:
        The singleton :class:`~lmchat.services.streaming_service.StreamingService`.

    Raises:
        RuntimeError: If ``app.state.streaming_service`` is unset.
    """
    svc = getattr(request.app.state, "streaming_service", None)
    if svc is None:
        raise RuntimeError(
            "app.state.streaming_service is unset — the FastAPI lifespan did "
            "not run, and no dependency_overrides entry exists for "
            "get_streaming_service_dep. Tests bypassing the lifespan must "
            "register an override; production code paths must use the lifespan."
        )
    return svc  # type: ignore[return-value]


def get_integrations_service_dep(request: Request) -> IntegrationsService:
    """Return the ``IntegrationsService`` attached to ``app.state.integrations_service``.

    Same loud-failure pattern as :func:`get_params_service_dep`.

    Provides access to the MCP integrations list.

    Args:
        request: The incoming FastAPI ``Request``.

    Returns:
        The singleton
        :class:`~lmchat.services.integrations_service.IntegrationsService`.

    Raises:
        RuntimeError: If ``app.state.integrations_service`` is unset.
    """
    svc = getattr(request.app.state, "integrations_service", None)
    if svc is None:
        raise RuntimeError(
            "app.state.integrations_service is unset — the FastAPI lifespan did not "
            "run, and no dependency_overrides entry exists for "
            "get_integrations_service_dep. Tests bypassing the lifespan must register "
            "an override; production code paths must use the lifespan."
        )
    return svc  # type: ignore[return-value]


async def stream_rate_limit(
    request: Request,
    response: Response,
    user: User = Depends(require_user),
) -> None:
    """FastAPI dependency: enforce the per-user stream rate limit.

    Uses ``app.state.stream_buckets`` (an ``InMemoryBucketStore`` attached by
    the lifespan) with key ``f"stream:{user.id}"``.

    Default: 30 streams per minute (``LM_CHAT_STREAM_RATE_LIMIT_PER_MIN``).

    On success: returns ``None`` (FastAPI discards it).
    On exhaustion: raises HTTP 429 with ``Retry-After`` header.

    Args:
        request:  The FastAPI ``Request`` (for ``app.state`` access).
        response: The FastAPI ``Response`` (for setting ``Retry-After``).
        user:     The authenticated user (resolved by ``require_user``).

    Raises:
        HTTPException: 429 Too Many Requests when the bucket is empty.
    """
    # Resolve the bucket store from app.state. NO ephemeral fallback:
    # silently creating a store would mean each request gets its own
    # never-shared bucket and rate-limiting is effectively disabled —
    # exactly the production misconfiguration footgun this guards against.
    # Loud RuntimeError matches the single-instance invariant pattern
    # above; tests that bypass the lifespan MUST register a
    # dependency_override.
    store: InMemoryBucketStore | None = getattr(
        request.app.state,
        "stream_buckets",
        None,
    )
    if store is None:
        raise RuntimeError(
            "app.state.stream_buckets is unset — the FastAPI lifespan did "
            "not run, and no dependency_overrides entry exists for "
            "stream_rate_limit. Tests bypassing the lifespan must register "
            "an override; production code paths must use the lifespan."
        )

    # Resolve rate limit from settings (lazy; cached by lru_cache).
    settings = get_settings()
    rate_per_min: int = getattr(settings, "lm_chat_stream_rate_limit_per_min", 30)
    burst: int = rate_per_min
    rate_per_sec = rate_per_min / _SECONDS_PER_MINUTE

    key = f"stream:{user.id}"  # type: ignore[attr-defined]
    allowed, retry_after = await store.consume(key, rate=rate_per_sec, burst=burst)

    if allowed:
        return None  # type: ignore[return-value]

    retry_after_secs = math.ceil(retry_after)
    response.headers["Retry-After"] = str(retry_after_secs)
    raise HTTPException(
        status_code=429,
        detail=f"Stream rate limit exceeded. Retry after {retry_after_secs}s.",
        headers={"Retry-After": str(retry_after_secs)},
    )


async def admin_rate_limit(
    request: Request,
    response: Response,
    user: User = Depends(require_admin),
) -> None:
    """FastAPI dependency: enforce the per-admin-user rate limit on admin routes.

    Uses ``app.state.admin_buckets`` (an ``InMemoryBucketStore`` attached by
    the lifespan) with key ``f"admin:{user.id}"``.

    Default: 30 requests per minute (``LM_CHAT_ADMIN_RATE_LIMIT_PER_MIN``).
    Prevents a compromised admin session from spamming ``revoke-sessions`` or
    ``models/refresh``.

    On success: returns ``None`` (FastAPI discards it).
    On exhaustion: raises HTTP 429 with ``Retry-After`` header.

    Args:
        request:  The FastAPI ``Request`` (for ``app.state`` access).
        response: The FastAPI ``Response`` (for setting ``Retry-After``).
        user:     The authenticated admin user (resolved by ``require_admin``).

    Raises:
        RuntimeError:  If ``app.state.admin_buckets`` is unset (lifespan did
                       not run AND no test override registered).
        HTTPException: 429 Too Many Requests when the bucket is empty.
    """
    store: InMemoryBucketStore | None = getattr(
        request.app.state,
        "admin_buckets",
        None,
    )
    if store is None:
        raise RuntimeError(
            "app.state.admin_buckets is unset — the FastAPI lifespan did "
            "not run, and no dependency_overrides entry exists for "
            "admin_rate_limit. Tests bypassing the lifespan must register "
            "an override; production code paths must use the lifespan."
        )

    settings = get_settings()
    rate_per_min: int = getattr(settings, "lm_chat_admin_rate_limit_per_min", 30)
    burst: int = rate_per_min
    rate_per_sec = rate_per_min / _SECONDS_PER_MINUTE

    key = f"admin:{user.id}"  # type: ignore[attr-defined]
    allowed, retry_after = await store.consume(key, rate=rate_per_sec, burst=burst)

    if allowed:
        return None  # type: ignore[return-value]

    retry_after_secs = math.ceil(retry_after)
    response.headers["Retry-After"] = str(retry_after_secs)
    raise HTTPException(
        status_code=429,
        detail=f"Admin rate limit exceeded. Retry after {retry_after_secs}s.",
        headers={"Retry-After": str(retry_after_secs)},
    )
