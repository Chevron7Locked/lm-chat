# SPDX-License-Identifier: Apache-2.0
"""Write-time system_prompt budget enforcement.

``projects.system_prompt`` ≤ 2000 estimated tokens; over-budget input is
REJECTED at write time (``InvalidProjectFieldError`` → routes translate
to 422) so user-authored text is never silently truncated.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.db.schema import metadata
from lmchat.services._token_budget import (
    PROJECT_PROMPT_TOKEN_BUDGET,
    approx_token_count,
)
from lmchat.services.projects_service import (
    InvalidProjectFieldError,
    ProjectsService,
)


async def _make_engine(tmp_path: Path):
    eng = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/c3_budget.db", pool_pre_ping=True
    )
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return eng


def _svc(engine) -> ProjectsService:
    return ProjectsService(engine=engine)


# ─── approx_token_count ──────────────────────────────────────────────────


def test_approx_token_count_ascii_path() -> None:
    """ASCII baseline — bytes == codepoints, so the byte-based
    heuristic matches the legacy codepoint convention."""
    assert approx_token_count("") == 0
    assert approx_token_count("x") == 1
    assert approx_token_count("x" * 4) == 1
    assert approx_token_count("x" * 8) == 2
    # 8000 chars ≈ 2000 tokens — at the budget edge.
    assert approx_token_count("x" * 8000) == 2000


def test_approx_token_count_cjk_uses_utf8_bytes_not_codepoints() -> None:
    """CJK pin: each CJK
    codepoint is 3 bytes in UTF-8, so 100 chars of CJK approximates
    75 tokens, NOT 25 (which the pre-fix codepoint heuristic
    returned). This is the load-bearing guarantee — without it the
    system_prompt budget under-counts CJK by 3× and lets oversize
    prompts slip past the 422 gate."""
    cjk_100 = "中" * 100
    # 100 codepoints × 3 bytes/codepoint = 300 bytes → 75 tokens.
    assert approx_token_count(cjk_100) == 75
    # Mixed prose — half ASCII, half CJK.
    mixed = ("a" * 50) + ("中" * 50)
    # 50 + 150 = 200 bytes → 50 tokens.
    assert approx_token_count(mixed) == 50


# ─── ProjectsService.create — write-time budget ──────────────────────────


@pytest.mark.asyncio
async def test_create_under_budget_succeeds(tmp_path: Path) -> None:
    eng = await _make_engine(tmp_path)
    svc = _svc(eng)
    # 4000 chars ≈ 1000 tokens — well under budget.
    project = await svc.create(
        user_id=1, name="Proj", system_prompt="x" * 4000
    )
    assert project.system_prompt == "x" * 4000
    await eng.dispose()


@pytest.mark.asyncio
async def test_create_at_budget_succeeds(tmp_path: Path) -> None:
    """At the boundary (estimated == budget) the create succeeds.
    Only exceeding the budget triggers the gate."""
    eng = await _make_engine(tmp_path)
    svc = _svc(eng)
    payload = "x" * (PROJECT_PROMPT_TOKEN_BUDGET * 4)
    assert approx_token_count(payload) == PROJECT_PROMPT_TOKEN_BUDGET
    project = await svc.create(
        user_id=1, name="Proj", system_prompt=payload
    )
    assert project is not None
    await eng.dispose()


@pytest.mark.asyncio
async def test_create_over_budget_raises_invalid_field(
    tmp_path: Path,
) -> None:
    """One token over the budget → InvalidProjectFieldError. The
    message names the budget AND the estimate so the admin can
    trim without guessing."""
    eng = await _make_engine(tmp_path)
    svc = _svc(eng)
    # 4 extra chars → 1 extra token over budget.
    payload = "x" * (PROJECT_PROMPT_TOKEN_BUDGET * 4 + 4)
    assert approx_token_count(payload) == PROJECT_PROMPT_TOKEN_BUDGET + 1

    with pytest.raises(InvalidProjectFieldError) as ei:
        await svc.create(user_id=1, name="Proj", system_prompt=payload)

    msg = str(ei.value)
    assert str(PROJECT_PROMPT_TOKEN_BUDGET) in msg
    assert "estimated" in msg.lower()
    await eng.dispose()


# ─── ProjectsService.update — write-time budget ──────────────────────────


@pytest.mark.asyncio
async def test_update_over_budget_raises(tmp_path: Path) -> None:
    """PATCH with an over-budget system_prompt is rejected; the row
    stays at its pre-mutation state."""
    eng = await _make_engine(tmp_path)
    svc = _svc(eng)
    project = await svc.create(
        user_id=1, name="Proj", system_prompt="hello"
    )

    over = "x" * (PROJECT_PROMPT_TOKEN_BUDGET * 4 + 4)
    with pytest.raises(InvalidProjectFieldError):
        await svc.update(
            user_id=1, project_id=project.id, system_prompt=over
        )

    refreshed = await svc.get(user_id=1, project_id=project.id)
    assert refreshed is not None
    assert refreshed.system_prompt == "hello", (
        f"row should not have been mutated, got: {refreshed.system_prompt!r}"
    )
    await eng.dispose()


@pytest.mark.asyncio
async def test_create_empty_prompt_passes_budget(tmp_path: Path) -> None:
    """Empty system_prompt is exempt — no token count, no rejection."""
    eng = await _make_engine(tmp_path)
    svc = _svc(eng)
    project = await svc.create(
        user_id=1, name="Proj", system_prompt=""
    )
    assert project.system_prompt == ""
    await eng.dispose()
