"""Subprocess coverage startup hook.

Python imports ``sitecustomize`` once during interpreter init, right after the
stdlib ``site`` module has set up ``sys.path``.  pytest fixtures put this
directory on ``PYTHONPATH`` and set ``COVERAGE_PROCESS_START``, which is what
the coverage docs say is enough to capture subprocess coverage.

Under Homebrew Python on macOS that recipe is silently incomplete: the
relocated ``site-packages`` (``/opt/homebrew/lib/python<ver>/site-packages``)
is added to ``sys.path`` *after* ``sitecustomize`` runs, so ``import coverage``
fails here even though it succeeds for application code that runs later.  The
``try/except ImportError`` then swallows the error and ``server.py`` coverage
silently reports 0% in CI.

We work around it by force-prepending every site-packages dir we can discover
through the Python install layout, before importing coverage.  All paths are
checked for existence so this stays safe on Linux/CI runners that don't have
Homebrew's relocations.
"""

import os
import sys
import sysconfig


def _candidate_site_packages():
    """All site-packages dirs that ``site.main()`` may add later in init.

    We can't call ``site.getsitepackages()`` reliably because Homebrew patches
    only kick in once ``site.main()`` has finished — and ``sitecustomize`` runs
    in the middle of that.  Reconstruct the canonical paths from the Python
    install layout instead.
    """
    ver = f"python{sys.version_info.major}.{sys.version_info.minor}"
    paths: list[str] = []

    # sysconfig knows the install scheme even mid-init
    try:
        paths.append(sysconfig.get_paths()["purelib"])
        paths.append(sysconfig.get_paths()["platlib"])
    except Exception:
        pass

    # macOS Homebrew (Apple Silicon and Intel) relocates site-packages here
    paths.append(f"/opt/homebrew/lib/{ver}/site-packages")
    paths.append(f"/usr/local/lib/{ver}/site-packages")

    # Debian/Ubuntu dist-packages convention
    paths.append(f"/usr/lib/{ver}/dist-packages")
    paths.append(f"/usr/local/lib/{ver}/dist-packages")

    return paths


for _p in _candidate_site_packages():
    if _p and os.path.isdir(_p) and _p not in sys.path:
        sys.path.insert(0, _p)

try:
    import coverage
    coverage.process_startup()
except ImportError:
    # No coverage installed — that's fine, this is dev-only tooling.
    pass
