# SPDX-License-Identifier: Apache-2.0
"""Unit tests for ``lmchat.models.chat_settings.ChatSettings`` (P13a R-13).

Covers the validation gate the cluster A PLAN spec calls out:
- reasoning levels (incl. empty-string coerce → None)
- temperature bounds (0-2)
- top_p / min_p bounds (0-1)
- top_k / max_tokens lower bounds (≥1)
- repeat_penalty bounds (0-5)
- system_prompt whitespace-only → None
- ab_compare nested shape
- forward-compat: unknown keys round-trip via ``extra='allow'``
- ``merge()`` semantics for overlay-with-priority
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from lmchat.models.chat_settings import AbCompareSettings, ChatSettings


class TestReasoningLevel:
    def test_accepts_each_valid_level(self) -> None:
        for level in ("off", "low", "medium", "high"):
            s = ChatSettings(reasoning_effort=level)  # type: ignore[arg-type]
            assert s.reasoning_effort == level

    def test_empty_string_coerced_to_none(self) -> None:
        s = ChatSettings.model_validate({"reasoning_effort": ""})
        assert s.reasoning_effort is None

    def test_invalid_level_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChatSettings.model_validate({"reasoning_effort": "extreme"})

    def test_reasoning_alias_field_independent(self) -> None:
        # Both keys are stored independently — UI surfaces use the alias
        # but the existing reasoning_effort key keeps its older consumers.
        s = ChatSettings.model_validate(
            {"reasoning_effort": "high", "reasoning": "low"}
        )
        assert s.reasoning_effort == "high"
        assert s.reasoning == "low"


class TestTemperature:
    @pytest.mark.parametrize("value", [0.0, 0.5, 1.0, 1.5, 2.0])
    def test_in_range(self, value: float) -> None:
        s = ChatSettings(temperature=value)
        assert s.temperature == value

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChatSettings(temperature=-0.1)

    def test_over_two_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChatSettings(temperature=2.5)


class TestTopP:
    @pytest.mark.parametrize("value", [0.0, 0.3, 0.95, 1.0])
    def test_in_range(self, value: float) -> None:
        s = ChatSettings(top_p=value)
        assert s.top_p == value

    def test_over_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChatSettings(top_p=1.2)


class TestTopK:
    def test_positive_int_accepted(self) -> None:
        s = ChatSettings(top_k=40)
        assert s.top_k == 40

    def test_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChatSettings(top_k=0)

    def test_negative_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChatSettings(top_k=-5)


class TestMinP:
    def test_in_range(self) -> None:
        s = ChatSettings(min_p=0.05)
        assert s.min_p == 0.05

    def test_over_one_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChatSettings(min_p=1.5)


class TestRepeatPenalty:
    @pytest.mark.parametrize("value", [0.0, 1.0, 1.1, 5.0])
    def test_in_range(self, value: float) -> None:
        s = ChatSettings(repeat_penalty=value)
        assert s.repeat_penalty == value

    def test_over_five_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChatSettings(repeat_penalty=10.0)


class TestMaxTokens:
    def test_positive_accepted(self) -> None:
        s = ChatSettings(max_tokens=2048)
        assert s.max_tokens == 2048

    def test_zero_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ChatSettings(max_tokens=0)


class TestSystemPrompt:
    def test_text_preserved(self) -> None:
        s = ChatSettings(system_prompt="You are a concise assistant.")
        assert s.system_prompt == "You are a concise assistant."

    def test_whitespace_only_coerced_to_none(self) -> None:
        s = ChatSettings.model_validate({"system_prompt": "   \n\t"})
        assert s.system_prompt is None

    def test_empty_string_coerced_to_none(self) -> None:
        s = ChatSettings.model_validate({"system_prompt": ""})
        assert s.system_prompt is None


class TestQualityToggles:
    def test_sc_default_none(self) -> None:
        s = ChatSettings()
        assert s.self_consistency_enabled is None

    def test_cove_default_none(self) -> None:
        s = ChatSettings()
        assert s.chain_of_verification_enabled is None

    def test_stateless_default_none(self) -> None:
        s = ChatSettings()
        assert s.stateless is None

    def test_toggles_round_trip(self) -> None:
        s = ChatSettings(
            self_consistency_enabled=True,
            chain_of_verification_enabled=True,
            stateless=True,
        )
        assert s.self_consistency_enabled is True
        assert s.chain_of_verification_enabled is True
        assert s.stateless is True


class TestAbCompare:
    def test_minimal_enabled(self) -> None:
        s = ChatSettings(ab_compare=AbCompareSettings(enabled=True))
        assert s.ab_compare is not None
        assert s.ab_compare.enabled is True
        assert s.ab_compare.model_a is None

    def test_with_models(self) -> None:
        s = ChatSettings.model_validate({
            "ab_compare": {"enabled": True, "model_a": "m-a", "model_b": "m-b"}
        })
        assert s.ab_compare is not None
        assert s.ab_compare.model_a == "m-a"
        assert s.ab_compare.model_b == "m-b"

    def test_enabled_required(self) -> None:
        with pytest.raises(ValidationError):
            ChatSettings.model_validate({"ab_compare": {"model_a": "m"}})


class TestForwardCompat:
    def test_unknown_key_round_trip(self) -> None:
        """Future fields must survive a validate → dump round trip.

        ``extra='allow'`` is load-bearing for the rolling-upgrade story.
        """
        s = ChatSettings.model_validate({
            "rag_enabled": True,
            "future_field": "value",
            "nested_future": {"k": 1},
        })
        dumped = s.model_dump()
        assert dumped["future_field"] == "value"
        assert dumped["nested_future"] == {"k": 1}

    def test_unknown_keys_do_not_break_validation(self) -> None:
        s = ChatSettings.model_validate({"unknown": True})
        assert s.rag_enabled is None


class TestActivePreset:
    def test_default_none(self) -> None:
        s = ChatSettings()
        assert s.active_preset is None

    def test_set(self) -> None:
        s = ChatSettings(active_preset="research")
        assert s.active_preset == "research"


class TestMerge:
    def test_overlay_wins(self) -> None:
        base = ChatSettings(temperature=0.7, top_p=0.9)
        overlay = ChatSettings(temperature=1.1)
        merged = base.merge(overlay)
        assert merged.temperature == 1.1
        assert merged.top_p == 0.9

    def test_none_does_not_clear(self) -> None:
        # exclude_none=True semantics: a None field in the overlay is NOT
        # treated as "clear this value"; the base value wins.  This matches
        # update_settings's existing behaviour (which only writes keys
        # explicitly present in the form submission).
        base = ChatSettings(temperature=0.7)
        overlay = ChatSettings()  # all None
        merged = base.merge(overlay)
        assert merged.temperature == 0.7
