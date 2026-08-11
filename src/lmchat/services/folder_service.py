# SPDX-License-Identifier: Apache-2.0
"""Folder catalogue service for lm-chat (feature-parity closure).

Folders are a user-named taxonomy stored two ways:

1. ``chats.folder`` — when a chat lives in a folder its name is set on the
   row.  This is the source of truth for "which folder a chat is in".
2. ``user_prefs.folders`` — JSON list of folder names the admin has
   created but may not yet have moved any chats into.  Combined with the
   distinct-folder set from ``chats`` this lets the sidebar show an empty
   folder right after the user clicks ``+ New folder``.

The visible folder set for ``user_id`` is::

    SELECT DISTINCT folder FROM chats WHERE user_id=? AND folder IS NOT NULL
    UNION
    SELECT unnest(prefs.folders)

Renames operate on both layers atomically (UPDATE chats SET folder=... +
JSON manipulation on prefs.folders).  Deletes set ``chats.folder=NULL``
on every matching chat (so the chats survive; they're just unfoldered)
and drop the name from ``prefs.folders``.

Ownership invariant
-------------------
Every method takes ``user_id`` and scopes its reads/writes by it.  The
route layer performs auth gating; the service performs ownership scoping.
Cross-user folder access is structurally impossible because the chats
table and the user_prefs row are both keyed by ``user_id``.
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.db.retry import with_write_retry
from lmchat.db.schema import chats as chats_table
from lmchat.db.schema import user_prefs as user_prefs_table
from lmchat.logging import get_logger
from lmchat.services._user_prefs_upsert import user_prefs_upsert
from lmchat.services.audit_service import write_audit_event

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Folder source abstraction (hard invariant)
# ---------------------------------------------------------------------------
#
# This module originally shipped two parallel sets of folder methods:
#   - ``list_folders / add_folder / rename_folder / delete_folder``
#     operating on ``user_prefs.folders`` JSON + chats filtered by user_id
#   - ``list_folders_for_project / add_folder_to_project / ...``
#     operating on ``projects.folders`` JSON + chats filtered by
#     (user_id, project_id)
#
# Consolidation is a hard invariant: rename / delete /
# list-with-counts logic must live ONCE. The dedup target is the audit
# event + collision check + chats nullify/rename + JSON read-modify-write
# code, which was structurally identical between the two surfaces.
#
# The shape: ``_FolderSource`` is a small ABC that hides where the folder
# list lives (user_prefs row vs a project row) and which chats it
# governs. The mutation methods take a source and delegate the JSON I/O
# + the chats WHERE clause to it. The public methods construct the right
# source and call into ONE shared implementation.

from dataclasses import dataclass  # noqa: E402, I001  # late import — avoids circular dep with folder_service
from sqlalchemy.sql import ColumnElement  # noqa: E402  # late import — avoids circular dep with folder_service

# The _FolderSource type alias below is a union of the two concrete
# source implementations. We avoid `Protocol` here because combining a
# protocol with @dataclass(frozen=True, slots=True) trips Pyright on
# the `user_id` writability invariant. Both dataclasses share the
# `user_id` attribute + a uniform read/write/chats_where/audit_detail
# interface enforced by duck typing in the consolidated impl methods.


@dataclass(frozen=True, slots=True)
class _UserPrefsFolderSource:
    """Folder list in ``user_prefs.folders`` JSON for *user_id*."""

    user_id: int

    async def read(self, conn: Any) -> list[str] | None:
        # user_prefs is auto-created on first write; absence ↔ empty.
        return await _fetch_prefs(conn, self.user_id)

    async def write(
        self, conn: Any, folders: list[str]
    ) -> bool:
        await _write_prefs(conn, self.user_id, folders)
        return True

    def chats_where(self) -> tuple[ColumnElement, ...]:
        return (chats_table.c.user_id == self.user_id,)

    def audit_detail_extras(self) -> dict[str, Any]:
        return {}


# ``_ProjectFolderSource`` was
# REMOVED. Per-project folders were dropped after conceptual review.
# The route layer (``routes/folders.py``) now
# returns 410 GONE for any call carrying ``project_id`` so older
# frontend builds get a structured error instead of a 500.
# Archive of the pre-removal ``projects.folders`` JSON lives in
# ``projects.meta.folders`` per migration 0023a.

_FolderSource = _UserPrefsFolderSource


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class FolderServiceError(Exception):
    """Base class for FolderService errors."""


class InvalidFolderNameError(FolderServiceError):
    """Raised when a folder name is empty, too long, or contains NULs.

    Note that ``chats.folder`` is Text so most characters are allowed; the
    constraints here exist to give the UI predictable behaviour and to
    prevent oddities like newline-laden names.
    """


class FolderConflictError(FolderServiceError):
    """Raised on rename when the destination folder already exists.

    Silent set-deduplication on rename would
    merge folders without asking, destroying the user's mental model.
    The route layer surfaces this as HTTP 409; the UI prompts the user
    to confirm merge or pick a different name.
    """


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_MAX_FOLDER_NAME_LEN: int = 128


def _validate_folder_name(name: str) -> str:
    """Return *name* trimmed; raise :class:`InvalidFolderNameError` on bad input.

    Args:
        name: Admin-supplied folder name.

    Returns:
        The trimmed name (whitespace stripped).

    Raises:
        InvalidFolderNameError: If the name is empty after trimming, longer
            than ``_MAX_FOLDER_NAME_LEN``, or contains control characters.
    """
    trimmed = name.strip()
    if trimmed == "":
        raise InvalidFolderNameError("folder name may not be empty")
    if len(trimmed) > _MAX_FOLDER_NAME_LEN:
        raise InvalidFolderNameError(
            f"folder name may not exceed {_MAX_FOLDER_NAME_LEN} characters"
        )
    if any(ord(c) < 0x20 for c in trimmed):
        raise InvalidFolderNameError(
            "folder name may not contain control characters"
        )
    return trimmed


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class FolderService:
    """CRUD on the admin-managed folder catalogue.

    Args:
        engine: Async SQLAlchemy engine connected to the application DB.
    """

    def __init__(self, *, engine: AsyncEngine) -> None:
        self._engine = engine

    # ------------------------------------------------------------------
    # Read path
    # ------------------------------------------------------------------

    async def list_folders(self, *, user_id: int) -> list[str]:
        """Return the visible folder list for *user_id*, alphabetically sorted.

        Delegates to ``_list_with_counts`` via
        ``_UserPrefsFolderSource``.

        ``_UserPrefsFolderSource.read()`` calls ``_fetch_prefs`` which
        is documented to never return ``None`` (it returns ``[]`` for
        a missing row and on every JSON-parse failure). The ``or []``
        below is therefore unreachable under the current source
        impl, but kept as a structural safety net so a future source
        change that introduces a None branch can't silently 500 this
        public method (kept as documented defensive code).

        Args:
            user_id: Owning user's PK.

        Returns:
            Sorted, de-duplicated list of folder names.
        """
        result = await self._list_with_counts(
            _UserPrefsFolderSource(user_id=user_id)
        )
        return result or []

    # ------------------------------------------------------------------
    # Write path
    # ------------------------------------------------------------------

    async def add_folder(self, *, user_id: int, name: str) -> list[str]:
        """Add *name* to the admin's folder catalogue.

        Idempotent — adding an existing name is a no-op (the catalogue
        stays sorted-and-deduped on read). Delegates to
        ``_add_via_source`` via ``_UserPrefsFolderSource``.

        Raises:
            InvalidFolderNameError: If *name* fails validation.
        """
        result = await self._add_via_source(
            _UserPrefsFolderSource(user_id=user_id), name
        )
        return result if result is not None else []

    async def rename_folder(
        self,
        *,
        user_id: int,
        old_name: str,
        new_name: str,
    ) -> list[str]:
        """Rename *old_name* → *new_name* across prefs + chats atomically.

        Delegates to ``_rename_via_source`` via
        ``_UserPrefsFolderSource``.

        Raises:
            InvalidFolderNameError: If *new_name* fails validation.
            FolderConflictError: If a folder with *new_name* exists.
        """
        result = await self._rename_via_source(
            _UserPrefsFolderSource(user_id=user_id),
            old_name=old_name,
            new_name=new_name,
        )
        return result if result is not None else []

    async def delete_folder(self, *, user_id: int, name: str) -> list[str]:
        """Remove *name* from the catalogue and unfolder all matching chats.

        Delegates to ``_delete_via_source`` via
        ``_UserPrefsFolderSource``. Idempotent — deleting a folder
        that doesn't exist returns the unchanged list.
        """
        result = await self._delete_via_source(
            _UserPrefsFolderSource(user_id=user_id), name
        )
        return result if result is not None else []

    # The four project-scoped
    # methods (``list_folders_for_project`` / ``add_folder_to_project``
    # / ``rename_folder_in_project`` / ``delete_folder_from_project``)
    # were REMOVED alongside ``_ProjectFolderSource``. The route layer
    # at ``routes/folders.py`` returns 410 GONE for any call carrying
    # ``project_id`` so older frontend builds get a structured error
    # instead of a 500. Pre-removal data lives in
    # ``projects.meta.folders`` per migration 0023a.

    # ------------------------------------------------------------------
    # Consolidated source-driven implementations (hard invariant)
    #
    # Each of these takes a ``_FolderSource`` and runs the rename /
    # delete / list-with-counts logic against it. The user-pref source
    # always returns folders (empty list when no row); the project
    # source returns ``None`` when the project is missing or not
    # owned — the route maps that to 404 without leaking existence.
    # ------------------------------------------------------------------

    async def _list_with_counts(
        self, source: _FolderSource
    ) -> list[str] | None:
        async with self._engine.connect() as conn:
            current = await source.read(conn)
            if current is None:
                return None
            chat_rows = (
                await conn.execute(
                    select(chats_table.c.folder)
                    .where(*source.chats_where())
                    .where(chats_table.c.folder.is_not(None))
                    .distinct()
                )
            ).fetchall()
            chat_folders = [
                row[0] for row in chat_rows if row[0] is not None
            ]
        return sorted(set(current) | set(chat_folders))

    async def _add_via_source(
        self, source: _FolderSource, name: str
    ) -> list[str] | None:
        validated = _validate_folder_name(name)

        async def _do() -> bool:
            async with self._engine.begin() as conn:
                current = await source.read(conn)
                if current is None:
                    return False
                new_list = _add(current, validated)
                return await source.write(conn, new_list)

        wrote = await with_write_retry(_do)
        if not wrote:
            return None
        log.info(
            "folders.add",
            user_id=source.user_id,
            name=validated,
            **source.audit_detail_extras(),
        )
        try:
            await write_audit_event(
                user_id=source.user_id,
                event="folder.added",
                ip=None,
                user_agent=None,
                detail={
                    "name": validated,
                    **source.audit_detail_extras(),
                },
                engine=self._engine,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "folders.audit_write_failed",
                op="add",
                error=str(exc),
            )
        return await self._list_with_counts(source)

    async def _rename_via_source(
        self,
        source: _FolderSource,
        *,
        old_name: str,
        new_name: str,
    ) -> list[str] | None:
        validated_new = _validate_folder_name(new_name)
        validated_old = old_name.strip()
        if validated_old == "":
            raise InvalidFolderNameError(
                "old folder name may not be empty"
            )
        if validated_new == validated_old:
            return await self._list_with_counts(source)

        _COLLISION_SENTINEL = "__collision__"

        async def _do() -> str | bool:
            async with self._engine.begin() as conn:
                current = await source.read(conn)
                if current is None:
                    return False
                chat_rows = (
                    await conn.execute(
                        select(chats_table.c.folder)
                        .where(*source.chats_where())
                        .where(chats_table.c.folder.is_not(None))
                        .distinct()
                    )
                ).fetchall()
                distinct_in_chats = {
                    row[0]
                    for row in chat_rows
                    if isinstance(row[0], str)
                }
                if validated_new in (set(current) | distinct_in_chats):
                    return _COLLISION_SENTINEL
                await conn.execute(
                    update(chats_table)
                    .where(*source.chats_where())
                    .where(chats_table.c.folder == validated_old)
                    .values(folder=validated_new)
                )
                renamed = _rename(current, validated_old, validated_new)
                return await source.write(conn, renamed)

        outcome = await with_write_retry(_do)
        if outcome == _COLLISION_SENTINEL:
            raise FolderConflictError(
                f"folder {validated_new!r} already exists; "
                "pick a different name or delete the existing folder "
                "first"
            )
        wrote = bool(outcome)
        if not wrote:
            return None
        log.info(
            "folders.rename",
            user_id=source.user_id,
            old=validated_old,
            new=validated_new,
            **source.audit_detail_extras(),
        )
        try:
            await write_audit_event(
                user_id=source.user_id,
                event="folder.renamed",
                ip=None,
                user_agent=None,
                detail={
                    "old": validated_old,
                    "new": validated_new,
                    **source.audit_detail_extras(),
                },
                engine=self._engine,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "folders.audit_write_failed",
                op="rename",
                error=str(exc),
            )
        return await self._list_with_counts(source)

    async def _delete_via_source(
        self, source: _FolderSource, name: str
    ) -> list[str] | None:
        validated = name.strip()
        if validated == "":
            raise InvalidFolderNameError("folder name may not be empty")

        async def _do() -> bool:
            async with self._engine.begin() as conn:
                await conn.execute(
                    update(chats_table)
                    .where(*source.chats_where())
                    .where(chats_table.c.folder == validated)
                    .values(folder=None)
                )
                current = await source.read(conn)
                if current is None:
                    return False
                pruned = _remove(current, validated)
                return await source.write(conn, pruned)

        wrote = await with_write_retry(_do)
        if not wrote:
            return None
        log.info(
            "folders.delete",
            user_id=source.user_id,
            name=validated,
            **source.audit_detail_extras(),
        )
        try:
            await write_audit_event(
                user_id=source.user_id,
                event="folder.deleted",
                ip=None,
                user_agent=None,
                detail={
                    "name": validated,
                    **source.audit_detail_extras(),
                },
                engine=self._engine,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "folders.audit_write_failed",
                op="delete",
                error=str(exc),
            )
        return await self._list_with_counts(source)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    


# ---------------------------------------------------------------------------
# Pure-Python list manipulators (testable in isolation)
# ---------------------------------------------------------------------------


def _add(current: list[str], name: str) -> list[str]:
    """Return a new list with *name* added (dedup, sorted)."""
    return sorted({*current, name})


def _rename(current: list[str], old: str, new: str) -> list[str]:
    """Return a new list with *old* → *new* replaced (dedup, sorted).

    If *old* is not present, *new* is added (the rename semantically
    "claims" the new name even when the prefs row didn't have *old*; the
    chats.folder rename path may still have migrated chat rows).
    """
    mapped = {(new if x == old else x) for x in current}
    mapped.add(new)
    return sorted(mapped)


def _remove(current: list[str], name: str) -> list[str]:
    """Return a new list with *name* removed."""
    return sorted([x for x in current if x != name])


async def _fetch_prefs(conn: Any, user_id: int) -> list[str]:
    """Read prefs.folders for *user_id*; return [] when no row exists."""
    row = (
        await conn.execute(
            select(user_prefs_table.c.folders).where(
                user_prefs_table.c.user_id == user_id
            )
        )
    ).fetchone()
    if row is None or row[0] is None:
        return []
    raw = row[0]
    if isinstance(raw, str):
        import json as _json

        try:
            decoded = _json.loads(raw)
        except ValueError:
            return []
        if isinstance(decoded, list):
            return [str(x) for x in decoded if isinstance(x, str)]
        return []
    if isinstance(raw, list):
        return [str(x) for x in raw if isinstance(x, str)]
    return []


async def _write_prefs(conn: Any, user_id: int, folders: list[str]) -> None:
    """Upsert ``user_prefs`` for *user_id* with *folders* + bump updated_at.

    Delegates to the shared :func:`user_prefs_upsert` primitive (atomic
    dialect-native ``INSERT ... ON CONFLICT DO UPDATE`` — see
    ``lmchat.services._user_prefs_upsert``). *folders* is the only
    column this caller ever writes, on both the insert and the
    conflict/update branch, so the same value is passed for both.
    """
    await user_prefs_upsert(
        conn,
        user_id,
        insert_extra={"folders": folders},
        update_values={"folders": folders},
    )
