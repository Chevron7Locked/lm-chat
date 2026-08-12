# SPDX-License-Identifier: Apache-2.0
"""Pydantic model for the ``chats.settings`` JSON column.

The ``chats.settings`` JSON blob is written by multiple clusters and a
column-rename / shape-shift would silently drift without a typed gate.
This module is that gate: read + write both go through
:class:`ChatSettings.model_validate` so any drift surfaces as a
validation error, not a stale field.

The model is **non-strict** (``extra='allow'``) so a newer client writing a
key that this server version does not validate yet is forwarded as-is — the
forward-compat invariant baked into ``chat_service.update_settings`` is
preserved.  Known keys are validated; unknown keys pass through.

Wire posture
------------
The settings JSON blob is **internal**.  HTTP clients PATCH individual fields
on :endpoint:`/api/chats/{id}` (Form-encoded) and the route layer composes a
:class:`ChatSettings` instance before merging.  The model is never serialised
to the wire directly; it lives entirely server-side.

Forward-compat invariant
------------------------
``extra='allow'`` is load-bearing: every previous addition that wrote into
this JSON blob (``reasoning_effort`` + ``rag_enabled``, then ``ab_compare``)
added a key without bumping a schema version.  Each new per-chat field
follows the same convention.  A future ``schema_version`` field can be added
non-breaking once a migration path is needed.
"""
from __future__ import annotations

from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from lmchat.logging import get_logger

_log = get_logger(__name__)

# Snapshot of declared field names for the warn-on-unknown gate. Populated
# lazily inside ``ChatSettings._warn_unknown_keys`` since BaseModel's
# ``model_fields`` is only fully populated after class creation.

# ---------------------------------------------------------------------------
# Valid reasoning levels (mirrors chat_service._VALID_REASONING)
# ---------------------------------------------------------------------------

ReasoningLevel = Literal["off", "low", "medium", "high"]

_VALID_REASONING_LEVELS: Final[frozenset[str]] = frozenset({
    "off",
    "low",
    "medium",
    "high",
})

# ---------------------------------------------------------------------------
# Bounds for sampling params
# ---------------------------------------------------------------------------

_TEMPERATURE_MIN: Final[float] = 0.0
_TEMPERATURE_MAX: Final[float] = 2.0
_TOP_P_MIN: Final[float] = 0.0
_TOP_P_MAX: Final[float] = 1.0
_MIN_P_MIN: Final[float] = 0.0
_MIN_P_MAX: Final[float] = 1.0
_TOP_K_MIN: Final[int] = 1
_REPEAT_PENALTY_MIN: Final[float] = 0.0
_REPEAT_PENALTY_MAX: Final[float] = 5.0
_MAX_TOKENS_MIN: Final[int] = 1
_REPEAT_WARNING_CUT_K_MIN: Final[int] = 0
_REPEAT_WARNING_CUT_K_MAX: Final[int] = 100


# ---------------------------------------------------------------------------
# Pre-existing settings models (preserve the original shape verbatim)
# ---------------------------------------------------------------------------


class AbCompareSettings(BaseModel):
    """A/B compare configuration nested under ``ab_compare``."""

    model_config = ConfigDict(extra="allow")

    enabled: bool
    model_a: str | None = None
    model_b: str | None = None


# ---------------------------------------------------------------------------
# Main settings model
# ---------------------------------------------------------------------------


