# SPDX-License-Identifier: Apache-2.0
"""Tests for the self-consistency majority-vote convergence rule.

Replaces the prior ``max(pairwise_cosines) > θ`` permissive trigger with
the majority rule: a draft set is converged iff the count of unique
pairs whose cosine > θ exceeds ``N*(N-1)/4`` (a strict majority of the
``N*(N-1)/2`` unique pairs).

Covered scenarios:
- N=3, 2-of-3 pairs above θ (1 below) → ABOVE majority threshold of 1.5 → converged.
- N=3, 1-of-3 pairs above θ → NOT converged.
- N=5, 7-of-10 pairs above θ → converged (threshold = 5).
- N=5, 5-of-10 pairs above θ → NOT converged (need strict >, not >=).
"""
from __future__ import annotations

import math
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import structlog.testing

from lmchat.lmstudio.types import CanonicalChatRequest, CanonicalEvent
from lmchat.services.quality_modes import QualityModeService


def _async_text_stream(txt: str) -> AsyncIterator[CanonicalEvent]:
    async def _gen() -> AsyncIterator[CanonicalEvent]:
        yield CanonicalEvent(type="message.delta", content=txt)
    return _gen()


def _models_service_with_embedding() -> AsyncMock:
    from lmchat.services.models_service import Capabilities, ModelInfo
    svc = AsyncMock()
    svc.list_loaded = AsyncMock(
        return_value=[
            ModelInfo(
                key="m",
                type="embedding",
                capabilities=Capabilities(vision=False, trained_for_tool_use=False),
                # Genuinely loaded — resolver filters on loaded_instance_ids.
                loaded_instance_ids=["m@q8_0"],
            )
        ]
    )
    return svc


def _adapter_with_drafts(drafts: list[str]) -> MagicMock:
    count = 0

    def _side(req: CanonicalChatRequest, **kw: object) -> AsyncIterator[CanonicalEvent]:
        nonlocal count
        text = drafts[count % len(drafts)]
        count += 1
        return _async_text_stream(text)

    adapter = MagicMock()
    adapter.stream_chat = MagicMock(side_effect=_side)
    return adapter


def _angle(theta_deg: float) -> list[float]:
    """Unit 2D vector at *theta_deg* degrees."""
    rad = math.pi / 180.0
    return [math.cos(theta_deg * rad), math.sin(theta_deg * rad)]


async def _run(
    threshold: float, vectors: list[list[float]], drafts: list[str]
) -> tuple[str, list[dict[str, object]]]:
    """Drive self_consistency end-to-end and return (chosen, sc_result_logs)."""
    adapter = _adapter_with_drafts(drafts)

    embedding_client = AsyncMock()
    embedding_client.embed_batch = AsyncMock(return_value=vectors)

    svc = QualityModeService(
        adapter=adapter,
        embedding_client=embedding_client,
        models_service=_models_service_with_embedding(),
    )

    with structlog.testing.capture_logs() as captured, patch(
        "lmchat.services.quality_modes.get_settings"
    ) as mock_settings:
        mock_settings.return_value = MagicMock(
            lm_chat_sc_threshold=threshold,
            lm_studio_default_model="test-model",
        )
        result = await svc.self_consistency(
            prompt="anything", n_drafts=len(drafts), model_id="test-model"
        )

    sc_results: list[dict[str, object]] = [
        dict(e) for e in captured if e.get("event", "").endswith("sc.result")
    ]
    return result, sc_results


