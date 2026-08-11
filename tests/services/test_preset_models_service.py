# SPDX-License-Identifier: Apache-2.0
"""Unit tests for PresetModelsService (W5).

Covers:
- get/set round-trip (known provider accepted, mapping persisted + returned).
- empty mapping → NULL stored; get returns {}.
- unknown provider rejected (entry dropped, not persisted).
- update overwrites previous value.
- provider=lmstudio always accepted (built-in, no registry needed).
- missing model_id drops entry.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine

from lmchat.db.schema import metadata
from lmchat.services.preset_models_service import PresetModelsService

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def engine():
    eng = create_async_engine("sqlite+aiosqlite:///:memory:", echo=False)
    async with eng.begin() as conn:
        await conn.run_sync(metadata.create_all)
    # Insert a dummy user so FK constraint is satisfied.
    from sqlalchemy import text
    async with eng.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash, is_admin) "
                "VALUES (1, 'alice', 'x', 0)"
            )
        )
    yield eng
    await eng.dispose()


def _make_registry(*known_slugs: str) -> MagicMock:
    """Return a stub ProviderRegistry that knows the given slugs."""
    registry = MagicMock()
    def _get(name: str) -> object | None:
        return MagicMock() if name in known_slugs else None
    registry.get = MagicMock(side_effect=_get)
    return registry


def _make_svc(engine, *known_slugs: str) -> PresetModelsService:
    registry = _make_registry(*known_slugs) if known_slugs else None
    return PresetModelsService(engine=engine, provider_registry=registry)


# ---------------------------------------------------------------------------
# Tests — get / set round-trip
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_empty_when_no_row(engine) -> None:
    """get_preset_models returns {} when no user_prefs row exists."""
    svc = _make_svc(engine)
    result = await svc.get_preset_models(user_id=1)
    assert result == {}


@pytest.mark.asyncio
async def test_set_and_get_round_trip(engine) -> None:
    """set → get returns the same mapping."""
    svc = _make_svc(engine, "openrouter")
    mapping = {
        "general": {"provider": "lmstudio", "model_id": "phi-4"},
        "research": {"provider": "openrouter", "model_id": "qwen/qwq"},
    }
    saved = await svc.set_preset_models(user_id=1, mapping=mapping)
    assert saved == mapping

    fetched = await svc.get_preset_models(user_id=1)
    assert fetched == mapping


@pytest.mark.asyncio
async def test_set_overwrites_previous(engine) -> None:
    """A second set replaces the first value."""
    svc = _make_svc(engine, "openrouter")
    await svc.set_preset_models(
        user_id=1,
        mapping={"general": {"provider": "lmstudio", "model_id": "old-model"}},
    )
    await svc.set_preset_models(
        user_id=1,
        mapping={"research": {"provider": "openrouter", "model_id": "new-model"}},
    )
    fetched = await svc.get_preset_models(user_id=1)
    assert "research" in fetched
    assert fetched["research"]["model_id"] == "new-model"
    # Old entry must be gone.
    assert "general" not in fetched


@pytest.mark.asyncio
async def test_set_empty_mapping_clears(engine) -> None:
    """set with {} clears the stored value; get returns {}."""
    svc = _make_svc(engine, "openrouter")
    await svc.set_preset_models(
        user_id=1,
        mapping={"general": {"provider": "openrouter", "model_id": "phi-4"}},
    )
    saved = await svc.set_preset_models(user_id=1, mapping={})
    assert saved == {}

    fetched = await svc.get_preset_models(user_id=1)
    assert fetched == {}


# ---------------------------------------------------------------------------
# Tests — provider validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_provider_entry_is_dropped(engine) -> None:
    """Entry with an unregistered provider slug is silently dropped."""
    registry = _make_registry("openrouter")  # "ghost" not in registry
    svc = PresetModelsService(engine=engine, provider_registry=registry)
    mapping = {
        "general": {"provider": "ghost", "model_id": "phantom-model"},
        "research": {"provider": "openrouter", "model_id": "qwen/qwq"},
    }
    saved = await svc.set_preset_models(user_id=1, mapping=mapping)
    # "ghost" entry must be absent.
    assert "general" not in saved, (
        f"entry with unknown provider must be dropped; got {saved!r}"
    )
    # Valid entry must survive.
    assert "research" in saved
    assert saved["research"]["provider"] == "openrouter"

    # Persisted value also excludes the dropped entry.
    fetched = await svc.get_preset_models(user_id=1)
    assert "general" not in fetched


@pytest.mark.asyncio
async def test_lmstudio_always_accepted_without_registry(engine) -> None:
    """provider='lmstudio' is always valid even when no registry is set."""
    svc = PresetModelsService(engine=engine, provider_registry=None)
    mapping = {"coder": {"provider": "lmstudio", "model_id": "qwen2.5-coder"}}
    saved = await svc.set_preset_models(user_id=1, mapping=mapping)
    assert "coder" in saved
    assert saved["coder"]["provider"] == "lmstudio"


@pytest.mark.asyncio
async def test_no_registry_accepts_all_providers(engine) -> None:
    """When provider_registry is None, all slugs are accepted (test environment)."""
    svc = PresetModelsService(engine=engine, provider_registry=None)
    mapping = {"analyst": {"provider": "some-cloud", "model_id": "gpt-test"}}
    saved = await svc.set_preset_models(user_id=1, mapping=mapping)
    assert "analyst" in saved


@pytest.mark.asyncio
async def test_all_unknown_providers_gives_empty_result(engine) -> None:
    """All entries dropped → empty dict returned; column cleared to NULL."""
    registry = _make_registry("openrouter")
    svc = PresetModelsService(engine=engine, provider_registry=registry)
    mapping = {
        "general": {"provider": "unknown-a", "model_id": "m1"},
        "coder": {"provider": "unknown-b", "model_id": "m2"},
    }
    saved = await svc.set_preset_models(user_id=1, mapping=mapping)
    assert saved == {}, f"all dropped → empty dict, got {saved!r}"

    fetched = await svc.get_preset_models(user_id=1)
    assert fetched == {}


# ---------------------------------------------------------------------------
# Tests — entry shape validation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_entry_missing_model_id_is_dropped(engine) -> None:
    """Entry without model_id is silently dropped."""
    svc = _make_svc(engine)
    mapping = {"general": {"provider": "lmstudio"}}  # no model_id
    saved = await svc.set_preset_models(user_id=1, mapping=mapping)
    assert "general" not in saved


@pytest.mark.asyncio
async def test_entry_non_dict_value_is_dropped(engine) -> None:
    """Entry whose value is not a dict is silently dropped."""
    svc = _make_svc(engine)
    mapping = {"general": "not-a-dict"}  # type: ignore[dict-item]
    saved = await svc.set_preset_models(user_id=1, mapping=mapping)
    assert "general" not in saved


@pytest.mark.asyncio
async def test_default_provider_lmstudio_when_absent(engine) -> None:
    """Entry without provider key defaults to 'lmstudio'."""
    svc = _make_svc(engine)
    mapping = {"coder": {"model_id": "phi-4"}}  # no provider
    saved = await svc.set_preset_models(user_id=1, mapping=mapping)
    assert "coder" in saved
    assert saved["coder"]["provider"] == "lmstudio"
