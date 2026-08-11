# SPDX-License-Identifier: Apache-2.0
"""Integration tests for LM Studio settings routes.

Covers:
- ``GET /api/settings/lmstudio`` — 401 unauthenticated; 200 with the
  env-tier values for a logged-in user with no override.
- ``PUT /api/settings/lmstudio`` — writes the per-user override row;
  resolved view reflects the new values; ``api_key_set`` flag flips.
- ``PUT /api/settings/lmstudio`` — empty-string write rejected as 400.
- ``POST /api/settings/lmstudio/test`` — probe shape mocked via the
  service-layer; returns ``ok=True`` + ``model_count``.
- ``PATCH /api/admin/lmstudio/default`` — admin gating: 403 for
  non-admin; 200 for admin; resolved view shows ``server_admin``
  source.
- Raw API key NEVER returned in any GET / PUT response (only
  ``api_key_set: bool``).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.db.schema import metadata
from lmchat.middleware._bucket_store import InMemoryBucketStore
from lmchat.routes._dependencies import get_models_service_dep
from lmchat.services.auth_service import _reset_dummy_hash_cache
from lmchat.services.lm_studio_overrides_service import ProbeResult
from lmchat.utils.hashing import hash_password

_LOW_N: int = 2**10


def _make_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:  # noqa: ANN401
    from lmchat.app import create_app
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    db_url = f"sqlite+aiosqlite:///{tmp_path}/p13g_route_test.db"
    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    monkeypatch.setenv("LM_STUDIO_BASE_URL", "http://env.example:1234")
    monkeypatch.setenv("LM_STUDIO_API_KEY", "env-api-key")
    monkeypatch.setenv("LM_STUDIO_DEFAULT_MODEL", "env-model")

    get_settings.cache_clear()
    engine_mod.dispose_engine()
    _reset_dummy_hash_cache()

    app = create_app()

    stub_models = AsyncMock()
    stub_models.list_loaded = AsyncMock(return_value=[])
    stub_models.refresh = AsyncMock(return_value=None)
    app.dependency_overrides[get_models_service_dep] = lambda: stub_models

    return app


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch: pytest.MonkeyPatch):
    from lmchat.config import get_settings
    from lmchat.db import engine as engine_mod

    monkeypatch.setenv("LM_CHAT_SECRET", "test-secret-32-bytes-of-entropy!!")
    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    yield
    get_settings.cache_clear()
    _reset_dummy_hash_cache()
    engine_mod.dispose_engine()


@pytest.fixture()
def test_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    app = _make_app(tmp_path, monkeypatch)
    with TestClient(app, raise_server_exceptions=True) as client:
        client.app.state.admin_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        client.app.state.stream_buckets = InMemoryBucketStore()  # type: ignore[attr-defined]
        yield client


async def _engine_for(tmp_path: Path) -> AsyncEngine:
    db_url = f"sqlite+aiosqlite:///{tmp_path}/p13g_route_test.db"
    eng = create_async_engine(db_url, pool_pre_ping=True)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


async def _insert_user(
    tmp_path: Path,
    username: str,
    is_admin: bool = False,
) -> int:
    pw_hash = hash_password("test-pw", n=_LOW_N, r=8, p=1)
    eng = await _engine_for(tmp_path)
    try:
        async with eng.begin() as conn:
            await conn.execute(
                text(
                    "INSERT OR IGNORE INTO users (username, password_hash, is_admin) "
                    "VALUES (:u, :pw, :admin)"
                ),
                {"u": username, "pw": pw_hash, "admin": 1 if is_admin else 0},
            )
            row = (
                await conn.execute(
                    text("SELECT id FROM users WHERE username = :u"), {"u": username}
                )
            ).fetchone()
            return int(row[0])  # type: ignore[index]
    finally:
        await eng.dispose()


def _login(client: TestClient, username: str, password: str = "test-pw") -> None:
    resp = client.post(
        "/api/auth/login",
        data={"username": username, "password": password},
    )
    assert resp.status_code == 200, resp.text


# ─── Tests ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_settings_requires_auth(test_client: TestClient) -> None:
    """Unauth GET returns 401."""
    response = test_client.get("/api/settings/lmstudio")
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_get_settings_returns_env_view(
    tmp_path: Path, test_client: TestClient
) -> None:
    """No override → resolved view shows 'unset' sources with empty values.

    Env fallback was removed from resolve().  When no user
    override or admin default exists, every field returns '' with source
    'unset'.  Env values are accessible only via GET /api/settings/lmstudio/env_suggestion.
    """
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    response = test_client.get("/api/settings/lmstudio")
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["base_url"] == ""
    assert data["default_model"] == ""
    assert data["api_key_set"] is False
    assert data["source_base_url"] == "unset"
    assert data["source_api_key"] == "unset"
    assert data["source_default_model"] == "unset"
    # Crucial invariant — the raw key NEVER appears.
    assert "env-api-key" not in response.text
    assert "api_key" not in data  # only api_key_set is exposed


@pytest.mark.asyncio
async def test_put_settings_writes_user_override(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PUT writes the per-user row; resolved view reflects + raw key hidden."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    response = test_client.put(
        "/api/settings/lmstudio",
        json={
            "base_url": "http://user.example:5678",
            "api_key": "user-secret-xyz",
            "default_model": "user-model",
        },
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["base_url"] == "http://user.example:5678"
    assert data["default_model"] == "user-model"
    assert data["api_key_set"] is True
    assert data["source_base_url"] == "user"
    assert data["source_api_key"] == "user"
    assert data["source_default_model"] == "user"
    # And the cleartext key still doesn't echo back.
    assert "user-secret-xyz" not in response.text


@pytest.mark.asyncio
async def test_put_settings_rejects_empty_string(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Empty string write → 400 with helpful detail."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    response = test_client.put(
        "/api/settings/lmstudio",
        json={"base_url": ""},
    )
    # Empty-string now rejected at the Pydantic
    # validator layer (422), with a helpful detail pointing at the
    # `clear` mechanism for resetting.
    assert response.status_code == 422, response.text
    assert "empty" in response.text.lower()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///etc/passwd",
        "/etc/passwd",
        "~/.lmstudio/config.json",  # allow-lmstudio-literal: negative-assertion-test
        "ftp://lan.host:1234",
        "//host:1234",
        "lan.host:1234",
    ],
)
async def test_put_settings_rejects_non_http_url(
    tmp_path: Path, test_client: TestClient, bad_url: str
) -> None:
    """base_url MUST be http(s); reject everything else."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    response = test_client.put(
        "/api/settings/lmstudio",
        json={"base_url": bad_url},
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_test_connection_rejects_non_http_url(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Probe body validates base_url too.

    Probe endpoint is admin-only (SSRF protection). This test uses admin
    to exercise the URL validation path; non-admin access tested by
    test_test_connection_requires_admin."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")
    response = test_client.post(
        "/api/settings/lmstudio/test",
        json={"base_url": "file:///tmp/leak"},
    )
    assert response.status_code == 422, response.text


