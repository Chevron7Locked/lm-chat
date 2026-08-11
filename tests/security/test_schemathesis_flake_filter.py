# SPDX-License-Identifier: Apache-2.0
"""Unit tests for tools/schemathesis_flake_filter.py."""
from __future__ import annotations

import importlib.util as _ilu
import sys
import textwrap
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

# Side-load ``tools/schemathesis_flake_filter.py`` — same pattern used by
# test_idor_grid.py; ``tools/`` is a script directory, not an installed package.
_REPO_ROOT = Path(__file__).resolve().parents[2]
_TOOLS_DIR = _REPO_ROOT / "tools"


def _load_module(name: str, file: Path) -> Any:
    spec = _ilu.spec_from_file_location(name, file)
    assert spec is not None and spec.loader is not None
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


_sff = _load_module(
    "schemathesis_flake_filter",
    _TOOLS_DIR / "schemathesis_flake_filter.py",
)

filter_xml = _sff.filter_xml
main = _sff.main


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_tree(
    testcases: list[dict[str, str]],
    *,
    suite_failures: int = 0,
    suite_skipped: int = 0,
) -> ET.ElementTree[ET.Element]:
    """Build a minimal JUnit XML tree for testing."""
    tests = len(testcases)
    root = ET.Element(
        "testsuites",
        {
            "name": "l5-schemathesis",
            "tests": str(tests),
            "failures": str(suite_failures),
            "errors": "0",
            "skipped": str(suite_skipped),
        },
    )
    suite = ET.SubElement(
        root,
        "testsuite",
        {
            "name": "schemathesis",
            "tests": str(tests),
            "failures": str(suite_failures),
            "errors": "0",
            "skipped": str(suite_skipped),
        },
    )
    for tc in testcases:
        tc_el = ET.SubElement(suite, "testcase", {"name": tc["name"]})
        if "failure_msg" in tc:
            fail = ET.SubElement(
                tc_el, "failure", {"type": "failure", "message": tc["failure_msg"]}
            )
            fail.text = tc["failure_msg"]
    return ET.ElementTree(root)


def _entries(
    *,
    testcase: str = "POST /api/auth/login",
    substring: str = "429",
    lmc_id: str = "LMC-2026-006",
) -> list[dict[str, str]]:
    return [
        {
            "testcase": testcase,
            "failure_substring": substring,
            "lmc_id": lmc_id,
            "rationale": "Rate limit is by design.",
            "approved_by": "operator",
            "approved_at": "2026-05-24",
        }
    ]


def _root_counters(tree: ET.ElementTree[ET.Element]) -> tuple[int, int, int]:
    """Return (tests, failures, skipped) from the <testsuites> root."""
    root = tree.getroot()
    assert root is not None
    return (
        int(root.get("tests", "0")),
        int(root.get("failures", "0")),
        int(root.get("skipped", "0")),
    )


