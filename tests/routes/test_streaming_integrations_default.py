# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the P13h-fix: admin-default integrations applied at the
streaming route when the FE omits the integrations field (None).

Invariants:
  1. integrations=None (omitted by FE) → route resolves admin defaults and
     stamps them onto payload.payload.integrations before handing off to
     streaming_service.
  2. integrations=[] (explicit empty) → left as-is (user chose "no tools").
  3. integrations=["mcp/x"] (explicit list) → left as-is.

The streaming_service and integrations_service are mocked with
dependency_overrides so these tests exercise only the route handler logic.
"""
from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# App factory (mirrors tests/routes/test_streaming.py)
# ---------------------------------------------------------------------------


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/integ_default_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")

    get_settings.cache_clear()
    engine_mod.dispose_engine()

    return create_app()


def _cleanup() -> None:
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    get_settings.cache_clear()
    engine_mod.dispose_engine()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_stream_body(
    chat_id: int = 1,
    integrations: list[str] | None = None,
    include_integrations: bool = False,
) -> dict[str, Any]:
    """Build a ChatStreamRequest body.

    Args:
        chat_id:             The chat_id field.
        integrations:        Value to put in payload.integrations.
        include_integrations: Whether to include the integrations key at all.
                             When False (and include_integrations is False),
                             the key is absent (simulates FE omitting the field).
    """
    inner: dict[str, Any] = {
        "model": "test-model",
        "input": [{"type": "text", "content": "hello"}],
    }
    if include_integrations:
        inner["integrations"] = integrations  # may be [] or [...]
    return {"chat_id": chat_id, "payload": inner}


def _make_integration_entry(
    value: str,
    enabled_by_default: bool,
) -> Any:
    """Return a minimal IntegrationEntry-like object (only .value and
    .enabled_by_default are read by the route handler)."""
    entry = MagicMock()
    entry.value = value
    entry.enabled_by_default = enabled_by_default
    return entry


def _make_mock_streaming_svc(captured: dict[str, Any]) -> MagicMock:
    """Return a mock StreamingService that captures the payload passed to stream_chat."""

    async def _stream(
        *,
        payload: Any,
        **_kwargs: object,
    ) -> AsyncIterator[bytes]:
        captured["payload"] = payload
        frame = b'event: chat.start\ndata: {"type": "chat.start", "msg_id": 1}\n\n'
        yield frame

    svc = MagicMock()
    svc.stream_chat = _stream
    return svc


# ---------------------------------------------------------------------------
# Test 1: integrations=None → admin defaults applied
# ---------------------------------------------------------------------------


def test_integrations_none_applies_admin_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When FE omits integrations (None), route stamps the admin-default entries."""
    app = _make_app(tmp_path, monkeypatch)

    captured: dict[str, Any] = {}

    with TestClient(app, raise_server_exceptions=False) as client:
        # Register + login.
        client.post(
            "/api/auth/register",
            data={"username": "default_user_1", "password": "Test1234!"},
        )
        login = client.post(
            "/api/auth/login",
            data={"username": "default_user_1", "password": "Test1234!"},
        )
        assert login.status_code == 200, f"Login failed: {login.text}"

        from lmchat.routes._dependencies import (
            get_integrations_service_dep,
            get_streaming_service_dep,
        )

        # IntegrationsService mock: 2 entries, one enabled_by_default=True.
        mock_integ_svc = MagicMock()
        # Layer 2 (resilience-fix pre-flight filter): default to "unknown
        # live set" (empty) so these tests — which aren't exercising the
        # live-set filter — are unaffected by it. A bare MagicMock's
        # unconfigured __contains__ returns False for everything, which
        # would otherwise make the live-filter (incorrectly) drop every
        # integration; explicitly returning a real empty set keeps the
        # "unknown → don't filter" contract instead.
        mock_integ_svc.live_serveable_ids = MagicMock(return_value=set())
        mock_integ_svc.list_available = AsyncMock(
            return_value=[
                _make_integration_entry("mcp/firecrawl", enabled_by_default=True),
                _make_integration_entry("mcp/searxng", enabled_by_default=False),
            ]
        )

        mock_stream_svc = _make_mock_streaming_svc(captured)

        app.dependency_overrides[get_streaming_service_dep] = lambda: mock_stream_svc
        app.dependency_overrides[get_integrations_service_dep] = (
            lambda: mock_integ_svc
        )

        # Omit integrations → None in CanonicalChatRequest.
        resp = client.post(
            "/api/chat/stream",
            json=_make_stream_body(include_integrations=False),
        )
        app.dependency_overrides.clear()

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    # Only the enabled_by_default=True entry should be stamped.
    resolved = captured["payload"].payload.integrations
    assert resolved == ["mcp/firecrawl"], (
        f"Expected admin defaults ['mcp/firecrawl'], got {resolved!r}"
    )

    _cleanup()


