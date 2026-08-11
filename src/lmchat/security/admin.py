# SPDX-License-Identifier: Apache-2.0
"""Admin authorization gate for lm-chat.

Decision: ``_dependencies.py`` already ships a complete
``require_admin`` implementation that:
  - wraps ``require_user`` (so unauthenticated callers get 401, not 403)
  - checks ``User.is_admin``
  - raises HTTP 403 on failure

Rather than duplicate or shadow that implementation, this module re-exports
it as the canonical ``require_admin`` symbol.  Route modules import from
here (``from lmchat.security.admin import require_admin``) so the import
path is stable when this module later replaces the body with the full
middleware-integrated version that adds security-headers and audit-log
entries.

Migration path:
  1. Write the enhanced implementation in this file.
  2. Update ``_dependencies.py`` to re-export from here (reversing the
     direction) or keep both pointing at the same implementation.
  3. No route imports change — all routes already use
     ``from lmchat.security.admin import require_admin``.
"""
from __future__ import annotations

from lmchat.routes._dependencies import require_admin as require_admin

__all__ = ["require_admin"]