@pytest.mark.asyncio
async def test_test_connection_requires_admin(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Non-admin POST /api/settings/lmstudio/test → 403.

    The probe endpoint makes an outbound HTTP request to a caller-supplied
    URL, so it MUST require admin privileges to prevent SSRF. Any
    authenticated non-admin user must receive 403."""
    await _insert_user(tmp_path, "bob", is_admin=False)
    _login(test_client, "bob")
    response = test_client.post(
        "/api/settings/lmstudio/test",
        json={"base_url": "http://probe.example"},
    )
    assert response.status_code == 403, (
        f"Expected 403 for non-admin, got {response.status_code}: {response.text}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scheme_url",
    [
        "file:///etc/passwd",
        "gopher://x:1234",
        "dict://x",
        "ftp://x",
    ],
)
async def test_test_connection_rejects_non_http_scheme(
    tmp_path: Path, test_client: TestClient, scheme_url: str
) -> None:
    """Non-HTTP schemes rejected with 422.

    The SSRF validator only allows http:// and https:// schemes.
    Private/loopback/LAN IPs are allowed (LM Studio runs locally)."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")
    response = test_client.post(
        "/api/settings/lmstudio/test",
        json={"base_url": scheme_url},
    )
    assert response.status_code == 422, (
        f"Expected 422 for non-http scheme {scheme_url!r}, "
        f"got {response.status_code}: {response.text}"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "local_url",
    [
        "http://localhost:1234",
        "http://127.0.0.1:8888/api/v1/models",
        "http://192.168.1.1/api/v1/models",
        "http://10.0.0.1:1234/api/v1/models",
    ],
)
async def test_test_connection_allows_local_ips(
    tmp_path: Path, test_client: TestClient, local_url: str
) -> None:
    """Private/loopback/LAN IPs are allowed (LM Studio runs locally).

    The SSRF validator only rejects non-http schemes.
    The probe itself may fail connectivity but the validator allows it."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")
    # The probe will attempt to connect, but the validator should let it through.
    # We expect a 200 with ok=false (connection failed) rather than 422.
    response = test_client.post(
        "/api/settings/lmstudio/test",
        json={"base_url": local_url},
    )
    assert response.status_code == 200, (
        f"Expected 200 for local IP {local_url!r}, "
        f"got {response.status_code}: {response.text}"
    )
    assert not response.json()["ok"], (
        f"Expected probe to fail (connection), but validator should allow {local_url!r}"
    )


@pytest.mark.asyncio
async def test_test_connection_route(
    tmp_path: Path,
    test_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """POST /test calls into the service's probe; returns the shape.

    Admin-gated route (SSRF protection) — caller must be admin to reach
    the probe; non-admin tested by test_test_connection_requires_admin."""
    await _insert_user(tmp_path, "alice", is_admin=True)
    _login(test_client, "alice")

    # Patch the service's probe method to return a deterministic result.
    svc = test_client.app.state.lm_studio_overrides_service  # type: ignore[attr-defined]
    monkeypatch.setattr(
        svc,
        "probe",
        AsyncMock(return_value=ProbeResult(ok=True, model_count=7, error=None)),
    )

    response = test_client.post(
        "/api/settings/lmstudio/test",
        json={"base_url": "http://probe.example", "api_key": "k"},
    )
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["ok"] is True
    assert data["model_count"] == 7
    assert data["error"] is None


@pytest.mark.asyncio
async def test_admin_default_requires_admin(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Non-admin PATCH /api/admin/lmstudio/default → 403."""
    await _insert_user(tmp_path, "bob", is_admin=False)
    _login(test_client, "bob")
    response = test_client.patch(
        "/api/admin/lmstudio/default",
        json={"base_url": "http://admin.example"},
    )
    assert response.status_code == 403