# ---------------------------------------------------------------------------
# Test 2: integrations=[] (explicit empty) → stays []
# ---------------------------------------------------------------------------


def test_integrations_explicit_empty_stays_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When FE sends integrations=[], route honours it — user chose no tools."""
    app = _make_app(tmp_path, monkeypatch)

    captured: dict[str, Any] = {}

    with TestClient(app, raise_server_exceptions=False) as client:
        client.post(
            "/api/auth/register",
            data={"username": "default_user_2", "password": "Test1234!"},
        )
        login = client.post(
            "/api/auth/login",
            data={"username": "default_user_2", "password": "Test1234!"},
        )
        assert login.status_code == 200

        from lmchat.routes._dependencies import (
            get_integrations_service_dep,
            get_streaming_service_dep,
        )

        mock_integ_svc = MagicMock()
        # Layer 2 (resilience-fix pre-flight filter): default to "unknown
        # live set" (empty) so these tests — which aren't exercising the
        # live-set filter — are unaffected by it. A bare MagicMock's
        # unconfigured __contains__ returns False for everything, which
        # would otherwise make the live-filter (incorrectly) drop every
        # integration; explicitly returning a real empty set keeps the
        # "unknown → don't filter" contract instead.
        mock_integ_svc.live_serveable_ids = MagicMock(return_value=set())
        # list_available should NOT be called for explicit []
        mock_integ_svc.list_available = AsyncMock(
            return_value=[
                _make_integration_entry("mcp/firecrawl", enabled_by_default=True),
            ]
        )

        mock_stream_svc = _make_mock_streaming_svc(captured)

        app.dependency_overrides[get_streaming_service_dep] = lambda: mock_stream_svc
        app.dependency_overrides[get_integrations_service_dep] = (
            lambda: mock_integ_svc
        )

        resp = client.post(
            "/api/chat/stream",
            json=_make_stream_body(integrations=[], include_integrations=True),
        )
        app.dependency_overrides.clear()

    assert resp.status_code == 200

    resolved = captured["payload"].payload.integrations
    assert resolved == [], (
        f"Expected explicit [] to be honoured, got {resolved!r}"
    )

    # list_available must NOT have been called for an explicit [].
    mock_integ_svc.list_available.assert_not_called()

    _cleanup()


# ---------------------------------------------------------------------------
# Test 3: explicit list → honoured but filtered to catalog-available ids
# ---------------------------------------------------------------------------


def test_integrations_explicit_list_available_preserved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit list of still-available ids passes through unchanged."""
    app = _make_app(tmp_path, monkeypatch)

    captured: dict[str, Any] = {}

    with TestClient(app, raise_server_exceptions=False) as client:
        client.post(
            "/api/auth/register",
            data={"username": "default_user_3", "password": "Test1234!"},
        )
        login = client.post(
            "/api/auth/login",
            data={"username": "default_user_3", "password": "Test1234!"},
        )
        assert login.status_code == 200

        from lmchat.routes._dependencies import (
            get_integrations_service_dep,
            get_streaming_service_dep,
        )

        mock_integ_svc = MagicMock()
        # Layer 2 (resilience-fix pre-flight filter): default to "unknown
        # live set" (empty) so these tests — which aren't exercising the
        # live-set filter — are unaffected by it. A bare MagicMock's
        # unconfigured __contains__ returns False for everything, which
        # would otherwise make the live-filter (incorrectly) drop every
        # integration; explicitly returning a real empty set keeps the
        # "unknown → don't filter" contract instead.
        mock_integ_svc.live_serveable_ids = MagicMock(return_value=set())
        mock_integ_svc.list_available = AsyncMock(
            return_value=[
                _make_integration_entry("mcp/searxng", enabled_by_default=True),
                _make_integration_entry("mcp/crawl4ai", enabled_by_default=False),
            ]
        )

        mock_stream_svc = _make_mock_streaming_svc(captured)

        app.dependency_overrides[get_streaming_service_dep] = lambda: mock_stream_svc
        app.dependency_overrides[get_integrations_service_dep] = (
            lambda: mock_integ_svc
        )

        resp = client.post(
            "/api/chat/stream",
            json=_make_stream_body(
                integrations=["mcp/searxng", "mcp/crawl4ai"],
                include_integrations=True,
            ),
        )
        app.dependency_overrides.clear()

    assert resp.status_code == 200

    resolved = captured["payload"].payload.integrations
    assert resolved == ["mcp/searxng", "mcp/crawl4ai"], (
        f"Expected available ids preserved, got {resolved!r}"
    )

    _cleanup()


