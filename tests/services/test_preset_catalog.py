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
  against a substring pinned from the FE's ``research`` persona text (a
  quick, hand-written sanity check), AND every preset's ``system_prompt``
  (all six, plus the shared ``_STANDARDS_TAIL``/``## STANDARDS`` block) is
  diffed byte-for-byte against text parsed live out of ``presets.ts`` —
  see :func:`_frontend_preset_system_prompts` and
  ``test_system_prompt_matches_frontend_verbatim`` below. A hand-edit to
  either side that isn't mirrored to the other fails loudly, with a
  unified diff naming the exact differing line(s).
"""
from __future__ import annotations

import difflib
import re
from pathlib import Path

import pytest

from lmchat.services.preset_catalog import (
    DEFAULT_PRESET_ID,
    PresetDefinition,
    get_preset_definition,
    list_adoptable_preset_ids,
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


# --- Full-string content-parity helpers -------------------------------------
#
# The pieces above (``_frontend_preset_ids``) only ever needed the six ``id``
# values. The functions below go further: they reconstruct each preset's
# FULL runtime ``system_prompt`` string straight out of the live
# ``presets.ts`` source (including the ``_STANDARDS_TAIL`` template-literal
# interpolation, resolved to its literal text) so it can be compared
# byte-for-byte against ``preset_catalog.py``'s own literal — the same
# comparison a human doing a manual side-by-side would do, just automated.
#
# What gets normalized, and why: only line endings (``\r\n`` -> ``\n``, in
# case either file is ever checked out with different EOL settings) and the
# two JS template-literal escapes ``presets.ts`` actually uses (``\```` and
# ``\${``, both required so a literal backtick / ``${`` inside the string
# doesn't terminate it / start an interpolation — see
# :func:`_unescape_ts_template_literal`). Nothing else is touched: internal
# blank lines, indentation, and trailing whitespace are left exactly as
# authored, because those ARE real prompt content the model sees — collapsing
# them would let a genuine formatting drift pass silently.

# Matches one preset object's ``id`` together with its ``system_prompt``
# template-literal body, up to (not including) the ``${_STANDARDS_TAIL}``
# interpolation every preset ends on. Non-greedy across both gaps so it
# can't walk past the current preset's own fields into the next preset's.
_PRESET_ENTRY_RE = re.compile(
    r'id:\s*"([^"]+)",[\s\S]*?system_prompt:\s*`([\s\S]*?)\$\{_STANDARDS_TAIL\}`,'
)

# The shared tail hoisted to a single FE constant (see presets.ts's own
# docstring: "the alternative was hand-syncing 6 copies"). Captured the same
# way — up to the closing backtick that ends the template literal.
_STANDARDS_TAIL_RE = re.compile(r"const _STANDARDS_TAIL = `([\s\S]*?)`;")

# Every Python preset's system_prompt ends on this identical block (mirrors
# _STANDARDS_TAIL). Slicing it out lets a drift in ONLY the shared block be
# diagnosed as such, rather than reported as "all six presets changed."
_STANDARDS_MARKER = "## STANDARDS\n"


def _unescape_ts_template_literal(text: str) -> str:
    """Undo the two JS template-literal escapes ``presets.ts`` actually uses.

    A template literal delimited by backticks must escape a literal
    backtick as ``\\```` and a literal ``${`` as ``\\${`` so it doesn't
    terminate the string / start an interpolation early — see the
    ``coder`` preset's "no stubs, no TODOs, no `pass`/`...` placeholders"
    line, which is the one place this matters today. Regex-extracting the
    raw source text (as this module does) captures those escapes
    verbatim; this undoes them so the comparison sees the same runtime
    string value the browser would. Only these two escapes are handled —
    a third one appearing in some future edit shows up as a literal
    backslash in the diff output, not a silent pass.
    """
    return text.replace("\\`", "`").replace("\\${", "${")


def _read_presets_ts() -> str:
    """Read ``presets.ts`` with line endings normalized to ``\\n``.

    A checkout-dependent CRLF/LF difference between the ``.py`` and ``.ts``
    files is not real prompt drift; normalizing here (and nowhere inside
    the captured text itself) keeps that distinction clean.
    """
    return _PRESETS_TS_PATH.read_text(encoding="utf-8").replace("\r\n", "\n")


def _frontend_standards_tail() -> str:
    """Return ``_STANDARDS_TAIL``'s literal value, unescaped.

    Starts with the constant's own leading ``\\n`` (the separator blank
    line every preset relies on) — callers that want just the
    ``## STANDARDS`` block should ``.lstrip("\\n")`` it.
    """
    text = _read_presets_ts()
    match = _STANDARDS_TAIL_RE.search(text)
    assert match, f"could not find the _STANDARDS_TAIL literal in {_PRESETS_TS_PATH}"
    return _unescape_ts_template_literal(match.group(1))


def _frontend_preset_system_prompts() -> dict[str, str]:
    """Reconstruct every preset's full runtime ``system_prompt`` from the
    live ``presets.ts`` source: the per-preset body up to the
    ``${_STANDARDS_TAIL}`` interpolation, plus that interpolation resolved
    to its literal text — exactly what the FE sends on the wire.
    """
    text = _read_presets_ts()
    standards_tail = _frontend_standards_tail()
    entries = _PRESET_ENTRY_RE.findall(text)
    assert entries, f"no system_prompt entries parsed out of {_PRESETS_TS_PATH}"
    result = {
        preset_id: _unescape_ts_template_literal(body) + standards_tail
        for preset_id, body in entries
    }
    assert len(result) == len(entries), (
        f"duplicate preset id parsed out of {_PRESETS_TS_PATH} — "
        "_PRESET_ENTRY_RE matched the same id twice"
    )
    return result


def _system_prompt(preset_id: str) -> str:
    """``get_preset_definition(preset_id).system_prompt``, with the
    ``Optional`` narrowed by an explicit assert instead of ``.system_prompt``
    on a possibly-``None`` value (keeps pyright quiet without ``# type:
    ignore``)."""
    preset = get_preset_definition(preset_id)
    assert preset is not None, f"no catalog entry for preset_id={preset_id!r}"
    return preset.system_prompt


def _standards_block(system_prompt: str) -> str:
    """Return the trailing ``## STANDARDS`` section of a preset's prompt."""
    idx = system_prompt.rindex(_STANDARDS_MARKER)
    return system_prompt[idx:]


def _drift_message(label: str, fe_text: str, py_text: str) -> str:
    """A unified diff between the FE- and BE-side text, for an assert
    message that names the differing line(s) instead of just failing."""
    diff = "".join(
        difflib.unified_diff(
            fe_text.splitlines(keepends=True),
            py_text.splitlines(keepends=True),
            fromfile=f"presets.ts :: {label}",
            tofile=f"preset_catalog.py :: {label}",
        )
    )
    return f"{label} has drifted between presets.ts and preset_catalog.py:\n{diff}"


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
        assert preset.short_description
        # The classifier prompt (_infer_mode_oob) needs this to stay a
        # cheap one-liner, not a second copy of the full system_prompt.
        assert len(preset.short_description) < 120


def test_default_preset_id_is_general() -> None:
    """Mirrors web/src/lib/presets.ts::DEFAULT_PRESET_ID."""
    assert DEFAULT_PRESET_ID == "general"


def test_list_adoptable_preset_ids_excludes_the_default() -> None:
    """RED-ON-REVERT for the operator-reported live defect (2026-08-14): a
    classifier offering the default persona as an adoptable option
    deterministically mis-picked it (8/8) for a clear /research-shaped
    exchange. The adoptable set must never include DEFAULT_PRESET_ID."""
    adoptable = list_adoptable_preset_ids()
    assert DEFAULT_PRESET_ID not in adoptable
    assert "general" not in adoptable


def test_list_adoptable_preset_ids_is_all_presets_minus_the_default() -> None:
    assert set(list_adoptable_preset_ids()) == set(list_preset_ids()) - {DEFAULT_PRESET_ID}
    assert len(list_adoptable_preset_ids()) == len(list_preset_ids()) - 1


def test_list_adoptable_preset_ids_preserves_catalog_order() -> None:
    adoptable = list_adoptable_preset_ids()
    full = [pid for pid in list_preset_ids() if pid != DEFAULT_PRESET_ID]
    assert adoptable == full


def test_list_adoptable_preset_ids_no_duplicates() -> None:
    ids = list_adoptable_preset_ids()
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize("preset_id", list_preset_ids())
def test_system_prompt_matches_frontend_verbatim(preset_id: str) -> None:
    """Full byte-for-byte parity between ``preset_catalog.py`` and
    ``presets.ts`` for every preset's ``system_prompt``.

    Supersedes (without replacing — see
    ``test_get_preset_definition_research_returns_research_persona`` above)
    the single-preset substring spot check: that check only ever covered
    ``research``, and only two short substrings of it. This covers all six
    presets, the full text of each, on every test run.
    """
    fe_prompts = _frontend_preset_system_prompts()
    assert preset_id in fe_prompts, (
        f"preset {preset_id!r} exists in preset_catalog.py but wasn't "
        f"parsed out of {_PRESETS_TS_PATH} — check _PRESET_ENTRY_RE against "
        "the live file (a structural change to the PRESETS object literal "
        "may have broken the regex, not the content)"
    )
    fe_text = fe_prompts[preset_id]
    py_text = _system_prompt(preset_id)
    assert fe_text == py_text, _drift_message(f"preset {preset_id!r}", fe_text, py_text)


def test_standards_block_matches_frontend_verbatim() -> None:
    """Isolates the SHARED ``## STANDARDS`` tail (the FE's
    ``_STANDARDS_TAIL``, appended verbatim to all six Python presets) so a
    drift in just this one shared block is diagnosed directly — as itself —
    rather than surfacing as "all six presets changed" across six separate
    parametrized failures above.
    """
    fe_block = _frontend_standards_tail().lstrip("\n")

    py_blocks = {
        preset_id: _standards_block(_system_prompt(preset_id)) for preset_id in list_preset_ids()
    }
    # BE-side self-consistency first: all six MUST share the exact same
    # tail (that's the entire reason the FE hoisted it to one constant). A
    # failure here means a hand-edit to one Python preset's STANDARDS
    # section skipped the other five — a bug independent of the FE.
    distinct_py_blocks = set(py_blocks.values())
    assert len(distinct_py_blocks) == 1, (
        "preset_catalog.py's six presets disagree on their own STANDARDS "
        f"block (must be identical across all six): {py_blocks!r}"
    )
    py_block = next(iter(distinct_py_blocks))
    assert fe_block == py_block, _drift_message("shared STANDARDS tail", fe_block, py_block)
