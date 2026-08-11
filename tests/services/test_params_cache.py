# SPDX-License-Identifier: Apache-2.0
"""Contract tests for ParamsService.

This is the contract test that locks the params-cache shape.

Covers:
- get_rejected returns empty frozenset for an unseen model.
- record_rejection adds the param; get_rejected returns it.
- record_rejection on the same param twice is idempotent.
- strip_rejected removes rejected keys; returns a copy (input dict unchanged).
- strip_rejected on a body with no rejected keys returns equal-content copy.
- invalidate(model_id="X") clears only X; other models unaffected.
- invalidate(model_id=None) clears the entire cache.
- seed_from_capabilities populates from the list (forward-compat hook).
- Two different model_ids have independent rejected sets.
- TTL expiry / non-expiry, clear_for_model, WARNING log on strip.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

import lmchat.services.params_service as params_module
from lmchat.services.params_service import ParamsService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def svc() -> ParamsService:
    """Return a fresh ParamsService for each test."""
    return ParamsService()


# ---------------------------------------------------------------------------
# get_rejected
# ---------------------------------------------------------------------------


async def test_get_rejected_empty_for_unseen_model(svc: ParamsService) -> None:
    """get_rejected returns an empty frozenset for a never-seen model_id."""
    result = await svc.get_rejected(model_id="new-model")
    assert result == frozenset()
    assert isinstance(result, frozenset)


# ---------------------------------------------------------------------------
# record_rejection
# ---------------------------------------------------------------------------


async def test_record_rejection_adds_param(svc: ParamsService) -> None:
    """record_rejection adds the param; get_rejected returns it."""
    await svc.record_rejection(model_id="qwen3.6", param="reasoning")
    result = await svc.get_rejected(model_id="qwen3.6")
    assert "reasoning" in result


async def test_record_rejection_idempotent(svc: ParamsService) -> None:
    """record_rejection on the same param twice produces a set of size 1."""
    await svc.record_rejection(model_id="model-a", param="top_k")
    await svc.record_rejection(model_id="model-a", param="top_k")
    result = await svc.get_rejected(model_id="model-a")
    assert result == frozenset({"top_k"})


async def test_record_rejection_multiple_params(svc: ParamsService) -> None:
    """Multiple distinct record_rejection calls accumulate all params."""
    await svc.record_rejection(model_id="model-a", param="top_k")
    await svc.record_rejection(model_id="model-a", param="reasoning")
    result = await svc.get_rejected(model_id="model-a")
    assert result == frozenset({"top_k", "reasoning"})


# ---------------------------------------------------------------------------
# strip_rejected
# ---------------------------------------------------------------------------


async def test_strip_rejected_removes_rejected_keys(svc: ParamsService) -> None:
    """strip_rejected removes rejected keys from the body."""
    await svc.record_rejection(model_id="model-b", param="temperature")
    body = {"temperature": 0.7, "top_p": 0.9, "model": "model-b"}
    stripped = svc.strip_rejected(body, model_id="model-b")
    assert "temperature" not in stripped
    assert stripped["top_p"] == 0.9
    assert stripped["model"] == "model-b"


async def test_strip_rejected_does_not_mutate_input(svc: ParamsService) -> None:
    """strip_rejected returns a NEW dict; the input dict is unchanged."""
    await svc.record_rejection(model_id="model-c", param="reasoning")
    original: dict[str, Any] = {"reasoning": "high", "model": "model-c"}
    stripped = svc.strip_rejected(original, model_id="model-c")
    # Caller's dict must be unchanged.
    assert "reasoning" in original
    # Returned dict must not have the rejected key.
    assert "reasoning" not in stripped
    # Must be a different object.
    assert stripped is not original


async def test_strip_rejected_no_rejections_returns_copy(svc: ParamsService) -> None:
    """strip_rejected with no rejections returns equal-content copy."""
    body = {"model": "clean-model", "temperature": 0.5}
    stripped = svc.strip_rejected(body, model_id="clean-model")
    assert stripped == body
    assert stripped is not body


async def test_strip_rejected_all_keys_removed(svc: ParamsService) -> None:
    """strip_rejected can produce an empty dict when all keys are rejected."""
    await svc.record_rejection(model_id="tiny-model", param="temperature")
    await svc.record_rejection(model_id="tiny-model", param="top_p")
    body: dict[str, Any] = {"temperature": 0.5, "top_p": 0.9}
    stripped = svc.strip_rejected(body, model_id="tiny-model")
    assert stripped == {}


# ---------------------------------------------------------------------------
# invalidate
# ---------------------------------------------------------------------------


async def test_invalidate_single_model_clears_only_that_model(svc: ParamsService) -> None:
    """invalidate(model_id='X') clears X's set; 'Y' is unaffected."""
    await svc.record_rejection(model_id="model-x", param="reasoning")
    await svc.record_rejection(model_id="model-y", param="top_k")

    await svc.invalidate(model_id="model-x")

    assert await svc.get_rejected(model_id="model-x") == frozenset()
    assert "top_k" in await svc.get_rejected(model_id="model-y")