# ---------------------------------------------------------------------------
# Test 4: explicit list containing a removed id (mcp/firecrawl) → dropped
# ---------------------------------------------------------------------------


def test_integrations_explicit_list_drops_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale/removed id (e.g. mcp/firecrawl) is dropped, not forwarded.

    Regression: a removed MCP server left in the FE's cached per-chat selection
    would otherwise reach LM Studio and crash the stream with
    "Cannot find plugin handle for plugin: mcp/firecrawl".
    """
    app = _make_app(tmp_path, monkeypatch)

    captured: dict[str, Any] = {}

    with TestClient(app, raise_server_exceptions=False) as client:
        client.post(
            "/api/auth/register",
            data={"username": "default_user_4", "password": "Test1234!"},
        )
        login = client.post(
            "/api/auth/login",
            data={"username": "default_user_4", "password": "Test1234!"},
        )
        assert login.status_code == 200

        from lmchat.routes._dependencies import (
            get_integrations_service_dep,
            get_streaming_service_dep,
        )

        mock_integ_svc = MagicMock()
        # Layer 2 (resilience-fix pre-flight filter): default to "unknown
        # live set" (empty) so these tests — which aren't exercising the
        # live-set filter — are unaffected by it. A bare MagicMock's
        # unconfigured __contains__ returns False for everything, which
        # would otherwise make the live-filter (incorrectly) drop every
        # integration; explicitly returning a real empty set keeps the
        # "unknown → don't filter" contract instead.
        mock_integ_svc.live_serveable_ids = MagicMock(return_value=set())
        mock_integ_svc.list_available = AsyncMock(
            return_value=[
                _make_integration_entry("mcp/searxng", enabled_by_default=True),
            ]
        )

        mock_stream_svc = _make_mock_streaming_svc(captured)

        app.dependency_overrides[get_streaming_service_dep] = lambda: mock_stream_svc
        app.dependency_overrides[get_integrations_service_dep] = (
            lambda: mock_integ_svc
        )

        resp = client.post(
            "/api/chat/stream",
            json=_make_stream_body(
                integrations=["mcp/firecrawl", "mcp/searxng"],
                include_integrations=True,
            ),
        )
        app.dependency_overrides.clear()

    assert resp.status_code == 200

    resolved = captured["payload"].payload.integrations
    assert resolved == ["mcp/searxng"], (
        f"Expected mcp/firecrawl dropped, got {resolved!r}"
    )

    _cleanup()


# ---------------------------------------------------------------------------
# Fix G — list_available() DB error must not 500 new chats
# ---------------------------------------------------------------------------


def test_list_available_raises_falls_back_to_empty_not_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fix G: when list_available() raises (transient DB error), the streaming
    route must degrade to empty integrations and return 200, not 500.

    Red-on-revert: removing the try/except in the streaming route causes the
    exception to propagate and return a 500 error response.
    """
    app = _make_app(tmp_path, monkeypatch)

    captured: dict[str, Any] = {}

    with TestClient(app, raise_server_exceptions=False) as client:
        client.post(
            "/api/auth/register",
            data={"username": "fixg_user_1", "password": "Test1234!"},
        )
        login = client.post(
            "/api/auth/login",
            data={"username": "fixg_user_1", "password": "Test1234!"},
        )
        assert login.status_code == 200, f"Login failed: {login.text}"

        from lmchat.routes._dependencies import (
            get_integrations_service_dep,
            get_streaming_service_dep,
        )

        # list_available raises a transient DB error.
        mock_integ_svc = MagicMock()
        # Layer 2 (resilience-fix pre-flight filter): default to "unknown
        # live set" (empty) so these tests — which aren't exercising the
        # live-set filter — are unaffected by it. A bare MagicMock's
        # unconfigured __contains__ returns False for everything, which
        # would otherwise make the live-filter (incorrectly) drop every
        # integration; explicitly returning a real empty set keeps the
        # "unknown → don't filter" contract instead.
        mock_integ_svc.live_serveable_ids = MagicMock(return_value=set())
        mock_integ_svc.list_available = AsyncMock(
            side_effect=RuntimeError("SQLite OperationalError: database is locked")
        )

        mock_stream_svc = _make_mock_streaming_svc(captured)

        app.dependency_overrides[get_streaming_service_dep] = lambda: mock_stream_svc
        app.dependency_overrides[get_integrations_service_dep] = (
            lambda: mock_integ_svc
        )

        # integrations omitted → None → triggers the default-lookup path.
        resp = client.post(
            "/api/chat/stream",
            json=_make_stream_body(include_integrations=False),
        )
        app.dependency_overrides.clear()

    # Must degrade gracefully — 200, not 500.
    assert resp.status_code == 200, (
        f"Expected 200 on list_available() failure; got {resp.status_code}: {resp.text}"
    )

    # The payload must have been delivered with empty integrations.
    resolved = captured.get("payload")
    assert resolved is not None, "streaming_service.stream_chat was never called"
    assert resolved.payload.integrations == [], (
        f"Expected empty integrations fallback; got {resolved.payload.integrations!r}"
    )

    _cleanup()


