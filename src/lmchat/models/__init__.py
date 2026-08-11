# SPDX-License-Identifier: Apache-2.0
"""Shared Pydantic models for lm-chat.

Distinct from ``lmchat.services`` (which defines service-level dataclass-style
Pydantic models with business semantics) and ``lmchat.lmstudio.types`` (wire
shapes for the LM Studio API).  This package holds models that describe
**stored** JSON shapes — currently the per-chat ``settings`` blob.
"""
from lmchat.models.chat_settings import ChatSettings

__all__ = ["ChatSettings"]
