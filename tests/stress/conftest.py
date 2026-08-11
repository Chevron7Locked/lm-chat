# SPDX-License-Identifier: Apache-2.0
"""Shared fixtures for stress tests.

Applies the same low-cost scrypt override used by tests/routes/conftest.py.
On macOS and constrained CI environments, OpenSSL's maxmem limit prevents
running scrypt at the production cost (N=2^17 ≈ 128 MiB) inside test
processes.  We replace hash_password in lmchat.services.auth_service with a
version that runs at N=2^10 (≈ 1 MiB) for the duration of each test.
"""
from __future__ import annotations

import functools
from collections.abc import Iterator

import pytest

_TEST_SCRYPT_N: int = 2**10


@pytest.fixture(autouse=True)
def _patch_scrypt_cost(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Replace hash_password in auth_service with a low-cost version.

    Mirrors tests/routes/conftest.py::_patch_scrypt_cost for the stress tier.
    The stored format is identical real scrypt — just at lower cost.
    """
    import lmchat.services.auth_service as auth_svc
    from lmchat.services.auth_service import _reset_dummy_hash_cache
    from lmchat.utils.hashing import hash_password as _real_hash

    @functools.wraps(_real_hash)
    def _low_cost_hash(password: str, **kwargs: object) -> str:
        kwargs["n"] = _TEST_SCRYPT_N
        return _real_hash(password, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(auth_svc, "hash_password", _low_cost_hash)
    _reset_dummy_hash_cache()
    yield
    _reset_dummy_hash_cache()