# ---------------------------------------------------------------------------
# N=3 cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_n3_two_pairs_above_threshold_converged() -> None:
    """3 drafts; 2 of 3 unique pairs > θ; 1 pair < θ → above majority of 1.5 → converged."""
    # v0 = 0°, v1 = 5° (close), v2 = 8° (close) — all three pairs are close.
    # Want 2-of-3 above. Tune: v0=0°, v1=5°, v2=40°.
    # cos(0,5) ≈ 0.9962 > 0.85   ✓
    # cos(0,40) ≈ 0.7660 < 0.85  ✗
    # cos(5,40) ≈ 0.7880 < 0.85  ✗  (only 1 above)
    # Try: v0=0°, v1=5°, v2=20° → cos(0,5)=0.9962, cos(0,20)=0.9397, cos(5,20)=0.9659 — all 3 above.
    # v0=0°, v1=5°, v2=35° → cos(0,5)=0.9962✓, cos(0,35)=0.8192✗,
    #   cos(5,35)=0.8480✗ → 1 above only.
    # v0=0°, v1=15°, v2=20° → cos(0,15)=0.9659✓, cos(0,20)=0.9397✓,
    #   cos(15,20)=0.9962✓ → all 3 above.
    # Need EXACTLY 2-of-3: separate one point.
    # v0=0°, v1=15°, v2=45° → cos(0,15)=0.9659✓, cos(0,45)=0.7071✗, cos(15,45)=0.8660✓ → 2 of 3.
    v0 = _angle(0)
    v1 = _angle(15)
    v2 = _angle(45)
    chosen, logs = await _run(threshold=0.85, vectors=[v0, v1, v2], drafts=["a", "b", "c"])
    assert logs, "expected sc.result log event"
    above = logs[0]["above_threshold"]
    majority = logs[0]["majority_required"]
    assert above == 2, f"expected 2 pairs > θ, got {above}"
    assert majority == pytest.approx(1.5)
    assert logs[0]["converged"] is True
    assert chosen in {"a", "b", "c"}


@pytest.mark.asyncio
async def test_n3_one_pair_above_threshold_not_converged() -> None:
    """3 drafts; 1 of 3 unique pairs > θ → NOT converged."""
    # v0=0°, v1=5°, v2=80° → cos(0,5)=0.9962✓, cos(0,80)=0.1736✗, cos(5,80)=0.2588✗ → 1 of 3.
    v0 = _angle(0)
    v1 = _angle(5)
    v2 = _angle(80)
    chosen, logs = await _run(threshold=0.85, vectors=[v0, v1, v2], drafts=["a", "b", "c"])
    assert logs
    above = logs[0]["above_threshold"]
    majority = logs[0]["majority_required"]
    assert above == 1, f"expected 1 pair > θ, got {above}"
    assert majority == pytest.approx(1.5)
    assert logs[0]["converged"] is False


@pytest.mark.asyncio
async def test_n3_all_pairs_above_threshold_converged() -> None:
    """3 drafts; 3 of 3 unique pairs > θ → trivially converged."""
    # v0=0°, v1=5°, v2=10° — all pairs near 1.
    v0 = _angle(0)
    v1 = _angle(5)
    v2 = _angle(10)
    chosen, logs = await _run(threshold=0.85, vectors=[v0, v1, v2], drafts=["a", "b", "c"])
    assert logs
    assert logs[0]["above_threshold"] == 3
    assert logs[0]["converged"] is True


# ---------------------------------------------------------------------------
# N=5 cases — exercises that majority_required = 5 (need > 5, i.e. >= 6).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_n5_seven_of_ten_pairs_above_threshold_converged() -> None:
    """5 drafts; 7 of 10 unique pairs > θ → > 5 majority → converged."""
    # Geometry: place 4 vectors at small spread (all pairs above θ), 1 vector
    # far away (3 pairs below θ).  4-choose-2 = 6 close pairs all above; the
    # outlier vector gives 4 outlier pairs (all below if angle is large enough).
    # Total above = 6, total below = 4 → above = 6, below = 4.  Need 7 above; tighten.
    # Tighten with the outlier closer: 4 close (0°, 5°, 10°, 15°) all mutual above (6 pairs).
    # Outlier at 35°: cos(0,35)=0.819✗, cos(5,35)=0.848✗, cos(10,35)=0.906✓,
    # cos(15,35)=0.946✓ → 2 above outlier-pairs → total above = 6 + 2 = 8.  Use this.
    angles = [0, 5, 10, 15, 35]
    vectors = [_angle(a) for a in angles]
    chosen, logs = await _run(
        threshold=0.85, vectors=vectors, drafts=["a", "b", "c", "d", "e"]
    )
    assert logs
    above_int = int(logs[0]["above_threshold"])  # type: ignore[arg-type]
    majority_f = float(logs[0]["majority_required"])  # type: ignore[arg-type]
    assert majority_f == pytest.approx(5.0)
    assert above_int > majority_f, (
        f"above={above_int} must exceed majority={majority_f}"
    )
    assert logs[0]["converged"] is True


