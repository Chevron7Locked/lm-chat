# SPDX-License-Identifier: Apache-2.0
"""Prompt library CRUD service for lm-chat.

Manages user-owned prompt presets stored in the ``prompts`` table.
Each prompt has a ``name`` (unique per user) and ``content`` (full text).
The composer accepts a ``/prompt <name>`` slash command that inserts the
preset text into the message composer (frontend-only; the backend provides
the data).

Invariants:
- Prompt names are unique per user (``uq_prompt_user_name`` constraint).
- Cross-user access always returns a ``PromptNotFoundError``, never a 403.
- CRUD operations use ``with_write_retry`` for transient SQLite busy errors.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict
from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.db.retry import with_write_retry
from lmchat.db.schema import prompts
from lmchat.logging import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class PromptNotFoundError(Exception):
    """Prompt missing OR not owned by the requesting user.

    Intentionally does not distinguish "missing" from "not yours" so that
    HTTP 404 is the only status code — 403 would leak existence.
    """


class PromptNameConflictError(Exception):
    """A prompt with this name already exists for the user."""


# ---------------------------------------------------------------------------
# Public Pydantic model
# ---------------------------------------------------------------------------


class Prompt(BaseModel):
    """Pydantic projection of one row from the ``prompts`` table.

    Attributes:
        id:         Row PK.
        user_id:    Owning user PK.
        name:       Short display label (unique per user).
        content:    Full prompt text.
        created_at: Row creation timestamp.
        updated_at: Last-updated timestamp.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    user_id: int
    name: str
    content: str
    created_at: datetime
    updated_at: datetime


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class PromptLibraryService:
    """CRUD service for user-managed prompt presets.

    Args:
        engine: Async SQLAlchemy engine.
    """

    def __init__(self, *, engine: AsyncEngine) -> None:
        self._engine = engine

    async def _get_prompt_row(self, prompt_id: int, *, user_id: int) -> object:
        """Fetch the prompts row for *prompt_id* owned by *user_id*.

        Args:
            prompt_id: PK of the prompt.
            user_id:   Must match the prompt's ``user_id`` column.

        Returns:
            SQLAlchemy Row.

        Raises:
            PromptNotFoundError: Row missing or owned by another user.
        """
        async with self._engine.connect() as conn:
            result = await conn.execute(
                select(prompts).where(
                    prompts.c.id == prompt_id,
                    prompts.c.user_id == user_id,
                )
            )
            row = result.fetchone()
        if row is None:
            raise PromptNotFoundError(
                f"prompt {prompt_id!r} not found for user {user_id!r}"
            )
        return row

    async def list_for_user(self, user_id: int) -> list[Prompt]:
        """Return all prompts for *user_id* ordered by name.

        Args:
            user_id: Owning user PK.

        Returns:
            List of :class:`Prompt`, alphabetically sorted.
        """
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(prompts)
                    .where(prompts.c.user_id == user_id)
                    .order_by(prompts.c.name)
                )
            ).fetchall()
        return [Prompt.model_validate(r, from_attributes=True) for r in rows]

    async def create(self, *, user_id: int, name: str, content: str) -> Prompt:
        """Insert a new prompt and return it.

        Args:
            user_id: PK of the owning user.
            name:    Short display label (unique per user).
            content: Full prompt text.

        Returns:
            The newly created :class:`Prompt`.

        Raises:
            PromptNameConflictError: If a prompt with *name* already exists
                for *user_id*.
        """

        async def _insert() -> int:
            async with self._engine.begin() as conn:
                result = await conn.execute(
                    insert(prompts).values(
                        user_id=user_id,
                        name=name,
                        content=content,
                    )
                )
                pk = result.inserted_primary_key
                if pk is None:
                    raise RuntimeError("INSERT into prompts returned no PK")
                return int(pk[0])

        try:
            new_id = await with_write_retry(_insert)
        except IntegrityError as exc:
            raise PromptNameConflictError(
                f"prompt named {name!r} already exists for user {user_id!r}"
            ) from exc

        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    select(prompts).where(prompts.c.id == new_id)
                )
            ).fetchone()

        if row is None:
            raise RuntimeError(f"prompts row {new_id!r} not found after INSERT")

        log.info("prompt.created", prompt_id=new_id, user_id=user_id, name=name)
        return Prompt.model_validate(row, from_attributes=True)

    async def update(
        self,
        prompt_id: int,
        *,
        user_id: int,
        name: str | None = None,
        content: str | None = None,
    ) -> Prompt:
        """Update a prompt's name and/or content.

        Args:
            prompt_id: PK of the prompt.
            user_id:   Must own the prompt.
            name:      New name (optional).
            content:   New content (optional).

        Returns:
            The updated :class:`Prompt`.

        Raises:
            PromptNotFoundError:    Row missing or owned by another user.
            PromptNameConflictError: New name conflicts with an existing prompt.
        """
        await self._get_prompt_row(prompt_id, user_id=user_id)

        values: dict[str, object] = {}  # type: ignore[type-arg]
        if name is not None:
            values["name"] = name
        if content is not None:
            values["content"] = content
        if not values:
            # Nothing to update — return as-is.
            row = await self._get_prompt_row(prompt_id, user_id=user_id)
            return Prompt.model_validate(row, from_attributes=True)

        # updated_at is not managed by the DB trigger for SQLite, so we set
        # it explicitly here. The column has server_default=func.now() but
        # UPDATE does not fire the default automatically on SQLite.
        from datetime import UTC
        values["updated_at"] = datetime.now(UTC)

        async def _update() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    update(prompts)
                    .where(
                        prompts.c.id == prompt_id,
                        prompts.c.user_id == user_id,
                    )
                    .values(**values)
                )

        try:
            await with_write_retry(_update)
        except IntegrityError as exc:
            raise PromptNameConflictError(
                f"prompt named {name!r} already exists for user {user_id!r}"
            ) from exc

        row = await self._get_prompt_row(prompt_id, user_id=user_id)
        log.info("prompt.updated", prompt_id=prompt_id, user_id=user_id)
        return Prompt.model_validate(row, from_attributes=True)

    async def delete(self, prompt_id: int, *, user_id: int) -> None:
        """Delete a prompt.

        Args:
            prompt_id: PK of the prompt to delete.
            user_id:   Must own the prompt.

        Raises:
            PromptNotFoundError: Row missing or owned by another user.
        """
        await self._get_prompt_row(prompt_id, user_id=user_id)

        async def _delete() -> None:
            async with self._engine.begin() as conn:
                await conn.execute(
                    delete(prompts).where(
                        prompts.c.id == prompt_id,
                        prompts.c.user_id == user_id,
                    )
                )

        await with_write_retry(_delete)
        log.info("prompt.deleted", prompt_id=prompt_id, user_id=user_id)
