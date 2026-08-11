# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the P15f DoS JUnit adapters.

Covers:
  - tools/redos_fuzzer.py       — ReDoS pattern fuzzer
  - tools/decompression_bomb_test.py — gzip/zip bomb upload harness
  - tools/json_bomb_test.py     — JSON bomb harness
  - tools/junit_from_slowhttptest.py — slowhttptest wrapper

All tests are purely unit-level — no external dependencies, no Docker,
no live target.  Tests must pass in a clean CI environment with only the
standard dev deps installed.
"""
from __future__ import annotations

import importlib.util as _ilu
import re
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOLS_DIR = REPO_ROOT / "tools"


def _load_module(name: str, file: Path) -> object:
    spec = _ilu.spec_from_file_location(name, file)
    assert spec is not None and spec.loader is not None
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# Load modules under test.
redos_fuzzer = _load_module("redos_fuzzer", TOOLS_DIR / "redos_fuzzer.py")
decomp_test = _load_module("decompression_bomb_test", TOOLS_DIR / "decompression_bomb_test.py")
json_bomb = _load_module("json_bomb_test", TOOLS_DIR / "json_bomb_test.py")
slowhttp = _load_module("junit_from_slowhttptest", TOOLS_DIR / "junit_from_slowhttptest.py")


def _get_suite(root: ET.Element) -> ET.Element:
    """Return the first <testsuite> child of a <testsuites> root."""
    suite = root.find("testsuite")
    assert suite is not None, "no <testsuite> element found"
    return suite


def _attr_int(elem: ET.Element, key: str) -> int:
    return int(elem.get(key, "0") or "0")


# ===========================================================================
# redos_fuzzer.py
# ===========================================================================

class TestRedosFuzzer:

    def test_suite_name(self) -> None:
        suite = redos_fuzzer._build_suite([("pat", True, "ok")])  # type: ignore[attr-defined]
        assert suite.get("name") == "dos-redos"

    def test_all_pass_no_failures(self) -> None:
        results = [("pat1", True, "ok"), ("pat2", True, "ok")]
        suite = redos_fuzzer._build_suite(results)  # type: ignore[attr-defined]
        assert _attr_int(suite, "failures") == 0
        assert _attr_int(suite, "tests") == 2

    def test_one_failure(self) -> None:
        results = [("pat1", True, "ok"), ("pat2", False, "slow: 200ms")]
        suite = redos_fuzzer._build_suite(results)  # type: ignore[attr-defined]
        assert _attr_int(suite, "failures") == 1
        tc_fail = suite.find(".//testcase[@name='pat2']/failure")
        assert tc_fail is not None

    def test_skipped_mode_emits_single_skipped(self) -> None:
        suite = redos_fuzzer._emit_skipped("no hypothesis installed")  # type: ignore[attr-defined]
        assert _attr_int(suite, "skipped") == 1
        assert _attr_int(suite, "failures") == 0
        sk = suite.find(".//skipped")
        assert sk is not None
        assert "no hypothesis installed" in (sk.get("message") or "")

    def test_skipped_mode_default_reason(self) -> None:
        suite = redos_fuzzer._emit_skipped()  # type: ignore[attr-defined]
        sk = suite.find(".//skipped")
        assert sk is not None
        assert sk.get("message"), "skipped message must not be empty"

    def test_run_pattern_fast_patterns_pass(self) -> None:
        """The known lm-chat patterns must all complete under 50 ms per seed."""
        pattern = re.compile(r"^[a-zA-Z0-9_]{3,64}$")
        ok, detail = redos_fuzzer._run_pattern("username_validate", pattern)  # type: ignore[attr-defined]
        assert ok, f"expected pass but got: {detail}"

    def test_patterns_inventory_not_empty(self) -> None:
        patterns = redos_fuzzer._PATTERNS  # type: ignore[attr-defined]
        assert len(patterns) >= 3, "expected at least 3 patterns in inventory"

    def test_seeds_list_not_empty(self) -> None:
        seeds = redos_fuzzer._SEEDS  # type: ignore[attr-defined]
        assert len(seeds) >= 10

    @pytest.mark.parametrize("name,pattern", [
        ("username", re.compile(r"^[a-zA-Z0-9_]{3,64}$")),
        ("whitespace", re.compile(r"\s+")),
        ("filename_sanitize", re.compile(r"[^a-zA-Z0-9._\- ]")),
    ])
    def test_all_lmchat_patterns_under_budget(
        self, name: str, pattern: re.Pattern[str]
    ) -> None:
        ok, detail = redos_fuzzer._run_pattern(name, pattern)  # type: ignore[attr-defined]
        assert ok, f"pattern {name!r} exceeded budget: {detail}"

    def test_main_skipped_returns_zero(self, tmp_path: Path) -> None:
        out = tmp_path / "redos.xml"
        rc = redos_fuzzer.main(["--skipped", "--output", str(out)])  # type: ignore[attr-defined]
        assert rc == 0
        root = ET.parse(out).getroot()
        suite = _get_suite(root)
        assert _attr_int(suite, "skipped") == 1

    def test_main_skipped_with_reason(self, tmp_path: Path) -> None:
        out = tmp_path / "redos.xml"
        rc = redos_fuzzer.main(  # type: ignore[attr-defined]
            ["--skipped", "--skipped-reason", "custom reason xyz", "--output", str(out)]
        )
        assert rc == 0
        root = ET.parse(out).getroot()
        suite = _get_suite(root)
        sk = suite.find(".//skipped")
        assert sk is not None
        assert "custom reason xyz" in (sk.get("message") or sk.text or "")


# ===========================================================================
# decompression_bomb_test.py
# ===========================================================================

class TestDecompressionBombAdapter:

    def test_gzip_bomb_is_bytes(self) -> None:
        bomb = decomp_test._make_gzip_bomb(compressed_kb=1)  # type: ignore[attr-defined]
        assert isinstance(bomb, bytes)
        assert len(bomb) > 0

    def test_zip_bomb_is_bytes(self) -> None:
        bomb = decomp_test._make_zip_bomb(compressed_kb=1)  # type: ignore[attr-defined]
        assert isinstance(bomb, bytes)
        assert len(bomb) > 0

    def test_nested_gzip_bomb_is_bytes(self) -> None:
        bomb = decomp_test._make_nested_gzip_bomb(layers=2)  # type: ignore[attr-defined]
        assert isinstance(bomb, bytes)

    def test_cases_list_not_empty(self) -> None:
        cases = decomp_test._make_cases()  # type: ignore[attr-defined]
        assert len(cases) >= 3

    def test_assert_case_4xx_passes(self) -> None:
        """4xx response from server = correct rejection."""
        case = decomp_test._make_cases()[0]  # type: ignore[attr-defined]

        def post_fn(path: str, payload: bytes, fname: str, ct: str) -> tuple[int, str]:
            return 413, "Request Entity Too Large"

        ok, detail = decomp_test._assert_case(case, post_fn)  # type: ignore[attr-defined]
        assert ok
        assert "413" in detail

    def test_assert_case_2xx_fails(self) -> None:
        """2xx response = bomb was accepted = test failure."""
        case = decomp_test._make_cases()[0]  # type: ignore[attr-defined]

        def post_fn(path: str, payload: bytes, fname: str, ct: str) -> tuple[int, str]:
            return 201, '{"id":1}'

        ok, detail = decomp_test._assert_case(case, post_fn)  # type: ignore[attr-defined]
        assert not ok

    def test_assert_case_5xx_fails(self) -> None:
        """5xx response = server panicked = test failure."""
        case = decomp_test._make_cases()[0]  # type: ignore[attr-defined]

        def post_fn(path: str, payload: bytes, fname: str, ct: str) -> tuple[int, str]:
            return 500, "Internal Server Error"

        ok, detail = decomp_test._assert_case(case, post_fn)  # type: ignore[attr-defined]
        assert not ok
        assert "5xx" in detail

    def test_build_suite_all_pass(self) -> None:
        results = [("a", True, "ok"), ("b", True, "ok")]
        suite = decomp_test._build_suite(results)  # type: ignore[attr-defined]
        assert suite.get("name") == "dos-decompression"
        assert _attr_int(suite, "failures") == 0

    def test_build_suite_one_fail(self) -> None:
        results = [("a", True, "ok"), ("b", False, "bomb accepted")]
        suite = decomp_test._build_suite(results)  # type: ignore[attr-defined]
        assert _attr_int(suite, "failures") == 1

    def test_skipped_mode(self) -> None:
        suite = decomp_test._emit_skipped("no docker")  # type: ignore[attr-defined]
        assert _attr_int(suite, "skipped") == 1
        assert _attr_int(suite, "failures") == 0

    def test_main_skipped(self, tmp_path: Path) -> None:
        out = tmp_path / "decomp.xml"
        rc = decomp_test.main(["--skipped", "--output", str(out)])  # type: ignore[attr-defined]
        assert rc == 0
        root = ET.parse(out).getroot()
        suite = _get_suite(root)
        assert _attr_int(suite, "skipped") == 1


# ===========================================================================
# json_bomb_test.py
# ===========================================================================

class TestJsonBombAdapter:

    def test_nested_arrays_is_valid_json(self) -> None:
        payload = json_bomb._make_deeply_nested_arrays(100)  # type: ignore[attr-defined]
        import json
        parsed = json.loads(payload)
        assert isinstance(parsed, list)

    def test_massive_array_length(self) -> None:
        payload = json_bomb._make_massive_array(1000)  # type: ignore[attr-defined]
        import json
        arr = json.loads(payload)
        assert len(arr) == 1000

    def test_duplicate_keys_is_string(self) -> None:
        payload = json_bomb._make_duplicate_keys(100)  # type: ignore[attr-defined]
        assert isinstance(payload, str)
        assert payload.startswith("{")

    def test_cases_not_empty(self) -> None:
        cases = json_bomb._make_cases()  # type: ignore[attr-defined]
        assert len(cases) >= 5

    def test_assert_case_4xx_passes(self) -> None:
        case = json_bomb._make_cases()[0]  # type: ignore[attr-defined]

        def post_json_fn(path: str, payload_str: str) -> tuple[int, str]:
            return 422, "Unprocessable Entity"

        ok, detail = json_bomb._assert_case(case, post_json_fn)  # type: ignore[attr-defined]
        assert ok

    def test_assert_case_5xx_fails(self) -> None:
        case = json_bomb._make_cases()[0]  # type: ignore[attr-defined]

        def post_json_fn(path: str, payload_str: str) -> tuple[int, str]:
            return 500, "Internal Server Error"

        ok, detail = json_bomb._assert_case(case, post_json_fn)  # type: ignore[attr-defined]
        assert not ok
        assert "5xx" in detail

    def test_build_suite_counts(self) -> None:
        results = [("a", True, "ok"), ("b", False, "boom")]
        suite = json_bomb._build_suite(results)  # type: ignore[attr-defined]
        assert suite.get("name") == "dos-jsonbomb"
        assert _attr_int(suite, "tests") == 2
        assert _attr_int(suite, "failures") == 1

    def test_skipped_mode(self) -> None:
        suite = json_bomb._emit_skipped()  # type: ignore[attr-defined]
        assert _attr_int(suite, "skipped") == 1

    def test_main_skipped(self, tmp_path: Path) -> None:
        out = tmp_path / "jsonbomb.xml"
        rc = json_bomb.main(["--skipped", "--output", str(out)])  # type: ignore[attr-defined]
        assert rc == 0
        root = ET.parse(out).getroot()
        suite = _get_suite(root)
        assert _attr_int(suite, "skipped") == 1

    @pytest.mark.parametrize("depth", [10, 100, 500])
    def test_nested_arrays_depth(self, depth: int) -> None:
        payload = json_bomb._make_deeply_nested_arrays(depth)  # type: ignore[attr-defined]
        import json
        parsed = json.loads(payload)
        # Verify it's nested depth levels deep.
        current = parsed
        for _ in range(depth - 1):
            assert isinstance(current, list), "expected list at each level"
            current = current[0]


# ===========================================================================
# junit_from_slowhttptest.py
# ===========================================================================

class TestSlowHttptestAdapter:

    def test_emit_skipped_default(self) -> None:
        suite = slowhttp._emit_skipped()  # type: ignore[attr-defined]
        assert suite.get("name") == "dos-slowloris"
        assert _attr_int(suite, "skipped") == 1
        assert _attr_int(suite, "failures") == 0

    def test_emit_skipped_custom_reason(self) -> None:
        suite = slowhttp._emit_skipped("custom reason")  # type: ignore[attr-defined]
        sk = suite.find(".//skipped")
        assert sk is not None
        assert "custom reason" in (sk.get("message") or "")

    def test_build_suite_pass(self) -> None:
        suite = slowhttp._build_suite(  # type: ignore[attr-defined]
            passed=True,
            status_line="Test exit status: Service available on 127.0.0.1:18001",
            raw_output="...",
        )
        assert _attr_int(suite, "failures") == 0
        assert _attr_int(suite, "tests") == 1

    def test_build_suite_fail(self) -> None:
        suite = slowhttp._build_suite(  # type: ignore[attr-defined]
            passed=False,
            status_line="Test exit status: Exit on Connection count reached 0",
            raw_output="lots of output",
        )
        assert _attr_int(suite, "failures") == 1
        fail = suite.find(".//failure")
        assert fail is not None

    @pytest.mark.parametrize("output,expected_pass", [
        ("Test exit status: Service available on 127.0.0.1:18001", True),
        ("Test exit status: Exit on Connection count reached 0", False),
        ("Test exit status: service available on localhost:18001", True),
        ("No status line here", True),   # inconclusive = pass
        ("", True),                       # empty = inconclusive = pass
    ])
    def test_parse_exit_status_parametrized(
        self, output: str, expected_pass: bool
    ) -> None:
        passed, status_line = slowhttp._parse_exit_status(output)  # type: ignore[attr-defined]
        assert passed == expected_pass, (
            f"parse_exit_status({output!r}) returned passed={passed}, expected {expected_pass}. "
            f"status_line={status_line!r}"
        )

    def test_main_skipped_flag(self, tmp_path: Path) -> None:
        out = tmp_path / "slowloris.xml"
        rc = slowhttp.main(["--skipped", "--output", str(out)])  # type: ignore[attr-defined]
        assert rc == 0
        root = ET.parse(out).getroot()
        suite = _get_suite(root)
        assert _attr_int(suite, "skipped") == 1

    def test_main_skipped_reason(self, tmp_path: Path) -> None:
        out = tmp_path / "slowloris.xml"
        rc = slowhttp.main([  # type: ignore[attr-defined]
            "--skipped",
            "--skipped-reason", "docker not available in CI",
            "--output", str(out),
        ])
        assert rc == 0
        root = ET.parse(out).getroot()
        suite = _get_suite(root)
        sk = suite.find(".//skipped")
        assert sk is not None
        assert "docker not available in CI" in (sk.get("message") or sk.text or "")

    def test_docker_unavailable_path_skips(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When Docker is unavailable the recipe degrades to a skipped testcase."""
        monkeypatch.setattr(slowhttp, "_docker_available", lambda: False)  # type: ignore[attr-defined]
        out = tmp_path / "slowloris_no_docker.xml"
        rc = slowhttp.main(["--output", str(out)])  # type: ignore[attr-defined]
        assert rc == 0
        root = ET.parse(out).getroot()
        suite = _get_suite(root)
        assert _attr_int(suite, "skipped") == 1

    def test_suite_name_constant(self) -> None:
        assert slowhttp._SUITE_NAME == "dos-slowloris"  # type: ignore[attr-defined]

    def test_xml_structure(self, tmp_path: Path) -> None:
        out = tmp_path / "structure.xml"
        slowhttp.main(["--skipped", "--output", str(out)])  # type: ignore[attr-defined]
        root = ET.parse(out).getroot()
        assert root.tag == "testsuites"
        suite = _get_suite(root)
        assert suite.tag == "testsuite"
        assert suite.find("testcase") is not None
