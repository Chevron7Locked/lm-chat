# SPDX-License-Identifier: Apache-2.0
"""Contract tests for ``lmchat.logging``.

Covers logging setup:
- Dual-renderer JSON (file) + Console (stdout/stderr).
- Processor chain: merge_contextvars + add_log_level +
  CallsiteParameterAdder + TimeStamper(iso, utc) +
  dict_tracebacks + StackInfoRenderer.
- Idempotent configure_logging (multiple calls safe).
- request_id (or any contextvars-bound key) propagates to
  every log line emitted within the scope.

These tests are the lock contract — every implementation must
make them green without modifying the test logic.
"""
from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path

import structlog.contextvars as cvars

from lmchat.logging import configure_logging, get_logger


def _flush_root() -> None:
    for h in logging.getLogger().handlers:
        h.flush()


def _read_json_lines(log_file: Path) -> list[dict[str, object]]:
    text = log_file.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def test_configure_logging_returns_none() -> None:
    """Public contract: configure_logging returns None."""
    assert configure_logging(console=False, log_file=None) is None


def test_configure_logging_is_idempotent(tmp_path: Path) -> None:
    """Calling configure_logging multiple times must not duplicate handlers."""
    log_file = tmp_path / "lmchat.log"
    configure_logging(console=False, log_file=log_file)
    count_after_first = len(logging.getLogger().handlers)
    configure_logging(console=False, log_file=log_file)
    count_after_second = len(logging.getLogger().handlers)
    assert count_after_first == count_after_second == 1


def test_get_logger_returns_usable_bound_logger(tmp_path: Path) -> None:
    """get_logger returns a structlog-bound logger with info + bind methods."""
    configure_logging(console=False, log_file=tmp_path / "x.log")
    log = get_logger(__name__)
    # Smoke: usable without raising
    log.info("smoke")
    bound = log.bind(extra="bound")
    bound.info("with_bind")


def test_file_handler_emits_json_with_iso_timestamp_and_level(tmp_path: Path) -> None:
    """File handler writes JSON lines with timestamp + level + event."""
    log_file = tmp_path / "test.log"
    configure_logging(console=False, log_file=log_file)
    log = get_logger(__name__)

    log.info("test_event", custom_field="hello")
    _flush_root()

    lines = _read_json_lines(log_file)
    assert len(lines) >= 1
    entry = next(line for line in lines if line.get("event") == "test_event")
    assert entry["custom_field"] == "hello"
    # ISO-8601 UTC timestamp present
    timestamp = entry.get("timestamp")
    assert isinstance(timestamp, str)
    assert "T" in timestamp  # ISO-8601 separator
    # add_log_level adds level
    assert entry.get("level") == "info"


def test_contextvars_propagate_to_log_lines(tmp_path: Path) -> None:
    """request_id bound via contextvars appears in log lines emitted in scope."""
    log_file = tmp_path / "test.log"
    configure_logging(console=False, log_file=log_file)
    log = get_logger(__name__)

    cvars.bind_contextvars(request_id="rid-abc123")
    try:
        log.info("inside_request")
    finally:
        cvars.clear_contextvars()

    log.info("outside_request")
    _flush_root()

    lines_by_event = {line["event"]: line for line in _read_json_lines(log_file)}
    assert lines_by_event["inside_request"].get("request_id") == "rid-abc123"
    assert "request_id" not in lines_by_event["outside_request"]


def test_log_level_filters_below_threshold(tmp_path: Path) -> None:
    """level='WARNING' suppresses info-level logs."""
    log_file = tmp_path / "test.log"
    configure_logging(level="WARNING", console=False, log_file=log_file)
    log = get_logger(__name__)

    log.info("filtered")
    log.warning("emitted")
    _flush_root()

    content = log_file.read_text(encoding="utf-8")
    assert "filtered" not in content
    assert "emitted" in content


