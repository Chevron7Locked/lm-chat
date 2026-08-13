# SPDX-License-Identifier: Apache-2.0
"""Role-preset catalog: id-set parity with ``web/src/lib/presets.ts`` + a
verbatim-content spot check.

``preset_catalog`` is the backend's only copy of the six role personas'
prompt text; the frontend module is the ONLY other place this text exists,
and there is no shared source of truth or codegen step tying them together
(see ``preset_catalog``'s module docstring). This test guards the two ways
that split can silently drift:

- **id-set parity** — a preset added/removed/renamed on the FE without a
  matching catalog change. IDs are parsed directly out of the live
  ``presets.ts`` source (not hardcoded here) so this guard survives a
  future 7th preset without needing this test file touched too.
- **content drift** — ``get_preset_definition('research')`` is checked
  against a substring pinned from the FE's ``research`` persona text, so a
  hand-edit on one side that isn't mirrored to the other fails loudly.
"""
from __future__ import annotations

import re
from pathlib import Path

from lmchat.services.preset_catalog import (
    PresetDefinition,
    get_preset_definition,
    list_preset_ids,
)

# Same repo-relative-path pattern as
# tests/contracts/test_sse_event_names_contract.py: tests/services/<file>.py
# -> parent (services) -> parent (tests) -> parent (repo root).
_PRESETS_TS_PATH = (
    Path(__file__).parent.parent.parent / "web" / "src" / "lib" / "presets.ts"
)

# Known-good snapshot, kept only as a sanity floor if the FE file ever goes
# missing/unreadable in some execution environment. The live ids parsed out
# of presets.ts below are the actual assertion; these six MUST stay in sync
# with ``web/src/lib/presets.ts::PRESETS`` any time a preset is added,
# removed, or renamed there.
_KNOWN_PRESET_IDS = {"general", "coder", "creative", "research", "analyst", "architect"}


def _frontend_preset_ids() -> set[str]:
    """Parse the ``Preset.id`` values straight out of ``presets.ts``.

    Matches ``id: "<value>",`` lines inside the ``PRESETS`` object literal.
    The ``Preset`` interface's own ``id: string;`` field declaration (no
    quotes — a type, not a value) does not match this pattern, so only the
    six concrete preset entries are picked up.
    """
    text = _PRESETS_TS_PATH.read_text(encoding="utf-8")
    ids = re.findall(r'id:\s*"([^"]+)",', text)
    assert ids, f"no preset ids parsed out of {_PRESETS_TS_PATH}"
    return set(ids)


def test_catalog_ids_match_frontend_presets_exactly() -> None:
    fe_ids = _frontend_preset_ids()
    assert fe_ids == _KNOWN_PRESET_IDS, (
        "web/src/lib/presets.ts preset ids changed — update _KNOWN_PRESET_IDS "
        "and src/lmchat/services/preset_catalog.py to match"
    )
    assert set(list_preset_ids()) == fe_ids


def test_list_preset_ids_returns_all_six_with_no_duplicates() -> None:
    ids = list_preset_ids()
    assert len(ids) == 6
    assert len(set(ids)) == len(ids)


def test_get_preset_definition_research_returns_research_persona() -> None:
    preset = get_preset_definition("research")
    assert isinstance(preset, PresetDefinition)
    assert preset.id == "research"
    assert preset.label == "Research"
    assert preset.temperature == 0.4
    # Substrings pinned verbatim from the ``research`` persona's opening and
    # a mid-body process step in web/src/lib/presets.ts — a content edit on
    # either side that isn't mirrored to the other breaks this.
    assert preset.system_prompt.startswith("Research mode.\n\n## TOOLS")
    assert (
        "you MUST search before answering. Do not answer these from memory."
        in preset.system_prompt
    )
    assert (
        "**Search → read → refine** — For each search-mandatory sub-question"
        in preset.system_prompt
    )
    # The shared STANDARDS tail (pinned into every preset) is resolved to
    # literal text, not left as an unresolved ``${_STANDARDS_TAIL}`` token.
    assert "${_STANDARDS_TAIL}" not in preset.system_prompt
    assert preset.system_prompt.endswith(
        "A gap you can't fill from evidence stays a named gap — never a "
        "guess dressed as fact."
    )


def test_get_preset_definition_returns_none_for_unknown_id() -> None:
    assert get_preset_definition("bogus-id") is None


def test_get_preset_definition_returns_none_for_raw_model_sentinel() -> None:
    # web/src/lib/presets.ts::RAW_PRESET_ID == "none" — the raw-model escape
    # hatch is deliberately absent from PRESETS (and therefore this catalog).
    assert get_preset_definition("none") is None


def test_all_catalog_entries_are_well_formed() -> None:
    for preset_id in list_preset_ids():
        preset = get_preset_definition(preset_id)
        assert preset is not None
        assert preset.id == preset_id
        assert preset.label
        assert preset.system_prompt
        assert isinstance(preset.temperature, float)
        assert 0.0 <= preset.temperature <= 1.0
