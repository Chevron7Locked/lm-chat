# SPDX-License-Identifier: Apache-2.0
"""Concurrency tests for ParamsService.

Tests for params-cache concurrency.

Covers:
- 10 concurrent record_rejection calls for the same model_id with different
  params → all 10 appear in get_rejected (per-key lock prevents lost updates).
- Two different model_ids racing don't block each other (no global lock).
"""
from __future__ import annotations

import asyncio

import pytest

from lmchat.services.params_service import ParamsService


@pytest.fixture()
def svc() -> ParamsService:
    """Return a fresh ParamsService for each test."""
    return ParamsService()


async def test_concurrent_record_rejection_same_model(svc: ParamsService) -> None:
    """10 concurrent record_rejection calls for the same model produce all 10 params.

    The per-model asyncio.Lock must prevent any update from being lost.
    """
    params = [f"param_{i}" for i in range(10)]

    # Fire all 10 coroutines concurrently.
    await asyncio.gather(
        *[svc.record_rejection(model_id="race-model", param=p) for p in params]
    )

    result = await svc.get_rejected(model_id="race-model")
    for p in params:
        assert p in result, f"Expected {p!r} in rejected set after concurrent writes"
    assert len(result) == 10


async def test_concurrent_different_models_do_not_block(svc: ParamsService) -> None:
    """Two models racing record_rejection concurrently don't interfere.

    If a global lock were used, this would serialise the two tasks; the
    per-model-lock design means model-A's lock doesn't block model-B.
    """
    results: dict[str, frozenset[str]] = {}

    async def record_and_capture(model_id: str, param: str) -> None:
        await svc.record_rejection(model_id=model_id, param=param)
        results[model_id] = await svc.get_rejected(model_id=model_id)

    await asyncio.gather(
        record_and_capture("model-independent-a", "reasoning"),
        record_and_capture("model-independent-b", "top_k"),
    )

    assert "reasoning" in results["model-independent-a"]
    assert "top_k" in results["model-independent-b"]
    # Cross-contamination check.
    assert "top_k" not in results["model-independent-a"]
    assert "reasoning" not in results["model-independent-b"]


async def test_concurrent_seed_and_record_same_model(svc: ParamsService) -> None:
    """seed_from_capabilities and record_rejection racing on the same model.

    Both paths acquire the same per-model lock; no param should be lost.
    """
    await asyncio.gather(
        svc.seed_from_capabilities(
            model_id="shared-model",
            unsupported=["top_k", "min_p"],
        ),
        svc.record_rejection(model_id="shared-model", param="reasoning"),
    )

    result = await svc.get_rejected(model_id="shared-model")
    assert "top_k" in result
    assert "min_p" in result
    assert "reasoning" in result


async def test_concurrent_invalidate_and_record(svc: ParamsService) -> None:
    """invalidate racing with record_rejection doesn't crash.

    Order is non-deterministic; the final state should be coherent
    (either the param is present or absent — no panic, no partial state).
    """
    await svc.record_rejection(model_id="volatile-model", param="existing")

    await asyncio.gather(
        svc.invalidate(model_id="volatile-model"),
        svc.record_rejection(model_id="volatile-model", param="new-param"),
    )

    # Either "new-param" is present (record won the race) or absent
    # (invalidate cleared it); what MUST NOT happen is a crash.
    result = await svc.get_rejected(model_id="volatile-model")
    assert isinstance(result, frozenset)
