# SPDX-License-Identifier: Apache-2.0
"""Alembic async-engine migration environment.

Implements the standard SQLAlchemy 2.0 / Alembic async pattern:

- ``run_migrations_online()`` is the entry point for normal migration
  runs; it delegates to ``run_async_migrations()`` via
  ``asyncio.run()``.
- ``run_async_migrations()`` creates an ``AsyncEngine`` from the
  config, obtains an async connection, then passes a sync callable
  (``do_run_migrations``) to ``connection.run_sync()``.  The
  ``run_sync`` wrapper is the bridge between Alembic's synchronous
  context API and our async engine.
- ``run_migrations_offline()`` handles the ``--sql`` / offline mode
  used for SQL script generation (e.g., the gate command
  ``alembic upgrade head --sql``).

URL resolution order:
1. ``sqlalchemy.url`` already set on the ``Config`` object by the
   caller (startup.py, tests, CLI with an explicit ``-x`` flag).
2. Fall back to ``lmchat.config.get_settings().database_url`` so that
   a plain ``alembic upgrade head`` from the CLI Just Works without
   extra flags.

This ordering ensures the programmatic ``_build_alembic_cfg()`` path
in ``startup.py`` wins, while the CLI path still picks up the env var.
"""
from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import async_engine_from_config
from sqlalchemy.pool import NullPool

from lmchat.db.schema import metadata as target_metadata

# ---------------------------------------------------------------------------
# Standard Alembic boilerplate — wire up Python logging from alembic.ini.
# ---------------------------------------------------------------------------

config = context.config

if config.config_file_name is not None:
    # disable_existing_loggers=False is required for in-process migration runs
    # (test suite, app startup). The fileConfig default (True) disables every
    # logger instantiated before this call — including the application's own
    # loggers such as ``lmchat.middleware.logging`` — which then silently drop
    # every record for the remainder of the process. That is invisible to the
    # migration itself but corrupts any later in-process logging assertions.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# ---------------------------------------------------------------------------
# Resolve the database URL.
#
# Priority:
#   1. Whatever the caller already set via config.set_main_option()
#      (startup.py, test helpers, programmatic callers).
#   2. lmchat.config.get_settings().database_url — the app config path,
#      used when running ``alembic`` directly from the CLI.
#
# We do NOT unconditionally call get_settings() and overwrite the config
# value, because that would clobber the URL that programmatic callers
# (startup.py, tests) already set before invoking alembic.command.upgrade().
# ---------------------------------------------------------------------------

_existing_url: str | None = config.get_main_option("sqlalchemy.url")
if not _existing_url:
    from lmchat.config import get_settings as _get_settings

    config.set_main_option("sqlalchemy.url", _get_settings().database_url)


# ---------------------------------------------------------------------------
# Sync helper — called inside connection.run_sync()
# ---------------------------------------------------------------------------


def do_run_migrations(connection: object) -> None:
    """Configure and run migrations synchronously.

    This function is passed to ``connection.run_sync()`` — Alembic's
    bridge that runs a sync callable on the underlying DBAPI connection
    obtained from an ``AsyncConnection``.

    Args:
        connection: The synchronous DBAPI connection provided by
            ``run_sync``.
    """
    context.configure(
        connection=connection,  # type: ignore[arg-type]
        target_metadata=target_metadata,
        # Emit column-type diffs in future autogenerate passes.
        compare_type=True,
        # Emit server-default diffs in future autogenerate passes.
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Async online path
# ---------------------------------------------------------------------------


async def run_async_migrations() -> None:
    """Create an ``AsyncEngine`` and run migrations online.

    Uses ``async_engine_from_config`` so that any ``[sqlalchemy.*]``
    overrides in ``alembic.ini`` are honoured in addition to the URL
    resolved above.

    ``NullPool`` is used so that the engine does not hold open
    connections after the migration completes — this is the canonical
    Alembic async pattern and avoids "database is locked" surprises in
    the SQLite single-writer model.
    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


# ---------------------------------------------------------------------------
# Offline path (used by ``alembic upgrade head --sql``)
# ---------------------------------------------------------------------------


def run_migrations_offline() -> None:
    """Run migrations in offline (SQL-script) mode.

    Configures the Alembic context with the URL from the config so that
    ``alembic upgrade head --sql`` produces valid SQL without connecting
    to a live database.  This is exercised by the gate command in the
    task spec.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


# ---------------------------------------------------------------------------
# Entry-point dispatch
# ---------------------------------------------------------------------------


def run_migrations_online() -> None:
    """Entry point for online migration runs.

    Calls the async migration path via ``asyncio.run()``.  Called by
    Alembic's migration runner when not in offline mode.
    """
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