# ---------------------------------------------------------------------------
# Fix G — 404 tool-use translation regression guard
# ---------------------------------------------------------------------------


def test_404_tool_use_translation_fires_on_openrouter_string() -> None:
    """Fix G: guard that the 'support tool use' → friendly message translation
    in StreamingService still fires when the OpenRouter error string is present.

    This prevents a silent regression if the match string or translation logic
    is accidentally removed (commit 3035e2c).
    """
    from lmchat.lmstudio.types import CanonicalEvent

    # Reproduce the exact error shape OpenRouter sends for a model that doesn't
    # support tool use (the string the real translation checks against).
    raw_error_event = CanonicalEvent(
        type="error",
        error={
            "code": "upstream_error",
            "message": "No endpoints support tool use",
        },
    )

    # Replicate the translation logic from streaming_service.py:2246-2263.
    event = raw_error_event
    if (
        event.type == "error"
        and event.error is not None
        and "support tool use"
        in str(event.error.get("message", "")).lower()
    ):
        event = event.model_copy(
            update={
                "error": {
                    **event.error,
                    "message": (
                        "This model doesn't support tools. Turn "
                        "off integrations for this chat, or pick "
                        "a tool-capable model."
                    ),
                }
            }
        )

    # The translation must have fired.
    assert event.error is not None
    assert "doesn't support tools" in event.error["message"], (
        f"Translation did not fire; message is: {event.error['message']!r}"
    )
    # The friendly message must be present, not the raw OpenRouter string.
    assert "support tool use" not in event.error["message"].lower(), (
        f"Raw OpenRouter error string still present after translation: {event.error['message']!r}"
    )


# ---------------------------------------------------------------------------
# Store-sourced integration must survive pre-flight validation
# ---------------------------------------------------------------------------