class ChatSettings(BaseModel):
    """Typed projection of the ``chats.settings`` JSON column.

    Fields are partitioned by when they were introduced:

    Pre-existing (preserved verbatim):
        - ``reasoning_effort``: one of {"off", "low", "medium", "high"} or
          ``None``.  ``None`` clears the per-chat override; the global
          default (from :class:`~lmchat.config.Settings`) takes over.
        - ``rag_enabled``: when ``True``, the RAG retrieval pipeline runs
          before each send and injects context into the user message.
        - ``ab_compare``: nested :class:`AbCompareSettings` for the A/B
          compare two-pane view.

    Per-chat rail fields (the missing per-chat rail surface):
        - ``system_prompt``: per-chat system prompt override.  Mirrors the
          v0.5.x ``cs-sys`` textarea (``app.js:6101``).
        - ``temperature``: sampler temperature (0-2).  Mirrors ``cs-temp``.
        - ``top_p``: nucleus sampling threshold (0-1).
        - ``top_k``: top-K filter (≥1 when set).
        - ``min_p``: min-P filter (0-1).
        - ``repeat_penalty``: penalty for token repetition.
        - ``max_tokens``: max output tokens cap.
        - ``reasoning``: alias for ``reasoning_effort`` in the v0.5.x
          right-rail UI (kept distinct so the rail can present the field
          under its UI name without disturbing the reasoning-pill toggle).
          When BOTH are set, ``reasoning_effort`` wins (it's the older,
          more widely-consumed key).
        - ``self_consistency_enabled``: opt-in to SC orchestration.  When
          ``True``, the request is routed through
          :class:`~lmchat.services.quality_modes.run_self_consistency`.
        - ``chain_of_verification_enabled``: opt-in to CoVe orchestration.
        - ``stateless``: when ``True``, the LM Studio request sets
          ``store=False`` (v0.5.x ``cs-stateless``) — the upstream stops
          appending this exchange to its response chain.
        - ``active_preset``: written when a preset slash
          command is fired (``/research /code /write /analyze /architect``).
          Present in the model now for forward-compat.
        - ``repeat_warning_cut_k``: per-chat override for the tool-call
          repeat-loop cut threshold (K) consumed by
          ``streaming_service._track_loop_cut_signals``. 0-100; 0 disables
          the cut. ``None`` clears the override and falls through to the
          global admin default (``app_settings_service.
          resolve_repeat_warning_cut_k``), then the config default (16).
    """

    # extra='allow' preserves the forward-compat invariant: unknown keys
    # (whether from an older or newer client) round-trip through the model
    # without being dropped.
    model_config = ConfigDict(extra="allow")

    # Pre-existing fields ----------------------------------------------------
    reasoning_effort: ReasoningLevel | None = None
    rag_enabled: bool | None = None
    ab_compare: AbCompareSettings | None = None

    # Per-chat rail fields -----------------------------------------------
    system_prompt: str | None = None
    temperature: float | None = Field(
        default=None,
        ge=_TEMPERATURE_MIN,
        le=_TEMPERATURE_MAX,
    )
    top_p: float | None = Field(
        default=None,
        ge=_TOP_P_MIN,
        le=_TOP_P_MAX,
    )
    top_k: int | None = Field(default=None, ge=_TOP_K_MIN)
    min_p: float | None = Field(
        default=None,
        ge=_MIN_P_MIN,
        le=_MIN_P_MAX,
    )
    repeat_penalty: float | None = Field(
        default=None,
        ge=_REPEAT_PENALTY_MIN,
        le=_REPEAT_PENALTY_MAX,
    )
    max_tokens: int | None = Field(default=None, ge=_MAX_TOKENS_MIN)
    reasoning: ReasoningLevel | None = None
    # bool | None (not bool = False) so merge() can distinguish
    # "explicitly false" from "unset". With ``bool = False`` the merge always
    # injected False into the stored JSON, making it impossible to represent
    # "no per-chat override; fall through to the global default."
    self_consistency_enabled: bool | None = None
    chain_of_verification_enabled: bool | None = None
    stateless: bool | None = None
    # None = inherit the global admin default (then the config default).
    # 0 disables the tool-call repeat-loop cut for this chat.
    repeat_warning_cut_k: int | None = Field(
        default=None,
        ge=_REPEAT_WARNING_CUT_K_MIN,
        le=_REPEAT_WARNING_CUT_K_MAX,
    )

    # Forward-compat ---------------------------------------------------------
    active_preset: str | None = None

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("reasoning_effort", "reasoning", mode="before")
    @classmethod
    def _coerce_reasoning_empty_string(cls, v: object) -> object:
        """Coerce empty-string reasoning to ``None``.

        v0.5.x's right-rail dropdown emits ``""`` to mean "clear override";
        the frontend ``useUpdateChat`` hook passes it through.  Convert it
        here so the literal-type validator accepts the value.
        """
        if v == "":
            return None
        return v

    @field_validator("system_prompt", mode="before")
    @classmethod
    def _strip_system_prompt(cls, v: object) -> object:
        """Empty / whitespace-only ``system_prompt`` is normalised to ``None``."""
        if isinstance(v, str) and v.strip() == "":
            return None
        return v

    @model_validator(mode="after")
    def _warn_unknown_keys(self) -> ChatSettings:
        """Structlog WARN on any extra keys beyond the schema.

        The design specifies "validate at read + write paths, warn on
        unknown".  ``model_config = ConfigDict(extra='allow')`` preserves
        forward-compat (newer clients writing keys this server doesn't
        validate yet must round-trip), so we keep the permissive semantics —
        this validator is observability only, never rejects.

        The check compares ``self.__pydantic_extra__`` (Pydantic's stash for
        unknown keys when ``extra='allow'``) against the declared schema.
        Empty dict / None on a clean instance is the normal case.
        """
        extra = getattr(self, "__pydantic_extra__", None)
        if extra:
            _log.warning(
                "chat_settings.unknown_keys",
                unknown_keys=sorted(extra.keys()),
            )
        return self

    # ------------------------------------------------------------------
    # Convenience predicates
    # ------------------------------------------------------------------

    def merge(self, other: ChatSettings) -> ChatSettings:
        """Return ``self`` with ``other``'s non-None values overlaid.

        Shallow merge identical to the chat_service.update_settings idiom:
        ``existing | other`` where ``other``'s keys win.  Used by the route
        layer to compose the incoming PATCH payload onto the stored shape
        before persisting.
        """
        base = self.model_dump(exclude_none=True)
        overlay = other.model_dump(exclude_none=True)
        return ChatSettings.model_validate({**base, **overlay})


__all__ = [
    "AbCompareSettings",
    "ChatSettings",
    "ReasoningLevel",
]
