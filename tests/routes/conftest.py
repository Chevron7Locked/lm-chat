# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures and sys.path setup for route integration tests.

The route integration tests use FastAPI TestClient with a real lifespan.
The lifespan calls ``ensure_schema_ready`` which imports
``migrations.versions.0001_baseline`` — the ``migrations/`` package lives
at the repo root and must be on ``sys.path``.

This conftest adds the repo root once at collection time so that all tests
in ``tests/routes/`` find the migration module regardless of the pytest
working directory.

scrypt cost override
--------------------
On macOS (and in restricted CI environments), OpenSSL's maxmem limit
prevents running scrypt at the production cost (N=2^17 ≈ 128 MiB) inside
test processes.  The ``_patch_scrypt_cost`` autouse fixture replaces
``hash_password`` in ``lmchat.services.auth_service`` with a version that
runs at N=2^10 (≈ 1 MiB) for the duration of each test.

This is NOT a mock — the hash is real scrypt, just at a lower cost.
The stored format (``scrypt$N$r$p$...``) is identical; ``needs_rehash()``
would flag such hashes as below-default in production, triggering a
transparent upgrade on the next login.  This is the correct behaviour.
"""
from __future__ import annotations

import sys
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import pytest

# tests/routes/ → tests/ → repo root
_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


# ---------------------------------------------------------------------------
# Shared helper: TestClient path-segment encoding
# ---------------------------------------------------------------------------


def encode_path_param_for_testclient(value: str) -> str:
    """Percent-encode ``value`` so a ``starlette.testclient.TestClient``
    request delivers it to the route handler at the SAME decode depth a
    real client + real ASGI server (uvicorn) would.

    Why this exists
    ----------------
    A real HTTP client percent-encodes a path segment exactly once
    before putting it on the wire, and a real ASGI server decodes the
    wire path exactly once before handing it to the route — so a
    ``{name:path}``-style handler sees ``value`` back unchanged.

    ``TestClient`` decodes the path an EXTRA time while building its
    ASGI scope: the string you pass to ``test_client.get/post/...`` is
    parsed into ``request.url.path`` (already one level decoded by
    httpx), and TestClient then runs ``unquote()`` on top of THAT
    (``scope["path"] = unquote(request.url.path)``). Net effect: a URL
    string encoded the way a real client would send it arrives at the
    route handler already decoded one level too many when going through
    TestClient — silently mistargeting any value that itself contains a
    percent-encoded-looking sequence (e.g. a folder literally named
    ``"A%2Fb"``: a single-encoded ``"A%252Fb"`` double-decodes to
    ``"A/b"`` under TestClient instead of the real ``"A%2Fb"``).

    To reproduce what production actually sees, a ``TestClient`` caller
    must encode one level DEEPER than a real client would — i.e.
    percent-encode twice. That is exactly what this helper does.

    Use it whenever a TestClient request path contains a segment (most
    commonly a ``:path``-converter segment) whose value needs percent-
    encoding, so the test exercises the value production code actually
    receives rather than an artifact of the test transport. See
    ``tests/routes/test_folders.py::test_rename_folder_does_not_double_decode_path``
    for a worked example, and the matching production-code comments in
    ``lmchat.routes.folders`` (``rename_folder``/``delete_folder``).

    Args:
        value: The raw (unencoded) path-segment value as production
            code would see it after a real client/server round trip.

    Returns:
        The string to embed in a ``TestClient`` request path so the
        route handler receives ``value`` unchanged.
    """
    return quote(quote(value, safe=""), safe="")

# ---------------------------------------------------------------------------
# Low-cost scrypt override for route tests
# ---------------------------------------------------------------------------

_TEST_SCRYPT_N: int = 2**10


@pytest.fixture(autouse=True)
def _patch_scrypt_cost(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Replace ``hash_password`` in auth_service with a low-cost version.

    The production default (N=2^17 ≈ 128 MiB) exceeds the OpenSSL maxmem
    limit in constrained test environments.  We patch the ``hash_password``
    reference inside ``lmchat.services.auth_service`` (the module that
    calls it) to use N=2^10.

    The dummy hash cache is also reset so it is recomputed at the same
    low cost — otherwise a cached full-cost dummy from a previously-run
    test would leave the dummy verify taking near-zero time on unknown-
    username paths.
    """
    import functools

    import lmchat.services.auth_service as auth_svc
    from lmchat.services.auth_service import _reset_dummy_hash_cache
    from lmchat.utils.hashing import hash_password as _real_hash

    # Wrap hash_password to always use n=2^10 regardless of the n kwarg.
    @functools.wraps(_real_hash)
    def _low_cost_hash(password: str, **kwargs: object) -> str:
        kwargs["n"] = _TEST_SCRYPT_N
        return _real_hash(password, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(auth_svc, "hash_password", _low_cost_hash)
    _reset_dummy_hash_cache()
    yield
    _reset_dummy_hash_cache()