def test_integrations_explicit_store_slug_not_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Store-installed MCP server's integration id must NOT be dropped by
    the pre-flight validator just because it's absent from the curated
    (LM-Studio-source) catalog.

    Red-on-revert: before the fix, the validator only checks
    ``integrations_service.list_available()`` (curated-only), so
    ``mcp/mytool`` — installed via the MCP Store, not in mcp.json — gets
    treated as a stale/unknown id and dropped, exactly like the removed-
    firecrawl regression in ``test_integrations_explicit_list_drops_unavailable``.
    """
    app = _make_app(tmp_path, monkeypatch)

    captured: dict[str, Any] = {}

    with TestClient(app, raise_server_exceptions=False) as client:
        client.post(
            "/api/auth/register",
            data={"username": "store_slug_user", "password": "Test1234!"},
        )
        login = client.post(
            "/api/auth/login",
            data={"username": "store_slug_user", "password": "Test1234!"},
        )
        assert login.status_code == 200, f"Login failed: {login.text}"

        from lmchat.routes._dependencies import (
            get_integrations_service_dep,
            get_streaming_service_dep,
        )
        from lmchat.services.mcp_server_store import McpServerSafeView

        # Curated catalog only knows about mcp/searxng (LM-Studio source).
        mock_integ_svc = MagicMock()
        # Layer 2 (resilience-fix pre-flight filter): default to "unknown
        # live set" (empty) so these tests — which aren't exercising the
        # live-set filter — are unaffected by it. A bare MagicMock's
        # unconfigured __contains__ returns False for everything, which
        # would otherwise make the live-filter (incorrectly) drop every
        # integration; explicitly returning a real empty set keeps the
        # "unknown → don't filter" contract instead.
        mock_integ_svc.live_serveable_ids = MagicMock(return_value=set())
        mock_integ_svc.list_available = AsyncMock(
            return_value=[
                _make_integration_entry("mcp/searxng", enabled_by_default=False),
            ]
        )

        # Store has an installed, enabled+consented server "mytool" — NOT in
        # the curated list.
        mock_store = AsyncMock()
        mock_store.list_all = AsyncMock(
            return_value=[
                McpServerSafeView(
                    id=1,
                    slug="mytool",
                    name="My Tool",
                    transport="stdio",
                    command="mytool",
                    args=None,
                    url=None,
                    secrets_set=[],
                    enabled=True,
                    source="store",
                    trust="unverified",
                    consented=True,
                ),
            ]
        )
        app.state.mcp_server_store = mock_store

        mock_stream_svc = _make_mock_streaming_svc(captured)

        app.dependency_overrides[get_streaming_service_dep] = lambda: mock_stream_svc
        app.dependency_overrides[get_integrations_service_dep] = (
            lambda: mock_integ_svc
        )

        resp = client.post(
            "/api/chat/stream",
            json=_make_stream_body(
                integrations=["mcp/mytool"],
                include_integrations=True,
            ),
        )
        app.dependency_overrides.clear()

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    resolved = captured["payload"].payload.integrations
    assert resolved == ["mcp/mytool"], (
        f"Expected the Store-sourced integration to survive pre-flight "
        f"validation, got {resolved!r}"
    )

    _cleanup()


def test_integrations_explicit_store_slug_disabled_still_dropped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A DISABLED store server's slug must still be dropped — the merge only
    admits enabled+consented store servers, matching the composer listing
    rule in routes/integrations.py.

    Uses a non-empty curated catalog (an unrelated mcp/searxng entry) so the
    "don't filter against an empty catalog" guard doesn't mask the assertion
    — with an empty catalog, filtering is skipped entirely regardless of the
    store merge, which would prove nothing about the disabled-server rule.
    """
    app = _make_app(tmp_path, monkeypatch)

    captured: dict[str, Any] = {}

    with TestClient(app, raise_server_exceptions=False) as client:
        client.post(
            "/api/auth/register",
            data={"username": "store_slug_user_2", "password": "Test1234!"},
        )
        login = client.post(
            "/api/auth/login",
            data={"username": "store_slug_user_2", "password": "Test1234!"},
        )
        assert login.status_code == 200

        from lmchat.routes._dependencies import (
            get_integrations_service_dep,
            get_streaming_service_dep,
        )
        from lmchat.services.mcp_server_store import McpServerSafeView

        mock_integ_svc = MagicMock()
        # Layer 2 (resilience-fix pre-flight filter): default to "unknown
        # live set" (empty) so these tests — which aren't exercising the
        # live-set filter — are unaffected by it. A bare MagicMock's
        # unconfigured __contains__ returns False for everything, which
        # would otherwise make the live-filter (incorrectly) drop every
        # integration; explicitly returning a real empty set keeps the
        # "unknown → don't filter" contract instead.
        mock_integ_svc.live_serveable_ids = MagicMock(return_value=set())
        mock_integ_svc.list_available = AsyncMock(
            return_value=[
                _make_integration_entry("mcp/searxng", enabled_by_default=False),
            ]
        )

        mock_store = AsyncMock()
        mock_store.list_all = AsyncMock(
            return_value=[
                McpServerSafeView(
                    id=1,
                    slug="mytool",
                    name="My Tool",
                    transport="stdio",
                    command="mytool",
                    args=None,
                    url=None,
                    secrets_set=[],
                    enabled=False,  # disabled — must not survive the merge
                    source="store",
                    trust="unverified",
                    consented=True,
                ),
            ]
        )
        app.state.mcp_server_store = mock_store

        mock_stream_svc = _make_mock_streaming_svc(captured)

        app.dependency_overrides[get_streaming_service_dep] = lambda: mock_stream_svc
        app.dependency_overrides[get_integrations_service_dep] = (
            lambda: mock_integ_svc
        )

        resp = client.post(
            "/api/chat/stream",
            json=_make_stream_body(
                integrations=["mcp/mytool"],
                include_integrations=True,
            ),
        )
        app.dependency_overrides.clear()

    assert resp.status_code == 200

    resolved = captured["payload"].payload.integrations
    assert resolved == [], (
        f"Expected disabled store server's slug to be dropped, got {resolved!r}"
    )


