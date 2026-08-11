# SPDX-License-Identifier: Apache-2.0
"""Structured logging configuration for lm-chat.

This module configures structlog with a dual-renderer pipeline:

1. A *shared* processor chain runs once per event, in the
   structlog 25.x canonical order:
   merge_contextvars → filter_by_level → add_log_level
   → CallsiteParameterAdder → TimeStamper(iso, utc)
   → dict_tracebacks → StackInfoRenderer.
   merge_contextvars runs first so contextvar state is
   visible to filter logic; filter_by_level short-circuits
   below-threshold events before the expensive processors
   (callsite lookup, traceback dict construction) run.
2. structlog hands the resulting event dict off to stdlib via
   ``ProcessorFormatter.wrap_for_formatter`` so a per-handler
   renderer can dispatch.
3. The file handler (RotatingFileHandler, 10 MB × 5 backups)
   renders JSON. The console handler renders human-readable
   output to stderr.

Both handlers attach to the root logger; foreign (non-structlog)
log records are converted to event dicts via
``foreign_pre_chain``.

Public surface:
    ``configure_logging(level=..., log_file=..., console=...)``
    ``get_logger(name) -> structlog.stdlib.BoundLogger``

Idempotent: calling ``configure_logging`` multiple times
replaces handlers cleanly rather than accumulating them.
"""
from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Final, cast

import structlog
from structlog.contextvars import merge_contextvars
from structlog.dev import ConsoleRenderer
from structlog.processors import (
    CallsiteParameter,
    CallsiteParameterAdder,
    JSONRenderer,
    StackInfoRenderer,
    TimeStamper,
    add_log_level,
    dict_tracebacks,
)
from structlog.stdlib import BoundLogger, LoggerFactory, ProcessorFormatter
from structlog.typing import EventDict, Processor

_DEFAULT_LOG_FILE: Final[Path] = Path("logs") / "lmchat.log"
_DEFAULT_LOG_MAX_BYTES: Final[int] = 10_000_000
_DEFAULT_LOG_BACKUP_COUNT: Final[int] = 5

# NAME_TO_LEVEL mapping from structlog.stdlib (reproduced locally to avoid
# importing the private mapping).
_NAME_TO_LEVEL: dict[str, int] = {
    "critical": logging.CRITICAL,
    "exception": logging.ERROR,
    "error": logging.ERROR,
    "warn": logging.WARNING,
    "warning": logging.WARNING,
    "info": logging.INFO,
    "debug": logging.DEBUG,
    "notset": logging.NOTSET,
}


def _safe_filter_by_level(
    logger: logging.Logger | None,
    method_name: str,
    event_dict: EventDict,
) -> EventDict:
    """Null-safe replacement for ``structlog.stdlib.filter_by_level``.

    ``ProcessorFormatter`` passes ``logger=None`` for foreign (non-structlog)
    log records when no explicit ``logger`` is provided to the formatter.
    The upstream ``filter_by_level`` calls ``not logger.disabled`` and raises
    ``AttributeError: 'NoneType' object has no attribute 'disabled'``.

    When ``logger`` is ``None`` (foreign record path), skip level filtering —
    the stdlib ``LogRecord`` has already been filtered at its origin handler.
    When ``logger`` is a real ``logging.Logger``, apply the normal level gate.
    """
    if logger is None:
        return event_dict
    level = _NAME_TO_LEVEL.get(method_name, logging.DEBUG)
    if not logger.disabled and level >= logger.getEffectiveLevel():
        return event_dict
    raise structlog.DropEvent


# Shared chain — runs for every event before rendering. The terminal
# step (``ProcessorFormatter.wrap_for_formatter``) only applies to
# structlog-originated events; ``foreign_pre_chain`` on the per-handler
# formatter runs this same list for stdlib-originated foreign records,
# guaranteeing one event-dict shape regardless of origin.
_SHARED_PROCESSORS: Final[list[Processor]] = [
    merge_contextvars,
    _safe_filter_by_level,
    add_log_level,
    CallsiteParameterAdder(
        parameters=[
            CallsiteParameter.FILENAME,
            CallsiteParameter.LINENO,
            CallsiteParameter.FUNC_NAME,
        ],
    ),
    TimeStamper(fmt="iso", utc=True),
    dict_tracebacks,
    StackInfoRenderer(),
]


def configure_logging(
    *,
    level: str | None = None,
    log_file: Path | str | None = _DEFAULT_LOG_FILE,
    console: bool = True,
) -> None:
    """Configure structlog + stdlib logging.

    Args:
        level: Log level name (``DEBUG`` / ``INFO`` / ``WARNING``
            / ``ERROR``). Defaults to ``INFO`` when ``None``.
        log_file: Path to the JSON rotating file. ``None`` skips
            the file handler entirely (useful for tests).
        console: When ``True`` attach a stderr console handler
            with human-readable rendering.

    The call is **idempotent** — any prior handlers attached to
    the root logger are removed before the new ones are
    installed, so calling this multiple times never accumulates
    duplicates.
    """
    root = logging.getLogger()

    # Strip prior handlers — idempotency contract. Close them so
    # any file descriptors are released before the same path is
    # reopened.
    for handler in list(root.handlers):
        try:
            handler.flush()
            handler.close()
        finally:
            root.removeHandler(handler)

    resolved_level = (level or "INFO").upper()
    root.setLevel(getattr(logging, resolved_level, logging.INFO))

    structlog.configure(
        processors=[*_SHARED_PROCESSORS, ProcessorFormatter.wrap_for_formatter],
        logger_factory=LoggerFactory(),
        wrapper_class=BoundLogger,
        cache_logger_on_first_use=True,
    )

    if log_file is not None:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.handlers.RotatingFileHandler(
            log_path,
            maxBytes=_DEFAULT_LOG_MAX_BYTES,
            backupCount=_DEFAULT_LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(
            ProcessorFormatter(
                foreign_pre_chain=_SHARED_PROCESSORS,
                processors=[
                    ProcessorFormatter.remove_processors_meta,
                    JSONRenderer(),
                ],
            ),
        )
        root.addHandler(file_handler)

    if console:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setFormatter(
            ProcessorFormatter(
                foreign_pre_chain=_SHARED_PROCESSORS,
                processors=[
                    ProcessorFormatter.remove_processors_meta,
                    ConsoleRenderer(colors=sys.stderr.isatty()),
                ],
            ),
        )
        root.addHandler(console_handler)


def get_logger(name: str) -> BoundLogger:
    """Return a structlog ``BoundLogger`` bound to ``name``.

    Callers should pass ``__name__`` so the callsite-recorded
    module aligns with module identity. If
    :func:`configure_logging` has not been called yet, structlog
    falls back to its built-in defaults — but every code path in
    lm-chat invokes ``configure_logging`` from the FastAPI
    lifespan before any application log is emitted.
    """
    return cast(BoundLogger, structlog.get_logger(name))
