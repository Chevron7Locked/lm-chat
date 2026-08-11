# SPDX-License-Identifier: Apache-2.0
"""Mid-conversation reactive-learn sequence tests for ParamsService.

Tests for params-cache mid-conversation invalidation.

Simulates the reactive-learn-and-retry pattern:
  1. get_rejected (empty)
  2. record_rejection("temperature")
  3. strip_rejected({temperature: 0.7, top_p: 0.9}) → {top_p: 0.9}
  4. record_rejection("top_p")
  5. strip_rejected({...}) → {}

Also covers the full happy path: a param that is NOT rejected passes through.
"""
from __future__ import annotations

import pytest

from lmchat.services.params_service import ParamsService

_MODEL = "mid-conv-model"


@pytest.fixture()
def svc() -> ParamsService:
    """Return a fresh ParamsService for each test."""
    return ParamsService()


async def test_reactive_learn_sequence(svc: ParamsService) -> None:
    """Full mid-conversation reactive-learn sequence.

    Turn 1: no rejections yet — body passes through intact.
    After 400 on 'temperature': strip drops it; body retried with top_p only.
    After 400 on 'top_p' as well: body is empty.
    """
    # Turn 1 — no rejections recorded.
    body1 = {"model": _MODEL, "temperature": 0.7, "top_p": 0.9}
    stripped1 = svc.strip_rejected(body1, model_id=_MODEL)
    assert stripped1 == body1
    assert stripped1 is not body1  # always a copy

    # --- upstream returns HTTP 400: error.param = "temperature" ---
    await svc.record_rejection(model_id=_MODEL, param="temperature")

    # Retry body has temperature removed.
    stripped2 = svc.strip_rejected(body1, model_id=_MODEL)
    assert "temperature" not in stripped2
    assert "top_p" in stripped2
    assert stripped2["top_p"] == 0.9

    # --- upstream returns HTTP 400 again: error.param = "top_p" ---
    await svc.record_rejection(model_id=_MODEL, param="top_p")

    stripped3 = svc.strip_rejected(body1, model_id=_MODEL)
    # Both rejected now — only 'model' key survives.
    assert "temperature" not in stripped3
    assert "top_p" not in stripped3
    assert stripped3.get("model") == _MODEL


async def test_non_rejected_param_passes_through(svc: ParamsService) -> None:
    """A param not in the rejected set is preserved in the stripped body."""
    await svc.record_rejection(model_id=_MODEL, param="reasoning")
    body = {"model": _MODEL, "temperature": 0.5, "reasoning": "high"}
    stripped = svc.strip_rejected(body, model_id=_MODEL)
    assert "reasoning" not in stripped
    assert stripped.get("temperature") == 0.5


async def test_reactive_learn_does_not_affect_other_models(svc: ParamsService) -> None:
    """Rejections for model A do not contaminate model B's requests."""
    await svc.record_rejection(model_id=_MODEL, param="top_k")

    other_body = {"model": "other-model", "top_k": 40, "temperature": 0.8}
    stripped = svc.strip_rejected(other_body, model_id="other-model")
    # 'top_k' is rejected for _MODEL only, not for 'other-model'.
    assert "top_k" in stripped


async def test_strip_returns_new_dict_after_each_rejection(svc: ParamsService) -> None:
    """Each call to strip_rejected returns a distinct dict object."""
    body = {"model": _MODEL, "temperature": 0.5}
    r1 = svc.strip_rejected(body, model_id=_MODEL)
    r2 = svc.strip_rejected(body, model_id=_MODEL)
    assert r1 is not r2
    assert r1 is not body
    assert r2 is not body
