# SPDX-License-Identifier: Apache-2.0
"""Provider seam — Workstream A1 of the multi-provider foundation.

The ``ChatProvider`` Protocol defined in ``base`` is the single contract
every provider implementation satisfies.  Import from this package for
the stable public surface; import from ``base`` directly when you need
the sanitization helper.
"""
from lmchat.providers.base import (
    ChatProvider,
    ContextMode,
    sanitize_request_for_provider,
)
from lmchat.providers.openai_compat import OpenAICompatProvider

__all__ = [
    "ChatProvider",
    "ContextMode",
    "OpenAICompatProvider",
    "sanitize_request_for_provider",
]
