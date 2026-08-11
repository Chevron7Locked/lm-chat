# SPDX-License-Identifier: Apache-2.0
"""Landlock sandbox launcher for stdio MCP child processes.

Confines a spawned MCP server to a default-deny filesystem view before
exec'ing it, using the Landlock LSM (Linux 5.13+) — no root, no extra
namespaces, no additional packages required.  Grants read+exec only to
language runtimes, package caches, and scratch space; everything else
(the app's SQLite database, ``/proc/1/environ``, the app's own source
tree) becomes unreadable to the child and to anything it spawns in turn.

This module is PURE STDLIB and imports nothing from ``lmchat`` — it must
run standalone, as a subprocess entry point invoked by the same Python
interpreter running the app (``python landlock.py --allow PATH... --
CMD ARG...``), without the ``lmchat`` package needing to be importable
from wherever the child's cwd happens to be.

Usage
-----
    landlock.py [--allow PATH]... -- CMD [ARG...]

``landlock_available()`` probes for kernel support (via
``landlock_create_ruleset`` with the ABI-version query) so callers —
notably ``lmchat.mcp.host``, which imports this function directly — can
decide fail-open vs fail-closed *before* spawning anything.  When run as
``__main__`` and Landlock support is absent, sandboxing is skipped and a
warning is written to stderr rather than refusing to run the child; the
caller decides whether that's acceptable via ``LM_CHAT_MCP_REQUIRE_SANDBOX``.
"""

from __future__ import annotations

import ctypes
import os
import sys

# Landlock is Linux-only. On non-Linux platforms (esp. Windows, where
# python.exe exports no `syscall` symbol) importing this module must not
# touch libc at all, so the probe below reports "unavailable" and the
# caller falls through to the documented unsandboxed-with-warning path
# instead of crashing at import time.
libc: ctypes.CDLL | None
if sys.platform == "linux":
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
else:
    libc = None

# Landlock syscall numbers (x86_64/arm64; stable since their introduction in
# Linux 5.13 — see include/uapi/asm-generic/unistd.h upstream).
_NR_CREATE_RULESET = 444
_NR_ADD_RULE = 445
_NR_RESTRICT_SELF = 446
_PR_SET_NO_NEW_PRIVS = 38
_RULE_PATH_BENEATH = 1

_O_PATH = getattr(os, "O_PATH", 0o10000000)

_EXECUTE = 1 << 0
_WRITE_FILE = 1 << 1
_READ_FILE = 1 << 2
_READ_DIR = 1 << 3

#: Access rights this ruleset governs — confidentiality focus: reads + exec.
#: (Not WRITE_FILE: MCP servers legitimately write scratch/cache files.)
_HANDLED = _EXECUTE | _READ_FILE | _READ_DIR
#: Access rights granted on each allow-listed path.
_GRANT = _EXECUTE | _READ_FILE | _READ_DIR

#: Paths every sandboxed child gets, regardless of ``--allow``: language
#: runtimes, package caches, and scratch space needed to cold-fetch and run
#: npx/uvx-style servers.
DEFAULT_ALLOW = [
    "/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc", "/opt",
    "/home/nonroot/.npm", "/home/nonroot/.cache", "/home/nonroot/.local",
    "/tmp",  # nosec B108 - intentional: MCP children get /tmp scratch space.
    "/dev",
]


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _layout_ = "ms"  # avoids the ctypes struct-layout deprecation warning
    _fields_ = [("allowed_access", ctypes.c_uint64), ("parent_fd", ctypes.c_int32)]


def landlock_available() -> bool:
    """Probe kernel Landlock support via the ABI-version query.

    Calling ``landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)``
    returns the supported ABI version (``>= 1``) when the running kernel has
    Landlock compiled in and enabled; a negative return (errno set) means
    it's unavailable — an old kernel, disabled at boot, or a non-Linux OS.
    Safe to call on any platform: an unrecognised syscall number returns
    ``-1`` rather than crashing.
    """
    if libc is None:
        return False
    version = libc.syscall(
        ctypes.c_long(_NR_CREATE_RULESET), None, ctypes.c_size_t(0), ctypes.c_uint(1)
    )
    return version >= 1


def apply(allow: list[str]) -> None:
    """Apply a default-deny Landlock ruleset granting read+exec on *allow*.

    Must be called before ``os.execvp`` — Landlock restrictions attach to
    the calling process and are inherited by everything it execs afterward,
    but (by design) can never be lifted once applied.

    Args:
        allow: Filesystem paths to grant read+exec access to. Paths that
            don't exist on this system are silently skipped.

    Raises:
        OSError: If ruleset creation, rule addition, or self-restriction
            fails (e.g. Landlock unavailable — check with
            ``landlock_available()`` first).
    """
    # Documented precondition: callers check landlock_available() first,
    # which is False (libc is None) on every non-Linux platform — so libc
    # is guaranteed non-None here. Narrows for pyright.
    assert libc is not None
    attr = _RulesetAttr(_HANDLED)
    ruleset_fd = libc.syscall(
        ctypes.c_long(_NR_CREATE_RULESET),
        ctypes.byref(attr),
        ctypes.c_size_t(8),
        ctypes.c_uint(0),
    )
    if ruleset_fd < 0:
        raise OSError(ctypes.get_errno(), "landlock_create_ruleset")

    for path in allow:
        try:
            fd = os.open(path, _O_PATH | os.O_CLOEXEC)
        except OSError:
            continue  # path doesn't exist here — skip, don't fail the whole ruleset
        rule = _PathBeneathAttr(_GRANT, fd)
        rc = libc.syscall(
            ctypes.c_long(_NR_ADD_RULE),
            ctypes.c_int(ruleset_fd),
            ctypes.c_int(_RULE_PATH_BENEATH),
            ctypes.byref(rule),
            ctypes.c_uint(0),
        )
        os.close(fd)
        if rc != 0:
            raise OSError(ctypes.get_errno(), f"landlock_add_rule {path}")

    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "prctl NO_NEW_PRIVS")
    restrict_rc = libc.syscall(
        ctypes.c_long(_NR_RESTRICT_SELF), ctypes.c_int(ruleset_fd), ctypes.c_uint(0)
    )
    if restrict_rc != 0:
        raise OSError(ctypes.get_errno(), "landlock_restrict_self")


def main(argv: list[str]) -> int:
    """Parse ``--allow PATH... -- CMD ARG...``, sandbox, then exec CMD.

    Applies the Landlock ruleset (default allow-list plus any ``--allow``
    paths) when available, then replaces this process's image with CMD via
    ``os.execvp`` — CMD inherits the restriction and cannot escape it.
    Falls back to an unsandboxed exec (with a stderr warning) when Landlock
    is unavailable, so the caller decides fail-open vs fail-closed.
    """
    allow = list(DEFAULT_ALLOW)
    i = 1
    while i < len(argv) and argv[i] != "--":
        if argv[i] == "--allow":
            allow.append(argv[i + 1])
            i += 2
        else:
            i += 1

    if i >= len(argv) or argv[i] != "--":
        sys.stderr.write("landlock: missing -- CMD\n")
        return 2
    cmd = argv[i + 1 :]
    if not cmd:
        sys.stderr.write("landlock: empty CMD\n")
        return 2

    if landlock_available():
        apply(allow)
    else:
        sys.stderr.write("landlock: WARNING Landlock unavailable — running UNSANDBOXED\n")

    os.execvp(cmd[0], cmd)  # never returns on success


if __name__ == "__main__":
    sys.exit(main(sys.argv))