def test_exception_traceback_is_structured(tmp_path: Path) -> None:
    """log.exception() produces structured exception fields (dict_tracebacks)."""
    log_file = tmp_path / "test.log"
    configure_logging(console=False, log_file=log_file)
    log = get_logger(__name__)

    try:
        raise ValueError("synthetic_boom")
    except ValueError:
        log.exception("explosion")
    _flush_root()

    lines = _read_json_lines(log_file)
    entry = next(line for line in lines if line.get("event") == "explosion")
    # dict_tracebacks writes the exception as a structured key.
    # Implementations differ in key name — accept either canonical form.
    assert "exception" in entry or "exc_info" in entry
    # The synthetic error message must appear somewhere in the structured payload.
    blob = json.dumps(entry)
    assert "synthetic_boom" in blob


def test_callsite_info_recorded(tmp_path: Path) -> None:
    """CallsiteParameterAdder records the call site (filename, lineno, func_name)."""
    log_file = tmp_path / "test.log"
    configure_logging(console=False, log_file=log_file)
    log = get_logger(__name__)
    log.info("callsite_check")
    _flush_root()

    entry = next(
        line for line in _read_json_lines(log_file) if line.get("event") == "callsite_check"
    )
    # Filenames are normalized by structlog.
    assert "filename" in entry
    assert "lineno" in entry
    assert "func_name" in entry


def test_log_directory_created_when_missing(tmp_path: Path) -> None:
    """configure_logging creates the log directory if it doesn't exist."""
    nested = tmp_path / "deep" / "dir" / "logs" / "app.log"
    assert not nested.parent.exists()
    configure_logging(console=False, log_file=nested)
    assert nested.parent.exists()


def test_none_log_file_means_no_file_handler() -> None:
    """log_file=None attaches no file handler (useful for tests)."""
    configure_logging(console=False, log_file=None)
    handlers = logging.getLogger().handlers
    # No handlers configured (console=False, log_file=None means a no-op
    # except for level setting).
    assert handlers == []


def test_console_handler_attached_when_console_true(tmp_path: Path) -> None:
    """console=True attaches a stderr StreamHandler; logs do not crash."""
    configure_logging(console=True, log_file=tmp_path / "c.log")
    handlers = logging.getLogger().handlers
    assert len(handlers) == 2  # one console + one rotating-file
    stream_handlers = [h for h in handlers if isinstance(h, logging.StreamHandler)
                       and not isinstance(h, logging.handlers.RotatingFileHandler)]
    assert len(stream_handlers) == 1
    # Smoke: emitting through the console renderer must not raise
    get_logger(__name__).info("console_smoke")


def test_foreign_stdlib_log_no_attribute_error(tmp_path: Path) -> None:
    """Foreign stdlib log records (e.g. from httpx) must not raise AttributeError.

    Regression guard for the boot-time bug where ProcessorFormatter passed
    logger=None to filter_by_level, which then called not logger.disabled and
    raised AttributeError: 'NoneType' object has no attribute 'disabled'.

    The fix: _safe_filter_by_level skips level filtering when logger is None
    (foreign record path — stdlib has already filtered at the origin handler).
    """
    from structlog.stdlib import ProcessorFormatter

    from lmchat.logging import _SHARED_PROCESSORS

    # Build a minimal ProcessorFormatter (no explicit logger → self.logger=None,
    # which is the path that triggers the bug) with the shared processor chain.
    # The final processor must return a string; use a lambda for the test.
    formatter = ProcessorFormatter(
        foreign_pre_chain=list(_SHARED_PROCESSORS),
        processors=[
            ProcessorFormatter.remove_processors_meta,
            lambda _logger, _method, event_dict: str(event_dict),
        ],
    )

    # Construct a synthetic stdlib LogRecord as httpx would produce.
    record = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="GET http://localhost:1234/api/v1/models HTTP/1.1 200 OK",
        args=(),
        exc_info=None,
    )

    # Must not raise AttributeError: 'NoneType' object has no attribute 'disabled'.
    try:
        formatter.format(record)
    except AttributeError as exc:
        raise AssertionError(
            f"Foreign stdlib log raised AttributeError (bug regression): {exc}"
        ) from exc
