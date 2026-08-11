"""Cross-project regression tests for MessageService.search().

The search route's scope=messages + scope=chats branches were missing
project_id wiring. These tests pin the contract at the service-layer
boundary. Companion route-level tests live alongside in
tests/routes/test_search_scope.py.
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from unittest.mock import AsyncMock

import alembic.command
import alembic.config
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from lmchat.embedding.client import EmbeddingClient
from lmchat.services.memory_service import MemoryService
from lmchat.services.message_service import MessageService
from lmchat.services.models_service import (
    Capabilities,
    ModelInfo,
    ModelsService,
)

# Ensure migrations/ is importable.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _alembic_cfg(db_url: str) -> alembic.config.Config:
    ini = _REPO_ROOT / "alembic.ini"
    cfg = alembic.config.Config(str(ini))
    cfg.set_main_option("sqlalchemy.url", db_url)
    cfg.set_main_option(
        "script_location", str(_REPO_ROOT / "migrations")
    )
    return cfg


def _run_upgrade(db_url: str) -> None:
    alembic.command.upgrade(_alembic_cfg(db_url), "head")


def _make_memory_service(engine: AsyncEngine) -> MemoryService:
    """Build a MemoryService stub mirroring test_message_search.py."""
    mock_embedding_client = AsyncMock(spec=EmbeddingClient)
    mock_models_service = AsyncMock(spec=ModelsService)
    mock_model = ModelInfo(
        key="embed-model",
        type="embedding",
        capabilities=Capabilities(
            vision=False, trained_for_tool_use=False
        ),
    )
    mock_models_service.list_loaded.return_value = [mock_model]
    return MemoryService(
        engine=engine,
        embedding_client=mock_embedding_client,
        models_service=mock_models_service,
    )


@pytest.fixture()
async def engine(tmp_path: Path) -> AsyncGenerator[AsyncEngine]:
    """Per-test SQLite engine with full migration chain applied."""
    db_path = tmp_path / "test_a2_search_scope.db"
    db_url = f"sqlite+aiosqlite:///{db_path}"
    await asyncio.to_thread(_run_upgrade, db_url)
    e = create_async_engine(db_url, pool_pre_ping=True)
    yield e
    await e.dispose()


@pytest.fixture()
def svc(engine: AsyncEngine) -> MessageService:
    return MessageService(
        engine=engine,
        memory_service=_make_memory_service(engine),
    )


async def _seed_two_project_chats(engine: AsyncEngine) -> None:
    """Two projects (PA, PB) + un-projected chat. Each chat carries a
    distinct message; the same word ``hit`` appears in all three.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO users (id, username, password_hash) "
                "VALUES (1, 'alice', 'scrypt$dummy')"
            )
        )
        await conn.execute(
            text(
                "INSERT INTO projects (id, user_id, name, description, "
                "system_prompt, created_at, updated_at) "
                "VALUES (10, 1, 'PA', '', '', 0, 0), "
                "       (20, 1, 'PB', '', '', 0, 0)"
            )
        )
        # 3 chats: one in PA, one in PB, one un-projected.
        await conn.execute(
            text(
                "INSERT INTO chats (id, user_id, title, project_id) "
                "VALUES (101, 1, 'in PA', 10), "
                "       (102, 1, 'in PB', 20), "
                "       (103, 1, 'no project', NULL)"
            )
        )
        # Each chat has one matching message — all contain ``hit``.
        await conn.execute(
            text(
                "INSERT INTO messages (chat_id, role, content) VALUES "
                "(101, 'user', 'hit from project A'), "
                "(102, 'user', 'hit from project B'), "
                "(103, 'user', 'hit from un-projected chat')"
            )
        )


# ─── MessageService.search() ─────────────────────────────────────────────


async def test_search_filters_by_project_id_sqlite_fts5(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """When project_id is set, search returns only messages whose parent
    chat carries that project_id. The other project's messages and the
    un-projected chat's message must be excluded.
    """
    await _seed_two_project_chats(engine)

    results = await svc.search(user_id=1, query="hit", project_id=10)
    chat_ids = sorted(r.chat_id for r in results)
    assert chat_ids == [101], (
        f"project_id=10 leaked rows from other scopes: {chat_ids}"
    )


async def test_search_project_id_none_returns_user_scoped_union(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """project_id=None preserves the legacy user-scoped union — every
    chat the user owns regardless of project_id. NOT "rows where
    chats.project_id IS NULL". This predicate shape is shared by every
    retrieval path.
    """
    await _seed_two_project_chats(engine)

    results = await svc.search(user_id=1, query="hit", project_id=None)
    chat_ids = sorted(r.chat_id for r in results)
    assert chat_ids == [101, 102, 103], (
        f"project_id=None must return user-scoped union, got: {chat_ids}"
    )


async def test_search_other_project_returns_only_its_chats(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """Symmetry: project_id=20 (PB) returns only PB's chat 102."""
    await _seed_two_project_chats(engine)

    results = await svc.search(user_id=1, query="hit", project_id=20)
    chat_ids = sorted(r.chat_id for r in results)
    assert chat_ids == [102], (
        f"project_id=20 leaked rows from other scopes: {chat_ids}"
    )


async def test_search_project_id_and_chat_id_compose(
    engine: AsyncEngine, svc: MessageService
) -> None:
    """When both project_id and chat_id are set, the result is the
    intersection. Both predicates compose without either silently
    overriding the other.
    """
    await _seed_two_project_chats(engine)

    # Chat 102 IS in project 20 — should appear.
    results = await svc.search(
        user_id=1, query="hit", project_id=20, chat_id=102
    )
    assert sorted(r.chat_id for r in results) == [102]

    # Chat 101 is NOT in project 20 — should be empty.
    results = await svc.search(
        user_id=1, query="hit", project_id=20, chat_id=101
    )
    assert results == []
