# SPDX-License-Identifier: Apache-2.0
"""Admin-invalidate tests for ParamsService.

Tests for params-cache invalidation.

Covers:
- invalidate(model_id="A") leaves "B" untouched.
- invalidate(model_id=None) clears all.
- invalidate on a never-seen model is a no-op (no KeyError).
"""
from __future__ import annotations

import pytest

from lmchat.services.params_service import ParamsService


@pytest.fixture()
def svc() -> ParamsService:
    """Return a fresh ParamsService for each test."""
    return ParamsService()


async def test_invalidate_model_a_leaves_model_b_untouched(svc: ParamsService) -> None:
    """invalidate(model_id='A') clears A; B remains intact."""
    await svc.record_rejection(model_id="model-A", param="reasoning")
    await svc.record_rejection(model_id="model-B", param="top_k")

    await svc.invalidate(model_id="model-A")

    assert await svc.get_rejected(model_id="model-A") == frozenset()
    assert await svc.get_rejected(model_id="model-B") == frozenset({"top_k"})


async def test_invalidate_all_clears_multiple_models(svc: ParamsService) -> None:
    """invalidate(model_id=None) clears every entry in the cache."""
    for i in range(5):
        await svc.record_rejection(model_id=f"model-{i}", param=f"param-{i}")

    await svc.invalidate(model_id=None)

    for i in range(5):
        assert await svc.get_rejected(model_id=f"model-{i}") == frozenset()


async def test_invalidate_never_seen_model_is_noop(svc: ParamsService) -> None:
    """invalidate on a model not in the cache raises no exception."""
    # No entry for "ghost-model" — must not raise KeyError.
    await svc.invalidate(model_id="ghost-model")
    # Afterwards it's still absent with an empty set.
    assert await svc.get_rejected(model_id="ghost-model") == frozenset()


async def test_invalidate_then_relearn(svc: ParamsService) -> None:
    """After invalidate, the model can re-learn rejections fresh."""
    await svc.record_rejection(model_id="reload-model", param="top_k")
    await svc.invalidate(model_id="reload-model")

    # Cache is empty.
    assert await svc.get_rejected(model_id="reload-model") == frozenset()

    # Re-learn.
    await svc.record_rejection(model_id="reload-model", param="reasoning")
    result = await svc.get_rejected(model_id="reload-model")
    assert result == frozenset({"reasoning"})
    assert "top_k" not in result


async def test_invalidate_all_then_relearn(svc: ParamsService) -> None:
    """After a full cache clear, all models can be re-learned independently."""
    await svc.record_rejection(model_id="alpha", param="p1")
    await svc.record_rejection(model_id="beta", param="p2")

    await svc.invalidate(model_id=None)

    # Only alpha re-learns.
    await svc.record_rejection(model_id="alpha", param="p3")
    assert await svc.get_rejected(model_id="alpha") == frozenset({"p3"})
    assert await svc.get_rejected(model_id="beta") == frozenset()