async def test_invalidate_all_clears_entire_cache(svc: ParamsService) -> None:
    """invalidate(model_id=None) clears all model entries."""
    await svc.record_rejection(model_id="model-x", param="reasoning")
    await svc.record_rejection(model_id="model-y", param="top_k")

    await svc.invalidate(model_id=None)

    assert await svc.get_rejected(model_id="model-x") == frozenset()
    assert await svc.get_rejected(model_id="model-y") == frozenset()


# ---------------------------------------------------------------------------
# seed_from_capabilities (forward-compat hook)
# ---------------------------------------------------------------------------


async def test_seed_from_capabilities_populates_set(svc: ParamsService) -> None:
    """seed_from_capabilities populates the rejected set from the list."""
    await svc.seed_from_capabilities(
        model_id="seed-model",
        unsupported=["top_k", "min_p", "reasoning"],
    )
    result = await svc.get_rejected(model_id="seed-model")
    assert result == frozenset({"top_k", "min_p", "reasoning"})


async def test_seed_from_capabilities_idempotent(svc: ParamsService) -> None:
    """seed_from_capabilities called twice with the same list is idempotent."""
    await svc.seed_from_capabilities(model_id="seed-model", unsupported=["top_k"])
    await svc.seed_from_capabilities(model_id="seed-model", unsupported=["top_k"])
    result = await svc.get_rejected(model_id="seed-model")
    assert result == frozenset({"top_k"})


async def test_seed_from_capabilities_empty_list_noop(svc: ParamsService) -> None:
    """seed_from_capabilities with empty list is a no-op."""
    await svc.seed_from_capabilities(model_id="model-z", unsupported=[])
    assert await svc.get_rejected(model_id="model-z") == frozenset()


async def test_seed_from_capabilities_merges_with_existing(svc: ParamsService) -> None:
    """seed_from_capabilities merges new params with previously recorded ones."""
    await svc.record_rejection(model_id="model-m", param="reasoning")
    await svc.seed_from_capabilities(model_id="model-m", unsupported=["top_k"])
    result = await svc.get_rejected(model_id="model-m")
    assert result == frozenset({"reasoning", "top_k"})


# ---------------------------------------------------------------------------
# Independent sets per model_id
# ---------------------------------------------------------------------------


async def test_independent_rejected_sets_per_model(svc: ParamsService) -> None:
    """Two different model_ids have completely independent rejected sets."""
    await svc.record_rejection(model_id="model-alpha", param="reasoning")
    await svc.record_rejection(model_id="model-beta", param="top_k")

    alpha = await svc.get_rejected(model_id="model-alpha")
    beta = await svc.get_rejected(model_id="model-beta")

    assert alpha == frozenset({"reasoning"})
    assert beta == frozenset({"top_k"})
    # No cross-contamination.
    assert "top_k" not in alpha
    assert "reasoning" not in beta


# ---------------------------------------------------------------------------
# TTL expiry (rejections are bounded, not permanent)
# ---------------------------------------------------------------------------


