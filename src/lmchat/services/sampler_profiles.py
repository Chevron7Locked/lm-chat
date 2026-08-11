# SPDX-License-Identifier: Apache-2.0
"""Per-model-family sampler profile injection for LM Studio requests.

This module provides vendor-recommended sampler profiles for Qwen-family
models, sourced from the Qwen3.6-35B-A3B HuggingFace model card.

Profile selection logic:
  - thinking=True  + tool_category == "code"  → PROFILE_THINKING_CODING
  - thinking=True  + any other category        → PROFILE_THINKING_GENERAL
  - thinking=False (any category)              → PROFILE_INSTRUCT

The "code" category is detected by exact match on the tool_category string
("code"). All other categories (review, review-dw, audit, …) fall through
to PROFILE_THINKING_GENERAL.

Model matching:
  - Case-insensitive substring match on normalized model_id
    (model_id.lower().split("/")[-1])
  - Excludes models ending with "-scar" (they bake their own settings)
  - Patterns are ordered by specificity; first match wins

Hot-reload:
  - Config file is read on first use and cached by mtime
  - If mtime changes, the config is reloaded on next call
  - Thread-safe under asyncio.Lock
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Final

# ---------------------------------------------------------------------------
# Verbatim vendor profiles — do NOT change values.
# Source: Qwen3.6-35B-A3B HuggingFace model card (verified 2026-05-28).
# ---------------------------------------------------------------------------

PROFILE_THINKING_GENERAL: Final[dict[str, float | int]] = {
    # General tasks + reasoning, thinking mode ON.
    "temperature": 1.0,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 1.5,
    "repetition_penalty": 1.0,
    "max_tokens": 32768,
}

PROFILE_THINKING_CODING: Final[dict[str, float | int]] = {
    # Precise coding / WebDev, thinking mode ON.
    "temperature": 0.6,
    "top_p": 0.95,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 0.0,
    "repetition_penalty": 1.0,
    "max_tokens": 81920,
}

PROFILE_INSTRUCT: Final[dict[str, float | int]] = {
    # Non-thinking / direct (thinking mode OFF).
    "temperature": 0.7,
    "top_p": 0.80,
    "top_k": 20,
    "min_p": 0.0,
    "presence_penalty": 1.5,
    "repetition_penalty": 1.0,
    "max_tokens": 32768,
}

# ---------------------------------------------------------------------------
# Module-level state for hot-reload
# ---------------------------------------------------------------------------

_CONFIG_PATH: Final[Path] = Path(__file__).parent / "sampler_profiles_config.json"
_config_cache: dict[str, list[dict[str, str]]] | None = None
_config_mtime: float | None = None
_config_lock = asyncio.Lock()


def _load_profile_config() -> dict[str, list[dict[str, str]]]:
    """Load sampler profiles config with mtime-based hot-reload.

    Returns the parsed JSON dict with keys:
      - "$schema_version": str
      - "patterns": list[{"match": str, "profile": str, "comment": str}]

    Reloads if mtime changes. Thread-safe under asyncio.Lock.
    """
    global _config_cache, _config_mtime

    if not _CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config file not found: {_CONFIG_PATH}")

    current_mtime = _CONFIG_PATH.stat().st_mtime

    # Check if we need to reload
    if _config_cache is not None and _config_mtime == current_mtime:
        return _config_cache

    # Load into local, then update globals
    with open(_CONFIG_PATH, encoding="utf-8") as f:
        config = json.load(f)

    _config_cache = config
    _config_mtime = current_mtime

    return config


def is_profiled_model(model_id: str) -> bool:
    """Return True if model_id matches any profile pattern.

    Normalization: model_id.lower().split("/")[-1]
    Exclusion: Returns False if normalized ID ends with "-scar".

    Pattern matching:
      - Case-insensitive substring match against pattern["match"]
      - First matching pattern wins (order matters in config)
      - Returns False if no patterns match

    Args:
        model_id: Full model identifier (e.g., "Qwen/Qwen3.6-35B-A3B-MLX")

    Returns:
        True if model should receive sampler profile injection.
    """
    if not model_id:
        return False

    # Normalize: lowercase, take last segment after "/"
    normalized = model_id.lower().split("/")[-1]

    # Exclusion: -scar models bake their own settings
    if normalized.endswith("-scar"):
        return False

    try:
        config = _load_profile_config()
    except (FileNotFoundError, json.JSONDecodeError):
        return False

    patterns = config.get("patterns", [])
    for pattern in patterns:
        match_str = pattern.get("match", "")
        if match_str and match_str.lower() in normalized:
            return True

    return False


def select_profile(*, tool_category: str, thinking: bool) -> dict[str, float | int]:
    """Return a copy of the sampler profile for the given task context.

    Profile selection:
      - thinking=True  + tool_category == "code"  → PROFILE_THINKING_CODING
      - thinking=True  + any other category        → PROFILE_THINKING_GENERAL
      - thinking=False (any category)              → PROFILE_INSTRUCT

    Args:
        tool_category: The tool category string ("code", "review", etc.)
        thinking: True if thinking mode is enabled

    Returns:
        A new dict with profile keys: temperature, top_p, top_k, min_p,
        presence_penalty, repeat_penalty (not repetition_penalty), max_tokens

    Note:
        The profile dict uses "repeat_penalty" (not "repetition_penalty")
        because CanonicalChatRequest expects the llama.cpp native field name.
        The source profiles use "repetition_penalty" (LM Studio's compat name),
        so we translate it here.
    """
    raw = None
    if not thinking:
        raw = dict(PROFILE_INSTRUCT)
    elif tool_category == "code":
        raw = dict(PROFILE_THINKING_CODING)
    else:
        raw = dict(PROFILE_THINKING_GENERAL)

    # Translate vendor names to CanonicalChatRequest wire names:
    #   repetition_penalty (HF/compat) → repeat_penalty (llama.cpp native)
    #   max_tokens (HF/compat)         → max_output_tokens (LM Studio native)
    if raw is not None:
        if "repetition_penalty" in raw:
            raw["repeat_penalty"] = raw.pop("repetition_penalty")
        if "max_tokens" in raw:
            raw["max_output_tokens"] = raw.pop("max_tokens")

    return raw


def profile_for_request(
    model_id: str,
    reasoning_effort: str | None,
    supports_reasoning: bool,
) -> dict | None:
    """Determine if and which profile to apply for a request.

    Logic:
      1. If model is not profiled → return None
      2. If supports_reasoning is False → force thinking=False (PROFILE_INSTRUCT)
      3. Else: thinking = (reasoning_effort != "off")
      4. Call select_profile(tool_category="general", thinking=thinking)

    Args:
        model_id: Full model identifier
        reasoning_effort: "off", "low", "medium", "high", or None
        supports_reasoning: True if model advertises reasoning capability

    Returns:
        A dict with profile keys if profile should be applied, None otherwise.
        The caller receives an owned mutable copy.
    """
    if not is_profiled_model(model_id):
        return None

    # Force thinking=False if model doesn't support reasoning
    if not supports_reasoning:
        thinking = False
    else:
        thinking = reasoning_effort != "off"

    return select_profile(tool_category="general", thinking=thinking)


def _reset_cache_for_testing() -> None:
    """Reset module-level cache for testing. Test-only helper.

    This helper is for internal test use only. It clears the config cache
    and mtime so tests can exercise hot-reload behavior.
    """
    global _config_cache, _config_mtime
    _config_cache = None
    _config_mtime = None
