# SPDX-License-Identifier: Apache-2.0
"""Tests for ``lmchat.routes._dependencies``.

Covers branches that the auth-route integration tests don't naturally
exercise:

* ``require_admin`` 403 path (no P1 route consumes it, but the dependency
  is public surface for P2+).
* ``require_admin`` 200 path (admin user passes through unchanged).
* ``get_session_store_dep`` loud failure when the lifespan did not run
  AND no override was registered.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import Depends, FastAPI, HTTPException
from fastapi.testclient import TestClient

from lmchat.routes._dependencies import (
    get_session_store_dep,
    require_admin,
    require_user,
)
from lmchat.services.auth_service import User


def _make_user(*, is_admin: bool) -> User:
    """Build a minimal ``User`` for dependency exercise."""
    now = datetime.now(UTC)
    return User(
        id=1,
        username="alice",
        is_admin=is_admin,
        created_at=now,
        updated_at=now,
        password_hash="scrypt$1024$8$1$AAAA$AAAA",
        totp_secret=None,
    )


@pytest.mark.asyncio
async def test_require_admin_returns_user_when_is_admin_true() -> None:
    """An admin user passes through ``require_admin`` unchanged."""
    admin = _make_user(is_admin=True)
    result = await require_admin(user=admin)
    assert result is admin
    assert result.is_admin is True


@pytest.mark.asyncio
async def test_require_admin_raises_403_when_not_admin() -> None:
    """A non-admin user causes ``require_admin`` to raise HTTP 403."""
    plain_user = _make_user(is_admin=False)
    with pytest.raises(HTTPException) as exc_info:
        await require_admin(user=plain_user)
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "admin privileges required"


@pytest.mark.asyncio
async def test_require_user_returns_user_when_authenticated() -> None:
    """A non-None user passes through ``require_user`` unchanged."""
    plain_user = _make_user(is_admin=False)
    result = await require_user(user=plain_user)
    assert result is plain_user


@pytest.mark.asyncio
async def test_require_user_raises_401_when_unauthenticated() -> None:
    """``require_user(user=None)`` raises HTTP 401."""
    with pytest.raises(HTTPException) as exc_info:
        await require_user(user=None)
    assert exc_info.value.status_code == 401
    assert exc_info.value.detail == "authentication required"


def test_get_session_store_dep_raises_when_lifespan_unset() -> None:
    """When ``app.state.session_store`` is unset and no override is
    registered, the dependency raises ``RuntimeError`` rather than
    silently creating a second store.
    """
    app = FastAPI()

    @app.get("/needs-store")
    def _needs_store(
        store: object = Depends(get_session_store_dep),
    ) -> dict[str, str]:
        return {"ok": str(type(store).__name__)}

    # No lifespan, no override — calling the route should propagate the
    # RuntimeError. raise_server_exceptions=True (the default) surfaces it
    # as the original exception type rather than masking it as a 500.
    with TestClient(app, raise_server_exceptions=True) as client:
        with pytest.raises(RuntimeError, match="app.state.session_store is unset"):
            _ = client.get("/needs-store")