@pytest.fixture()
def fake_clock(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """Replace the module's monotonic clock with a controllable fake."""
    clock = {"now": 1000.0}
    monkeypatch.setattr(params_module, "_monotonic", lambda: clock["now"])
    return clock


async def test_rejected_param_expires_after_ttl(
    svc: ParamsService, fake_clock: dict[str, float]
) -> None:
    """A rejected param drops out of the active set once the TTL elapses."""
    await svc.record_rejection(model_id="ttl-model", param="reasoning")
    fake_clock["now"] += params_module._REJECTED_PARAM_TTL_SEC + 1

    assert await svc.get_rejected(model_id="ttl-model") == frozenset()
    # And the strip path no longer strips — the param re-probes naturally.
    body = {"reasoning": "high", "model": "ttl-model"}
    assert svc.strip_rejected(body, model_id="ttl-model") == body


async def test_rejected_param_survives_half_ttl(
    svc: ParamsService, fake_clock: dict[str, float]
) -> None:
    """A rejected param is still rejected before the TTL elapses."""
    await svc.record_rejection(model_id="ttl-model", param="reasoning")
    fake_clock["now"] += params_module._REJECTED_PARAM_TTL_SEC / 2

    assert "reasoning" in await svc.get_rejected(model_id="ttl-model")
    stripped = svc.strip_rejected(
        {"reasoning": "high", "model": "ttl-model"}, model_id="ttl-model"
    )
    assert "reasoning" not in stripped


async def test_re_rejection_refreshes_ttl(
    svc: ParamsService, fake_clock: dict[str, float]
) -> None:
    """Recording the same rejection again restarts that param's TTL window."""
    await svc.record_rejection(model_id="ttl-model", param="top_k")
    fake_clock["now"] += params_module._REJECTED_PARAM_TTL_SEC / 2
    # Fresh 400 — the rejection is proven still live.
    await svc.record_rejection(model_id="ttl-model", param="top_k")
    # Past the ORIGINAL window but inside the refreshed one.
    fake_clock["now"] += (params_module._REJECTED_PARAM_TTL_SEC / 2) + 1

    assert "top_k" in await svc.get_rejected(model_id="ttl-model")


async def test_ttl_expiry_is_per_param(
    svc: ParamsService, fake_clock: dict[str, float]
) -> None:
    """Only entries older than the TTL expire; younger ones stay rejected."""
    await svc.record_rejection(model_id="ttl-model", param="reasoning")
    fake_clock["now"] += params_module._REJECTED_PARAM_TTL_SEC / 2
    await svc.record_rejection(model_id="ttl-model", param="top_k")
    fake_clock["now"] += (params_module._REJECTED_PARAM_TTL_SEC / 2) + 1

    result = await svc.get_rejected(model_id="ttl-model")
    assert result == frozenset({"top_k"})


# ---------------------------------------------------------------------------
# clear_for_model (probe-completion hook target)
# ---------------------------------------------------------------------------


async def test_clear_for_model_empties_rejected_set(svc: ParamsService) -> None:
    """clear_for_model drops the model's rejected set; others unaffected."""
    await svc.record_rejection(model_id="reloaded-model", param="reasoning")
    await svc.record_rejection(model_id="other-model", param="top_k")

    svc.clear_for_model("reloaded-model")

    assert await svc.get_rejected(model_id="reloaded-model") == frozenset()
    assert "top_k" in await svc.get_rejected(model_id="other-model")


async def test_clear_for_model_unseen_model_noop(svc: ParamsService) -> None:
    """clear_for_model on a never-seen model is a silent no-op."""
    svc.clear_for_model("never-seen")
    assert await svc.get_rejected(model_id="never-seen") == frozenset()


# ---------------------------------------------------------------------------
# WARNING log on strip (capability loss must be observable)
# ---------------------------------------------------------------------------


async def test_strip_logs_warning_with_param_and_model(
    svc: ParamsService, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The strip path emits a WARNING naming the param(s) and model key.

    Asserts against the module logger directly (not structlog capture_logs)
    so the test is independent of whatever global structlog configuration
    earlier tests in the suite may have installed.
    """
    await svc.record_rejection(model_id="strip-model", param="reasoning")

    log_spy = MagicMock()
    monkeypatch.setattr(params_module, "log", log_spy)
    svc.strip_rejected(
        {"reasoning": "high", "model": "strip-model"}, model_id="strip-model"
    )

    warning_calls = [
        c for c in log_spy.warning.call_args_list
        if c.args and c.args[0] == "params_cache.strip_applied"
    ]
    assert len(warning_calls) == 1
    kwargs = warning_calls[0].kwargs
    assert kwargs["model_id"] == "strip-model"
    assert "reasoning" in kwargs["stripped_params"]
    # The strip event goes out at WARNING, not info — no .info() call for it.
    info_events = [
        c for c in log_spy.info.call_args_list
        if c.args and c.args[0] == "params_cache.strip_applied"
    ]
    assert info_events == []