def _suite_counters(tree: ET.ElementTree[ET.Element]) -> tuple[int, int, int]:
    """Return (tests, failures, skipped) from the first <testsuite>."""
    suite = tree.getroot().find("testsuite")
    assert suite is not None
    return (
        int(suite.get("tests", "0")),
        int(suite.get("failures", "0")),
        int(suite.get("skipped", "0")),
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestFilterXml:
    def test_matching_failure_becomes_skipped(self) -> None:
        """Matching testcase + substring → failure element replaced by skipped."""
        tree = _build_tree(
            [{"name": "POST /api/auth/login", "failure_msg": "[429] Too Many Requests"}],
            suite_failures=1,
        )
        converted = filter_xml(tree, _entries())
        assert converted == 1

        suite = tree.getroot().find("testsuite")
        assert suite is not None
        tc = suite.find("testcase")
        assert tc is not None
        assert tc.find("failure") is None
        skipped = tc.find("skipped")
        assert skipped is not None
        assert "LMC-2026-006" in (skipped.get("message") or "")
        assert skipped.get("type") == "schemathesis-known-flake"

    def test_non_matching_testcase_unchanged(self) -> None:
        """A testcase whose name doesn't match the glob is left alone."""
        tree = _build_tree(
            [{"name": "GET /api/other", "failure_msg": "[429] Too Many Requests"}],
            suite_failures=1,
        )
        converted = filter_xml(tree, _entries())
        assert converted == 0

        suite = tree.getroot().find("testsuite")
        assert suite is not None
        tc = suite.find("testcase")
        assert tc is not None
        assert tc.find("failure") is not None  # unchanged
        assert tc.find("skipped") is None

    def test_matching_testcase_nonmatching_substring_unchanged(self) -> None:
        """Testcase name matches but failure_substring is absent → no change."""
        tree = _build_tree(
            [{"name": "POST /api/auth/login", "failure_msg": "[500] Internal Error"}],
            suite_failures=1,
        )
        converted = filter_xml(tree, _entries(substring="429"))
        assert converted == 0

        tc = tree.getroot().find(".//testcase")
        assert tc is not None
        assert tc.find("failure") is not None

    def test_idempotency_re_run_does_not_double_filter(self) -> None:
        """Running filter_xml twice produces the same result as running it once."""
        tree = _build_tree(
            [{"name": "POST /api/auth/login", "failure_msg": "[429] Too Many Requests"}],
            suite_failures=1,
        )
        converted_1 = filter_xml(tree, _entries())
        assert converted_1 == 1

        # Second pass on already-filtered tree.
        converted_2 = filter_xml(tree, _entries())
        assert converted_2 == 0  # nothing to convert

        # Still only one skipped, not two.
        tc = tree.getroot().find(".//testcase")
        assert tc is not None
        assert len(list(tc.iter("skipped"))) == 1

    def test_failures_counter_decrements(self) -> None:
        """testsuite failures counter decreases by number of converted elements."""
        tree = _build_tree(
            [
                {"name": "POST /api/auth/login", "failure_msg": "[429] Too Many"},
                {"name": "GET /api/stable", "failure_msg": "[500] Crash"},
            ],
            suite_failures=2,
        )
        filter_xml(tree, _entries())

        _tests, failures, skipped = _suite_counters(tree)
        assert failures == 1  # only the login one was converted
        assert skipped == 1

    def test_skipped_counter_increments(self) -> None:
        """testsuite skipped counter increases by number of converted elements."""
        tree = _build_tree(
            [{"name": "POST /api/auth/login", "failure_msg": "[429] Rate limit"}],
            suite_failures=1,
            suite_skipped=2,
        )
        filter_xml(tree, _entries())

        _tests, _failures, skipped = _suite_counters(tree)
        assert skipped == 3  # was 2, now 3

    def test_tests_counter_unchanged(self) -> None:
        """Total tests count must not change after filtering."""
        tree = _build_tree(
            [{"name": "POST /api/auth/login", "failure_msg": "[429] limit"}],
            suite_failures=1,
        )
        tests_before, _, _ = _root_counters(tree)
        filter_xml(tree, _entries())
        tests_after, _, _ = _root_counters(tree)
        assert tests_before == tests_after

    def test_root_testsuites_counters_updated(self) -> None:
        """testsuites-level failures + skipped are re-summed after filtering."""
        tree = _build_tree(
            [{"name": "POST /api/auth/login", "failure_msg": "[429] rate"}],
            suite_failures=1,
        )
        filter_xml(tree, _entries())

        _tests, failures, skipped = _root_counters(tree)
        assert failures == 0
        assert skipped == 1


class TestMainCli:
    def test_allowlist_missing_exits_0(self, tmp_path: Path) -> None:
        """If the allowlist file does not exist, exit with 0 and do nothing."""
        xml_content = textwrap.dedent("""\
            <?xml version='1.0' encoding='utf-8'?>
            <testsuites name="l5-schemathesis" tests="1" failures="1" errors="0" skipped="0">
              <testsuite name="schemathesis" tests="1" failures="1" errors="0" skipped="0">
                <testcase name="POST /api/auth/login">
                  <failure type="failure" message="[429] Too Many">text</failure>
                </testcase>
              </testsuite>
            </testsuites>
        """)
        input_xml = tmp_path / "input.xml"
        input_xml.write_text(xml_content)
        output_xml = tmp_path / "output.xml"
        missing_allowlist = tmp_path / "no-such-file.yaml"

        rc = main(
            [
                "--input", str(input_xml),
                "--allowlist", str(missing_allowlist),
                "--output", str(output_xml),
            ]
        )
        assert rc == 0
        # Output should not have been written (no-op).
        assert not output_xml.exists()

    def test_matching_entry_applied_via_cli(self, tmp_path: Path) -> None:
        """End-to-end CLI: matching failure in XML is downgraded to skipped."""
        xml_content = textwrap.dedent("""\
            <?xml version='1.0' encoding='utf-8'?>
            <testsuites name="l5-schemathesis" tests="1" failures="1" errors="0" skipped="0">
              <testsuite name="schemathesis" tests="1" failures="1" errors="0" skipped="0">
                <testcase name="POST /api/auth/login">
                  <failure type="failure" message="[429] Too Many Requests">detail</failure>
                </testcase>
              </testsuite>
            </testsuites>
        """)
        allowlist_content = textwrap.dedent("""\
            known_flakes:
              - testcase: "POST /api/auth/login"
                failure_substring: "429"
                rationale: "Rate limit is by design."
                lmc_id: LMC-2026-006
                approved_by: operator
                approved_at: "2026-05-24"
        """)
        input_xml = tmp_path / "input.xml"
        input_xml.write_text(xml_content)
        allowlist = tmp_path / "flakes.yaml"
        allowlist.write_text(allowlist_content)
        output_xml = tmp_path / "output.xml"

        rc = main(
            [
                "--input", str(input_xml),
                "--allowlist", str(allowlist),
                "--output", str(output_xml),
            ]
        )
        assert rc == 0

        tree = ET.parse(output_xml)
        root = tree.getroot()
        assert root.get("failures") == "0"
        assert root.get("skipped") == "1"
        tc = root.find(".//testcase")
        assert tc is not None
        assert tc.find("failure") is None
        assert tc.find("skipped") is not None
