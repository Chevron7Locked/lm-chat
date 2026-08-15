# SPDX-License-Identifier: Apache-2.0
"""Tests for ``lmchat.config.Settings`` — local-first timeout defaults.

Local models are naturally slow; background tasks that nothing waits on must
not be cut on a cloud-latency number. These guard the generous local-first
defaults for the aux-model and MCP tool-call timeout knobs so nobody quietly
restores a cloud-sized budget.
"""
from __future__ import annotations

from lmchat.config import Settings

_SECRET = "test-secret-32-bytes-of-entropy!!"


def test_aux_model_timeout_defaults_generous() -> None:
    """Fire-and-forget background aux calls (auto-title, compaction summary,
    auto-memory distillation, follow-up chips) get a generous default budget
    since nothing waits on them."""
    settings = Settings(lm_chat_secret=_SECRET)  # type: ignore[call-arg]
    assert settings.lm_chat_aux_model_timeout_sec == 900.0


def test_mcp_tool_call_timeout_defaults_local_first() -> None:
    """MCP tool calls are on the turn path, so the budget is aligned with
    ``lm_chat_stream_idle_timeout_sec`` rather than a short cloud-latency
    number."""
    settings = Settings(lm_chat_secret=_SECRET)  # type: ignore[call-arg]
    assert settings.lm_chat_mcp_tool_call_timeout_sec == 300.0
    assert (
        settings.lm_chat_mcp_tool_call_timeout_sec
        == settings.lm_chat_stream_idle_timeout_sec
    )


def test_mode_adoption_defaults_off() -> None:
    """C3 (model-decided role adoption) is opt-in, unlike follow-up chips.

    Adopting a mode changes the persona + temperature of the user's NEXT
    message — a bigger behavioral change than an additive chip row — so it
    stays off until an admin turns it on, same posture as
    ``lm_chat_default_integrations_enabled_by_default``.
    """
    settings = Settings(lm_chat_secret=_SECRET)  # type: ignore[call-arg]
    assert settings.lm_chat_mode_adoption_enabled is False