@pytest.mark.asyncio
async def test_n5_exactly_five_pairs_above_threshold_not_converged() -> None:
    """5 drafts; 5 of 10 pairs > θ — NOT strictly greater than 5 → NOT converged.

    Geometry: two close clusters of 2 vectors each, plus one outlier.
    Cluster A: 0°, 5° (pair AB → above).
    Cluster B: 60°, 65° (pair BB → above).
    Outlier: 175°.
    Pairs:
      (A1,A2): cos(0,5)=0.996  ✓
      (B1,B2): cos(60,65)=0.996  ✓
      (A1,B1): cos(0,60)=0.5  ✗
      (A1,B2): cos(0,65)=0.42 ✗
      (A2,B1): cos(5,60)=0.57 ✗
      (A2,B2): cos(5,65)=0.5  ✗
      (A1,O):  cos(0,175)=-0.996 ✗
      (A2,O):  cos(5,175)=-0.985 ✗
      (B1,O):  cos(60,175)=-0.82 ✗
      (B2,O):  cos(65,175)=-0.77 ✗
    Above = 2 — way below majority.

    To hit exactly 5, design a cluster-of-3 + isolated-2.
    Cluster A (3 close): 0°, 5°, 10° → 3 pairs all > 0.85.
    Cluster B (2 isolated): 75°, 175°.  Pair (75,175): cos(100°)=-0.17 → below.
    Cross: (0,75)=0.26, (0,175)=-0.996, (5,75)=0.34, (5,175)=-0.985,
           (10,75)=0.42, (10,175)=-0.97 → all below.
    Above = 3 — still below 5.

    Five-of-ten requires a 4-clique (6 pairs above) too high, or 3-clique (3 above) too low.
    Try cluster-A 4 close + cluster-B 1 far + tune so 2 cross pairs sit just above 0.85.
    Angles: 0°, 5°, 10°, 30°, 70°.
    Pairs (10 total):
      (0,5)=0.9962 ✓
      (0,10)=0.9848 ✓
      (0,30)=0.866 ✓ (barely; cos30 = √3/2 ≈ 0.866 > 0.85)
      (0,70)=0.342 ✗
      (5,10)=0.9962 ✓
      (5,30)=0.9063 ✓
      (5,70)=0.4226 ✗
      (10,30)=0.9397 ✓
      (10,70)=0.5  ✗
      (30,70)=0.766 ✗
    Above = 6 — too many.

    Push 30° to 32°: cos(0,32)=0.848<0.85✗, cos(5,32)=0.891✓, cos(10,32)=0.927✓
    Pairs:
      (0,5)=0.996 ✓
      (0,10)=0.985 ✓
      (0,32)=0.848 ✗
      (0,70)=0.342 ✗
      (5,10)=0.996 ✓
      (5,32)=0.891 ✓
      (5,70)=0.423 ✗
      (10,32)=0.927 ✓
      (10,70)=0.5 ✗
      (32,70)=0.788 ✗
    Above = 5 — exactly what we need.
    """
    angles = [0, 5, 10, 32, 70]
    vectors = [_angle(a) for a in angles]
    chosen, logs = await _run(
        threshold=0.85, vectors=vectors, drafts=["a", "b", "c", "d", "e"]
    )
    assert logs
    above = logs[0]["above_threshold"]
    majority = logs[0]["majority_required"]
    assert majority == pytest.approx(5.0)
    assert above == 5, f"expected exactly 5 pairs above, got {above}"
    # Strictly greater than required — 5 is NOT > 5.
    assert logs[0]["converged"] is False