# ---------------------------------------------------------------------------
# Layer 2 (resilience fix): pre-flight filter against LM Studio's LIVE
# serveable set, independent of the admin-curated catalog. Catches a
# DB-configured/enabled_by_default id that LM Studio itself no longer
# serves (e.g. a removed/renamed MCP server) BEFORE it ever reaches LM
# Studio and dies the turn with "Cannot find plugin handle for plugin:
# mcp/...". Only filters when the live set is KNOWN (non-empty) — see the
# no-op-on-unknown coverage already exercised by every test above (each
# stubs live_serveable_ids() -> set()).
# ---------------------------------------------------------------------------


def test_integrations_none_admin_default_dropped_when_not_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An enabled_by_default id absent from the LIVE set is dropped, even
    though it passed the curated-catalog check (it's still IN the catalog —
    just no longer serveable by LM Studio, e.g. firecrawl replaced by
    crawl4ai but still enabled_by_default in the DB).
    """
    app = _make_app(tmp_path, monkeypatch)

    captured: dict[str, Any] = {}

    with TestClient(app, raise_server_exceptions=False) as client:
        client.post(
            "/api/auth/register",
            data={"username": "live_filter_user_1", "password": "Test1234!"},
        )
        login = client.post(
            "/api/auth/login",
            data={"username": "live_filter_user_1", "password": "Test1234!"},
        )
        assert login.status_code == 200

        from lmchat.routes._dependencies import (
            get_integrations_service_dep,
            get_streaming_service_dep,
        )

        mock_integ_svc = MagicMock()
        mock_integ_svc.list_available = AsyncMock(
            return_value=[
                _make_integration_entry("mcp/firecrawl", enabled_by_default=True),
                _make_integration_entry("mcp/searxng", enabled_by_default=True),
            ]
        )
        # Live set is KNOWN and non-empty, but firecrawl isn't in it — LM
        # Studio's mcp.json no longer has that server configured.
        mock_integ_svc.live_serveable_ids = MagicMock(
            return_value={"mcp/searxng"}
        )

        mock_stream_svc = _make_mock_streaming_svc(captured)

        app.dependency_overrides[get_streaming_service_dep] = lambda: mock_stream_svc
        app.dependency_overrides[get_integrations_service_dep] = (
            lambda: mock_integ_svc
        )

        resp = client.post(
            "/api/chat/stream",
            json=_make_stream_body(include_integrations=False),
        )
        app.dependency_overrides.clear()

    assert resp.status_code == 200

    resolved = captured["payload"].payload.integrations
    assert resolved == ["mcp/searxng"], (
        f"Expected the not-live default dropped, got {resolved!r}"
    )

    _cleanup()


def test_integrations_explicit_list_dropped_when_not_live(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicitly-requested id that's in the curated catalog but absent
    from LM Studio's LIVE set is dropped by the Layer 2 pre-flight filter —
    the catalog filter alone isn't enough to catch this case.
    """
    app = _make_app(tmp_path, monkeypatch)

    captured: dict[str, Any] = {}

    with TestClient(app, raise_server_exceptions=False) as client:
        client.post(
            "/api/auth/register",
            data={"username": "live_filter_user_2", "password": "Test1234!"},
        )
        login = client.post(
            "/api/auth/login",
            data={"username": "live_filter_user_2", "password": "Test1234!"},
        )
        assert login.status_code == 200

        from lmchat.routes._dependencies import (
            get_integrations_service_dep,
            get_streaming_service_dep,
        )

        mock_integ_svc = MagicMock()
        # Curated catalog has BOTH — the catalog filter alone would keep
        # both. Only the live-set filter can catch the stale one here.
        mock_integ_svc.list_available = AsyncMock(
            return_value=[
                _make_integration_entry("mcp/firecrawl", enabled_by_default=False),
                _make_integration_entry("mcp/searxng", enabled_by_default=False),
            ]
        )
        mock_integ_svc.live_serveable_ids = MagicMock(
            return_value={"mcp/searxng"}
        )

        mock_stream_svc = _make_mock_streaming_svc(captured)

        app.dependency_overrides[get_streaming_service_dep] = lambda: mock_stream_svc
        app.dependency_overrides[get_integrations_service_dep] = (
            lambda: mock_integ_svc
        )

        resp = client.post(
            "/api/chat/stream",
            json=_make_stream_body(
                integrations=["mcp/firecrawl", "mcp/searxng"],
                include_integrations=True,
            ),
        )
        app.dependency_overrides.clear()

    assert resp.status_code == 200

    resolved = captured["payload"].payload.integrations
    assert resolved == ["mcp/searxng"], (
        f"Expected the not-live explicit selection dropped, got {resolved!r}"
    )

    _cleanup()