def _mock_probe_ok():
    """Return a context-manager mock that simulates a successful probe (HTTP 200)."""
    probe_resp = MagicMock()
    probe_resp.status_code = 200

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=probe_resp)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)

    mock_cls = MagicMock(return_value=mock_client)
    return mock_cls


@pytest.mark.asyncio
async def test_admin_default_write(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Admin write surfaces as ``server_admin`` source on resolve.

    The route now probes the new URL before writing; we mock httpx.AsyncClient
    at the module level so the probe succeeds without network access.
    """
    await _insert_user(tmp_path, "rootadm", is_admin=True)
    _login(test_client, "rootadm")

    # Also mock rewire_singletons to avoid requiring full app.state singletons
    # in the TestClient context.
    svc = test_client.app.state.lm_studio_overrides_service  # type: ignore[attr-defined]
    with (
        patch("httpx.AsyncClient", new=_mock_probe_ok()),
        patch.object(svc, "rewire_singletons", new=AsyncMock()),
    ):
        response = test_client.patch(
            "/api/admin/lmstudio/default",
            json={
                "base_url": "http://admin.example",
                "api_key": "admin-secret",
                "default_model": "admin-model",
            },
        )
    assert response.status_code == 200, response.text
    data = response.json()
    # Admin made no per-user override; their resolved view shows admin tier.
    assert data["source_base_url"] == "server_admin"
    assert data["source_api_key"] == "server_admin"
    assert data["source_default_model"] == "server_admin"
    # And again — no cleartext key in the response.
    assert "admin-secret" not in response.text


@pytest.mark.asyncio
async def test_admin_default_api_key_only_body_still_probes(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Regression test for the admin PATCH probe-gate bypass.

    Prior to this fix, the gate only fired when ``base_url`` was present in the
    body. A PATCH of ``{"api_key": "wrong-key"}`` skipped the probe
    entirely, wrote the bad key to the DB, and rewired all five
    singletons onto it — defeating the gate's whole purpose (it exists to
    prevent replacing a working client with a broken one) for
    exactly the credential field this remediation is about.

    The FE happens to mask the bypass (always sends base_url alongside
    api_key when the field is pre-filled), but the API surface is the
    contract. After the fix, a 500-from-probe must abort the save.
    """
    # Pre-condition: an admin row exists with a working base_url so the
    # api_key-only PATCH has something to probe against.
    await _insert_user(tmp_path, "rootadm", is_admin=True)
    _login(test_client, "rootadm")
    svc = test_client.app.state.lm_studio_overrides_service  # type: ignore[attr-defined]
    await svc.set_admin_default(
        base_url="http://admin.example",
        api_key="working-key",
        default_model="admin-model",
        clear=None,
    )

    # Mock httpx to return 500 on the probe — simulating the new bad
    # key getting rejected by upstream.
    def _mock_probe_500():
        probe_resp = MagicMock()
        probe_resp.status_code = 500
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=probe_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        return MagicMock(return_value=mock_client)

    # api_key-only body — no base_url, no default_model. Pre-fix this
    # would have skipped the gate and shipped the bad key.
    with (
        patch("httpx.AsyncClient", new=_mock_probe_500()),
        patch.object(svc, "rewire_singletons", new=AsyncMock()) as mock_rewire,
    ):
        response = test_client.patch(
            "/api/admin/lmstudio/default",
            json={"api_key": "wrong-key"},
        )

    # The probe must reject → 400 Save aborted.
    assert response.status_code == 400, response.text
    assert "probe" in response.text.lower()
    assert "save aborted" in response.text.lower()
    # And the rewire must NOT have happened — the gate's whole point.
    mock_rewire.assert_not_called()


# ─── key_pruned / auth_failed banner accuracy ───────────────────────────────


@pytest.mark.asyncio
async def test_get_settings_suppresses_key_pruned_when_models_loaded(
    tmp_path: Path, test_client: TestClient
) -> None:
    """The 'key cleared by secret rotation — models won't load' banner is
    suppressed when models ARE loading via another tier (env/user override).
    The admin hit a stuck banner while inference worked fine."""
    from types import SimpleNamespace  # noqa: PLC0415
    from unittest.mock import AsyncMock  # noqa: PLC0415

    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")
    test_client.app.state.lm_studio_key_pruned = True  # type: ignore[attr-defined]
    test_client.app.state.models_service = SimpleNamespace(  # type: ignore[attr-defined]
        list_loaded=AsyncMock(
            return_value=[SimpleNamespace(loaded_instance_ids=["inst-1"])]
        )
    )
    resp = test_client.get("/api/settings/lmstudio")
    assert resp.status_code == 200, resp.text
    assert resp.json()["key_pruned"] is False


@pytest.mark.asyncio
async def test_get_settings_keeps_key_pruned_when_nothing_loaded(
    tmp_path: Path, test_client: TestClient
) -> None:
    """When nothing is loaded, key_pruned stays True — the banner is accurate."""
    from types import SimpleNamespace  # noqa: PLC0415
    from unittest.mock import AsyncMock  # noqa: PLC0415

    await _insert_user(tmp_path, "bob")
    _login(test_client, "bob")
    test_client.app.state.lm_studio_key_pruned = True  # type: ignore[attr-defined]
    test_client.app.state.models_service = SimpleNamespace(  # type: ignore[attr-defined]
        list_loaded=AsyncMock(
            return_value=[SimpleNamespace(loaded_instance_ids=[])]
        )
    )
    resp = test_client.get("/api/settings/lmstudio")
    assert resp.status_code == 200, resp.text
    assert resp.json()["key_pruned"] is True


# ─── Preferred embedding model GET/PATCH ───────────────────────────────────


@pytest.mark.asyncio
async def test_get_settings_surfaces_preferred_embedding_model(
    tmp_path: Path, test_client: TestClient
) -> None:
    """GET /api/settings/lmstudio surfaces preferred_embedding_model_id + loaded embedders.

    The GET response includes the current preferred
    embedder value and the list of loaded embedding models so the FE can
    render the selector without an extra round-trip.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")

    # Seed a preferred embedding model in the DB.
    svc = test_client.app.state.lm_studio_overrides_service  # type: ignore[attr-defined]
    await svc.set_preferred_embedding_model("nomic-embed-v1.5")

    # Mock models_service to return one loaded embedder.
    test_client.app.state.models_service = SimpleNamespace(  # type: ignore[attr-defined]
        list_loaded=AsyncMock(
            return_value=[
                SimpleNamespace(
                    key="nomic-embed-v1.5",
                    type="embedding",
                    loaded_instance_ids=["nomic-embed-v1.5@q8_0"],
                )
            ]
        )
    )

    resp = test_client.get("/api/settings/lmstudio")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["preferred_embedding_model_id"] == "nomic-embed-v1.5"
    # Each entry now carries an `active` marker (the resolver's pick). The
    # preferred + only-loaded model resolves active here.
    assert data["loaded_embedding_models"] == [
        {"key": "nomic-embed-v1.5", "active": True}
    ]


@pytest.mark.asyncio
async def test_patch_embedding_model_persists_loaded_model(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH /api/settings/lmstudio/embedding-model with a loaded model persists it.

    An admin can pin the embedder to any currently-loaded
    embedding model; the preference is written to the DB.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    await _insert_user(tmp_path, "rootadm", is_admin=True)
    _login(test_client, "rootadm")

    # Make "nomic-embed-v1.5" appear as a loaded embedder.
    test_client.app.state.models_service = SimpleNamespace(  # type: ignore[attr-defined]
        list_loaded=AsyncMock(
            return_value=[
                SimpleNamespace(
                    key="nomic-embed-v1.5",
                    type="embedding",
                    loaded_instance_ids=["nomic-embed-v1.5@q8_0"],
                )
            ]
        )
    )

    resp = test_client.patch(
        "/api/settings/lmstudio/embedding-model",
        json={"embedding_model_id": "nomic-embed-v1.5"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["preferred_embedding_model_id"] == "nomic-embed-v1.5"
    # The rebuilt embedder list marks the just-pinned model active so the FE
    # can render "· active" without re-deriving the resolver's pick.
    assert data["loaded_embedding_models"] == [
        {"key": "nomic-embed-v1.5", "active": True}
    ]

    # Verify it actually persisted to the DB.
    svc = test_client.app.state.lm_studio_overrides_service  # type: ignore[attr-defined]
    stored = await svc.fetch_preferred_embedding_model()
    assert stored == "nomic-embed-v1.5"


@pytest.mark.asyncio
async def test_patch_embedding_model_rejects_unloaded_model(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH /api/settings/lmstudio/embedding-model with an unloaded model → 400.

    The route validates that the requested model is among
    the currently-loaded embedders; if not, it returns 400 with a clear
    message rather than persisting a broken preference.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    await _insert_user(tmp_path, "rootadm", is_admin=True)
    _login(test_client, "rootadm")

    # Nothing is loaded.
    test_client.app.state.models_service = SimpleNamespace(  # type: ignore[attr-defined]
        list_loaded=AsyncMock(return_value=[])
    )

    resp = test_client.patch(
        "/api/settings/lmstudio/embedding-model",
        json={"embedding_model_id": "bge-m3"},
    )
    assert resp.status_code == 400, resp.text
    assert "not currently loaded" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_patch_embedding_model_null_clears_preference(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH /api/settings/lmstudio/embedding-model with null clears the preference.

    Setting embedding_model_id to null returns selection to
    the deterministic auto-pick (lexicographic sort over loaded embedders).
    """
    from types import SimpleNamespace  # noqa: PLC0415

    await _insert_user(tmp_path, "rootadm", is_admin=True)
    _login(test_client, "rootadm")

    # First, persist a preference.
    svc = test_client.app.state.lm_studio_overrides_service  # type: ignore[attr-defined]
    await svc.set_preferred_embedding_model("nomic-embed-v1.5")

    # models_service irrelevant for the null/clear path (no validation needed).
    test_client.app.state.models_service = SimpleNamespace(  # type: ignore[attr-defined]
        list_loaded=AsyncMock(return_value=[])
    )

    resp = test_client.patch(
        "/api/settings/lmstudio/embedding-model",
        json={"embedding_model_id": None},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["preferred_embedding_model_id"] is None

    # Verify the column was cleared.
    stored = await svc.fetch_preferred_embedding_model()
    assert stored is None


@pytest.mark.asyncio
async def test_patch_embedding_model_requires_admin(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH /api/settings/lmstudio/embedding-model requires admin — 403 for non-admin."""
    await _insert_user(tmp_path, "bob", is_admin=False)
    _login(test_client, "bob")
    resp = test_client.patch(
        "/api/settings/lmstudio/embedding-model",
        json={"embedding_model_id": None},
    )
    assert resp.status_code == 403, resp.text


# ─── Background-tasks model selector ───────────────────────────────────────


@pytest.mark.asyncio
async def test_get_settings_surfaces_background_model(
    tmp_path: Path, test_client: TestClient
) -> None:
    """GET /api/settings/lmstudio surfaces preferred_background_model_id + loaded LLMs."""
    from types import SimpleNamespace  # noqa: PLC0415

    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")

    svc = test_client.app.state.lm_studio_overrides_service  # type: ignore[attr-defined]
    await svc.set_preferred_background_model("small-llm-3b")

    # One loaded LLM (the pinned one) + an embedder that must be filtered out.
    test_client.app.state.models_service = SimpleNamespace(  # type: ignore[attr-defined]
        list_loaded=AsyncMock(
            return_value=[
                SimpleNamespace(
                    key="small-llm-3b",
                    type="llm",
                    loaded_instance_ids=["small-llm-3b@q4"],
                ),
                SimpleNamespace(
                    key="nomic-embed-v1.5",
                    type="embedding",
                    loaded_instance_ids=["nomic-embed-v1.5@q8_0"],
                ),
            ]
        )
    )

    resp = test_client.get("/api/settings/lmstudio")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["preferred_background_model_id"] == "small-llm-3b"
    # Only LLMs appear (the embedder is excluded).
    assert data["loaded_background_models"] == [{"key": "small-llm-3b"}]


@pytest.mark.asyncio
async def test_patch_background_model_persists_loaded_model(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH /api/settings/lmstudio/background-model with a loaded LLM persists it."""
    from types import SimpleNamespace  # noqa: PLC0415

    await _insert_user(tmp_path, "rootadm", is_admin=True)
    _login(test_client, "rootadm")

    test_client.app.state.models_service = SimpleNamespace(  # type: ignore[attr-defined]
        list_loaded=AsyncMock(
            return_value=[
                SimpleNamespace(
                    key="small-llm-3b",
                    type="llm",
                    loaded_instance_ids=["small-llm-3b@q4"],
                )
            ]
        )
    )

    resp = test_client.patch(
        "/api/settings/lmstudio/background-model",
        json={"background_model_id": "small-llm-3b"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["preferred_background_model_id"] == "small-llm-3b"
    assert data["loaded_background_models"] == [{"key": "small-llm-3b"}]

    svc = test_client.app.state.lm_studio_overrides_service  # type: ignore[attr-defined]
    stored = await svc.fetch_preferred_background_model()
    assert stored == "small-llm-3b"


@pytest.mark.asyncio
async def test_patch_background_model_rejects_unloaded_model(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH /api/settings/lmstudio/background-model with an unloaded model → 400."""
    from types import SimpleNamespace  # noqa: PLC0415

    await _insert_user(tmp_path, "rootadm", is_admin=True)
    _login(test_client, "rootadm")

    test_client.app.state.models_service = SimpleNamespace(  # type: ignore[attr-defined]
        list_loaded=AsyncMock(return_value=[])
    )

    resp = test_client.patch(
        "/api/settings/lmstudio/background-model",
        json={"background_model_id": "small-llm-3b"},
    )
    assert resp.status_code == 400, resp.text
    assert "not currently loaded" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_patch_background_model_null_clears_preference(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH /api/settings/lmstudio/background-model with null clears the preference."""
    from types import SimpleNamespace  # noqa: PLC0415

    await _insert_user(tmp_path, "rootadm", is_admin=True)
    _login(test_client, "rootadm")

    svc = test_client.app.state.lm_studio_overrides_service  # type: ignore[attr-defined]
    await svc.set_preferred_background_model("small-llm-3b")

    test_client.app.state.models_service = SimpleNamespace(  # type: ignore[attr-defined]
        list_loaded=AsyncMock(return_value=[])
    )

    resp = test_client.patch(
        "/api/settings/lmstudio/background-model",
        json={"background_model_id": None},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["preferred_background_model_id"] is None

    stored = await svc.fetch_preferred_background_model()
    assert stored is None


@pytest.mark.asyncio
async def test_patch_background_model_requires_admin(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH /api/settings/lmstudio/background-model requires admin — 403 for non-admin."""
    await _insert_user(tmp_path, "bob", is_admin=False)
    _login(test_client, "bob")
    resp = test_client.patch(
        "/api/settings/lmstudio/background-model",
        json={"background_model_id": None},
    )
    assert resp.status_code == 403, resp.text


async def _seed_corpus_embedding(tmp_path: Path, *, dim: int) -> None:
    """Seed one ``message_embeddings`` row of dimension *dim* (full FK chain).

    The corpus dimension is read from the actual stored vector byte-length, so a
    single row of the right dimension is enough to make the SET-time guard fire.
    """
    import struct  # noqa: PLC0415

    blob = struct.pack(f"<{dim}f", *([0.1] * dim))
    eng = await _engine_for(tmp_path)
    try:
        async with eng.begin() as conn:
            await conn.execute(
                text(
                    "INSERT OR IGNORE INTO users (id, username, password_hash) "
                    "VALUES (1, 'corpususer', 'scrypt$x')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO chats (id, user_id, title) VALUES (1, 1, 'c')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO messages (id, chat_id, role, content) "
                    "VALUES (1, 1, 'user', 'hello')"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO message_embeddings "
                    "(message_id, embedding_model_id, embedding, text_hash) "
                    "VALUES (1, :mid, :emb, 'h')"
                ),
                {"mid": "text-embedding-nomic-embed-text-v1.5", "emb": blob},
            )
    finally:
        await eng.dispose()


@pytest.mark.asyncio
async def test_patch_embedding_model_rejects_dimension_mismatch_on_nonempty_corpus(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Pinning a DIFFERENT-dimension embedder on a non-empty
    corpus is rejected with a clear "re-index to change" message.

    The corpus is 768-dim (nomic). bge-m3 probes as 1024-dim. Switching without
    a re-index would corrupt recall (cross-dimension cosine), so the PATCH must
    return 400 and NOT persist the new preference.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    await _insert_user(tmp_path, "rootadm", is_admin=True)
    _login(test_client, "rootadm")

    # Corpus is 768-dim.
    await _seed_corpus_embedding(tmp_path, dim=768)

    # bge-m3 is loaded (so the loaded-check passes) and probes as 1024-dim.
    test_client.app.state.models_service = SimpleNamespace(  # type: ignore[attr-defined]
        list_loaded=AsyncMock(
            return_value=[
                SimpleNamespace(
                    key="text-embedding-bge-m3",
                    type="embedding",
                    loaded_instance_ids=["text-embedding-bge-m3@q8_0"],
                )
            ]
        )
    )
    # Probe returns a 1024-dim vector → mismatch with the 768-dim corpus.
    test_client.app.state.embedding_client = SimpleNamespace(  # type: ignore[attr-defined]
        embed_one=AsyncMock(return_value=[0.1] * 1024)
    )

    resp = test_client.patch(
        "/api/settings/lmstudio/embedding-model",
        json={"embedding_model_id": "text-embedding-bge-m3"},
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "1024-dim" in detail and "768-dim" in detail
    assert "re-index" in detail.lower()

    # The preference must NOT have been persisted.
    svc = test_client.app.state.lm_studio_overrides_service  # type: ignore[attr-defined]
    assert await svc.fetch_preferred_embedding_model() != "text-embedding-bge-m3"


@pytest.mark.asyncio
async def test_patch_embedding_model_allows_same_dimension_on_nonempty_corpus(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Pinning a SAME-dimension embedder on a non-empty corpus
    is allowed (and pinning on an EMPTY corpus is always allowed).

    A 768-dim model on a 768-dim corpus is dimension-compatible, so the switch
    is accepted and persisted — no re-index required.
    """
    from types import SimpleNamespace  # noqa: PLC0415

    await _insert_user(tmp_path, "rootadm", is_admin=True)
    _login(test_client, "rootadm")

    # Corpus is 768-dim.
    await _seed_corpus_embedding(tmp_path, dim=768)

    # A different nomic-family model that is ALSO 768-dim.
    test_client.app.state.models_service = SimpleNamespace(  # type: ignore[attr-defined]
        list_loaded=AsyncMock(
            return_value=[
                SimpleNamespace(
                    key="text-embedding-nomic-embed-text-v1.5",
                    type="embedding",
                    loaded_instance_ids=["text-embedding-nomic-embed-text-v1.5@q8_0"],
                )
            ]
        )
    )
    # Probe returns a 768-dim vector → matches the corpus.
    test_client.app.state.embedding_client = SimpleNamespace(  # type: ignore[attr-defined]
        embed_one=AsyncMock(return_value=[0.1] * 768)
    )

    resp = test_client.patch(
        "/api/settings/lmstudio/embedding-model",
        json={"embedding_model_id": "text-embedding-nomic-embed-text-v1.5"},
    )
    assert resp.status_code == 200, resp.text
    assert (
        resp.json()["preferred_embedding_model_id"]
        == "text-embedding-nomic-embed-text-v1.5"
    )

    # Persisted.
    svc = test_client.app.state.lm_studio_overrides_service  # type: ignore[attr-defined]
    stored = await svc.fetch_preferred_embedding_model()
    assert stored == "text-embedding-nomic-embed-text-v1.5"


# ─── Probe gate only fires on ACTUAL changes ───────────────────────────────


@pytest.mark.asyncio
async def test_admin_patch_model_only_skips_probe_on_unreachable_endpoint(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Model-only PATCH must succeed even when the LM Studio
    endpoint returns 401 / is unreachable.

    Before the fix, the probe fired whenever ``base_url`` OR ``api_key`` was
    in the body — the Settings form always pre-fills ``base_url`` unchanged.
    A user trying to fix a stale ``default_model`` had their save aborted with
    "Probe … failed: HTTP 401. Save aborted" even though nothing
    LM-Studio-facing changed.

    After the fix, the probe only fires when the submitted value actually
    differs from the stored resolved admin value.  A model-only save (or a
    save with the SAME base_url / empty api_key) must not probe and must write
    the new default_model.
    """
    await _insert_user(tmp_path, "rootadm", is_admin=True)
    _login(test_client, "rootadm")
    svc = test_client.app.state.lm_studio_overrides_service  # type: ignore[attr-defined]

    # Seed an existing admin row with a working base_url + api_key.
    await svc.set_admin_default(
        base_url="http://localhost:1234",
        api_key="the-key",
        default_model="old-model",
        clear=None,
    )

    # Mock httpx to always return 401 — if the probe fires, the save aborts.
    def _mock_probe_401():
        probe_resp = MagicMock()
        probe_resp.status_code = 401
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=probe_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        return MagicMock(return_value=mock_client)

    with (
        patch("httpx.AsyncClient", new=_mock_probe_401()),
        patch.object(svc, "rewire_singletons", new=AsyncMock()),
    ):
        # Send the SAME base_url (unchanged), no api_key — only default_model
        # changes.  This is exactly what the Settings form does.
        response = test_client.patch(
            "/api/admin/lmstudio/default",
            json={
                "base_url": "http://localhost:1234",
                "default_model": "new-model",
            },
        )

    # Must succeed — no probe should have fired.
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["default_model"] == "new-model"
    assert data["source_default_model"] == "server_admin"


@pytest.mark.asyncio
async def test_admin_patch_changed_base_url_to_broken_endpoint_is_rejected(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Guard preserved: a DIFFERENT base_url that
    returns 401 / is unreachable must still be rejected with HTTP 400.

    The fix skips the probe only for UNCHANGED values.  Genuinely new URLs
    must still be probed — this test verifies the guard is not accidentally
    disabled for real changes.
    """
    await _insert_user(tmp_path, "rootadm", is_admin=True)
    _login(test_client, "rootadm")
    svc = test_client.app.state.lm_studio_overrides_service  # type: ignore[attr-defined]

    # Seed an existing admin row.
    await svc.set_admin_default(
        base_url="http://localhost:1234",
        api_key=None,
        default_model="old-model",
        clear=None,
    )

    # New, different URL that the probe will reject.
    def _mock_probe_401():
        probe_resp = MagicMock()
        probe_resp.status_code = 401
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=probe_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        return MagicMock(return_value=mock_client)

    with (
        patch("httpx.AsyncClient", new=_mock_probe_401()),
        patch.object(svc, "rewire_singletons", new=AsyncMock()),
    ):
        response = test_client.patch(
            "/api/admin/lmstudio/default",
            json={"base_url": "http://NEW-HOST:5678"},
        )

    assert response.status_code == 400, response.text
    assert "save aborted" in response.text.lower()


@pytest.mark.asyncio
async def test_admin_patch_new_api_key_still_probed(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Guard preserved: a brand-new api_key that
    makes the probe return 401 must still be rejected with HTTP 400.

    A different (non-empty) api_key is a real credential change and must be
    probed — this test verifies the fix does not accidentally skip it.
    """
    await _insert_user(tmp_path, "rootadm", is_admin=True)
    _login(test_client, "rootadm")
    svc = test_client.app.state.lm_studio_overrides_service  # type: ignore[attr-defined]

    await svc.set_admin_default(
        base_url="http://localhost:1234",
        api_key="old-key",
        default_model="old-model",
        clear=None,
    )

    def _mock_probe_401():
        probe_resp = MagicMock()
        probe_resp.status_code = 401
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=probe_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        return MagicMock(return_value=mock_client)

    with (
        patch("httpx.AsyncClient", new=_mock_probe_401()),
        patch.object(svc, "rewire_singletons", new=AsyncMock()),
    ):
        response = test_client.patch(
            "/api/admin/lmstudio/default",
            json={"api_key": "brand-new-wrong-key"},
        )

    # The new key changes the credential → probe fires → 401 → 400.
    assert response.status_code == 400, response.text
    assert "save aborted" in response.text.lower()


# ─── Endpoint-mode toggle ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_settings_defaults_endpoint_mode_to_native(
    tmp_path: Path, test_client: TestClient
) -> None:
    """GET /api/settings/lmstudio reports native when nothing has been saved."""
    await _insert_user(tmp_path, "alice")
    _login(test_client, "alice")

    resp = test_client.get("/api/settings/lmstudio")
    assert resp.status_code == 200, resp.text
    assert resp.json()["lm_studio_endpoint_mode"] == "native"


@pytest.mark.asyncio
async def test_patch_endpoint_mode_persists_openai_compat(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH /api/settings/lmstudio/endpoint-mode persists openai_compat."""
    await _insert_user(tmp_path, "rootadm", is_admin=True)
    _login(test_client, "rootadm")

    resp = test_client.patch(
        "/api/settings/lmstudio/endpoint-mode",
        json={"endpoint_mode": "openai_compat"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"endpoint_mode": "openai_compat"}

    svc = test_client.app.state.lm_studio_overrides_service  # type: ignore[attr-defined]
    assert await svc.fetch_endpoint_mode() == "openai_compat"

    # GET now reflects the saved value.
    resp = test_client.get("/api/settings/lmstudio")
    assert resp.json()["lm_studio_endpoint_mode"] == "openai_compat"


@pytest.mark.asyncio
async def test_patch_endpoint_mode_back_to_native(
    tmp_path: Path, test_client: TestClient
) -> None:
    """Switching back to native persists and reads back correctly."""
    await _insert_user(tmp_path, "rootadm", is_admin=True)
    _login(test_client, "rootadm")

    svc = test_client.app.state.lm_studio_overrides_service  # type: ignore[attr-defined]
    await svc.set_endpoint_mode("openai_compat")

    resp = test_client.patch(
        "/api/settings/lmstudio/endpoint-mode",
        json={"endpoint_mode": "native"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"endpoint_mode": "native"}
    assert await svc.fetch_endpoint_mode() == "native"


@pytest.mark.asyncio
async def test_patch_endpoint_mode_rejects_unknown_value(
    tmp_path: Path, test_client: TestClient
) -> None:
    """An unrecognized endpoint_mode value is rejected as 422 (Pydantic Literal)."""
    await _insert_user(tmp_path, "rootadm", is_admin=True)
    _login(test_client, "rootadm")

    resp = test_client.patch(
        "/api/settings/lmstudio/endpoint-mode",
        json={"endpoint_mode": "bogus"},
    )
    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_patch_endpoint_mode_requires_admin(
    tmp_path: Path, test_client: TestClient
) -> None:
    """PATCH /api/settings/lmstudio/endpoint-mode requires admin — 403 for non-admin."""
    await _insert_user(tmp_path, "bob", is_admin=False)
    _login(test_client, "bob")

    resp = test_client.patch(
        "/api/settings/lmstudio/endpoint-mode",
        json={"endpoint_mode": "openai_compat"},
    )
    assert resp.status_code == 403, resp.text
