# SPDX-License-Identifier: Apache-2.0
"""Cross-platform import-safety tests for ``lmchat.mcp.landlock``.

Regression test for a Windows-only crash: the module used to run
``ctypes.CDLL(None, use_errno=True)`` and ``libc.syscall.restype = ...`` at
MODULE IMPORT time, unconditionally. On Windows, ``python.exe`` exports no
``syscall`` symbol, so ``libc.syscall`` raised ``AttributeError`` at import —
crashing the whole app (this module sits in the mandatory import chain via
``mcp/host.py`` -> ``app.py``), contradicting the module's own docstring
("Safe to call on any platform ... rather than crashing") and README.md's
documented fallback behaviour (unsandboxed-with-warning on non-Linux).

The actual Windows crash can't be reproduced on Linux/Mac CI — it depends on
symbols the real OS's libc exports. Instead, this test exercises the GUARD
LOGIC that prevents the crash: with ``sys.platform`` mocked to ``"win32"``,
importing/reloading the module must not touch ``libc`` at all.
"""

from __future__ import annotations

import importlib
import sys

import pytest

import lmchat.mcp.landlock as landlock_module


@pytest.fixture
def _restore_landlock_module():
    """Reload the real module afterward so other tests see its true state.

    Leaving the module reloaded-as-"win32" (libc=None) after this test would
    poison every later test/import in the same process — e.g. any test that
    relies on ``landlock_available()`` reflecting the real platform.
    """
    try:
        yield
    finally:
        importlib.reload(landlock_module)


def test_import_on_windows_does_not_touch_libc(monkeypatch, _restore_landlock_module):
    """Reloading under a mocked win32 platform must not raise, and must
    leave libc unset (None) rather than attempting any ctypes.CDLL syscall
    binding — this is what prevents the AttributeError Windows import crash.
    """
    monkeypatch.setattr(sys, "platform", "win32")

    # Must not raise (this is the crash we're guarding against).
    importlib.reload(landlock_module)

    assert landlock_module.libc is None
    assert landlock_module.landlock_available() is False