def test_integrations_store_slug_survives_nonempty_live_set_via_union(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Store-installed integration must survive the Layer 2 live-set
    filter even when the live set is KNOWN and non-empty but doesn't itself
    name the store id — the filter is
    ``live_serveable_ids() | store_available``, not ``live_serveable_ids()``
    alone. Store servers run through their own client-side runtime, never
    through LM Studio's mcp.json, so they can never appear in
    ``live_serveable_ids()`` on their own merit.

    This is the exact false-drop the union prevents: without it, ANY
    non-empty live set (e.g. LM Studio genuinely has other native servers
    configured) would silently strip every store-sourced integration —
    not just the removed-firecrawl case, but every 1-click Store install.
    """
    app = _make_app(tmp_path, monkeypatch)

    captured: dict[str, Any] = {}

    with TestClient(app, raise_server_exceptions=False) as client:
        client.post(
            "/api/auth/register",
            data={"username": "union_safety_user", "password": "Test1234!"},
        )
        login = client.post(
            "/api/auth/login",
            data={"username": "union_safety_user", "password": "Test1234!"},
        )
        assert login.status_code == 200

        from lmchat.routes._dependencies import (
            get_integrations_service_dep,
            get_streaming_service_dep,
        )
        from lmchat.services.mcp_server_store import McpServerSafeView

        # Curated catalog only knows about an unrelated LM-Studio-source
        # entry — mcp/store-server is NOT in it, matching
        # test_integrations_explicit_store_slug_not_dropped's setup.
        mock_integ_svc = MagicMock()
        mock_integ_svc.list_available = AsyncMock(
            return_value=[
                _make_integration_entry("mcp/searxng", enabled_by_default=False),
            ]
        )
        # Live set is KNOWN and non-empty (LM Studio genuinely has a native
        # server configured) but does NOT contain the store-sourced id —
        # only the union with store_available should keep it.
        mock_integ_svc.live_serveable_ids = MagicMock(
            return_value={"mcp/native-only"}
        )

        # Store has an installed, enabled+consented server "store-server" —
        # NOT in the curated list AND NOT in the live set.
        mock_store = AsyncMock()
        mock_store.list_all = AsyncMock(
            return_value=[
                McpServerSafeView(
                    id=1,
                    slug="store-server",
                    name="Store Server",
                    transport="stdio",
                    command="store-server",
                    args=None,
                    url=None,
                    secrets_set=[],
                    enabled=True,
                    source="store",
                    trust="unverified",
                    consented=True,
                ),
            ]
        )
        app.state.mcp_server_store = mock_store

        mock_stream_svc = _make_mock_streaming_svc(captured)

        app.dependency_overrides[get_streaming_service_dep] = lambda: mock_stream_svc
        app.dependency_overrides[get_integrations_service_dep] = (
            lambda: mock_integ_svc
        )

        resp = client.post(
            "/api/chat/stream",
            json=_make_stream_body(
                integrations=["mcp/store-server"],
                include_integrations=True,
            ),
        )
        app.dependency_overrides.clear()

    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    resolved = captured["payload"].payload.integrations
    assert resolved == ["mcp/store-server"], (
        f"Expected the store-sourced integration to survive the live-set "
        f"filter via the store-available union, got {resolved!r}"
    )

    _cleanup()
