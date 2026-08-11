# SPDX-License-Identifier: Apache-2.0
"""P15h — Unit tests for the soak-test adapters.

Covers:
  - JSONL checkpoint parsing (load_checkpoints)
  - Drift assertion logic (assess_drift) — parametrized borderline-pass,
    clear-fail per axis (memory_growth, p99_stability, error_rate)
  - JUnit XML emission shape (build_junit, _emit_skipped_suite)
  - CLI skipped mode (--skipped / missing file)

15 tests minimum per spec; this suite has 18.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

# ---------------------------------------------------------------------------
# Load the two modules under test without installing them as packages.
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
TOOLS_DIR = REPO_ROOT / "tools"


def _load(name: str, file: Path):  # type: ignore[return]
    spec = importlib.util.spec_from_file_location(name, file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


junit_from_soak = _load("junit_from_soak", TOOLS_DIR / "junit_from_soak.py")
soak_test = _load("soak_test", TOOLS_DIR / "soak_test.py")

# Re-export the public API we need.
load_checkpoints = junit_from_soak.load_checkpoints
assess_drift = junit_from_soak.assess_drift
build_junit = junit_from_soak.build_junit
_emit_skipped_suite = junit_from_soak._emit_skipped_suite
MEMORY_GROWTH_MAX = junit_from_soak.MEMORY_GROWTH_MAX
P99_STABILITY_MAX = junit_from_soak.P99_STABILITY_MAX
ERROR_RATE_MAX = junit_from_soak.ERROR_RATE_MAX

# Also import assess_drift from soak_test (separate implementation).
soak_assess_drift = soak_test.assess_drift


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _checkpoint(
    *,
    idx: int = 0,
    rss_mb: float = 100.0,
    p99_ms: float = 200.0,
    total_requests: int = 1000,
    total_failures: int = 0,
    elapsed_seconds: float = 1800.0,
) -> dict:
    return {
        "checkpoint_index": idx,
        "elapsed_seconds": elapsed_seconds,
        "timestamp_utc": "2026-05-23T12:00:00Z",
        "rss_mb": rss_mb,
        "fd_count": 50,
        "total_requests": total_requests,
        "total_failures": total_failures,
        "p95_ms": p99_ms * 0.8,
        "p99_ms": p99_ms,
        "error_rate": total_failures / total_requests if total_requests else 0.0,
    }


def _suite_from_root(root: ET.Element) -> ET.Element:
    """Extract the first <testsuite> child from <testsuites>."""
    suite = root.find("testsuite")
    assert suite is not None
    return suite


def _failures_in(suite: ET.Element) -> list[ET.Element]:
    return suite.findall(".//failure")


def _skipped_in(suite: ET.Element) -> list[ET.Element]:
    return suite.findall(".//skipped")


# ---------------------------------------------------------------------------
# 1. JSONL parsing — load_checkpoints
# ---------------------------------------------------------------------------

class TestLoadCheckpoints:
    def test_parses_valid_jsonl(self, tmp_path: Path) -> None:
        p = tmp_path / "cp.jsonl"
        records = [_checkpoint(idx=i, elapsed_seconds=i * 1800.0) for i in range(3)]
        p.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        loaded = load_checkpoints(p)
        assert len(loaded) == 3
        assert loaded[0]["checkpoint_index"] == 0
        assert loaded[2]["checkpoint_index"] == 2

    def test_skips_blank_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "cp.jsonl"
        p.write_text(
            json.dumps(_checkpoint(idx=0)) + "\n\n" + json.dumps(_checkpoint(idx=1)) + "\n",
            encoding="utf-8",
        )
        loaded = load_checkpoints(p)
        assert len(loaded) == 2

    def test_raises_on_invalid_json(self, tmp_path: Path) -> None:
        p = tmp_path / "cp.jsonl"
        p.write_text("not json\n", encoding="utf-8")
        with pytest.raises(ValueError, match="invalid JSON"):
            load_checkpoints(p)

    def test_raises_file_not_found(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            load_checkpoints(tmp_path / "missing.jsonl")

    def test_empty_file_returns_empty_list(self, tmp_path: Path) -> None:
        p = tmp_path / "cp.jsonl"
        p.write_text("", encoding="utf-8")
        assert load_checkpoints(p) == []


# ---------------------------------------------------------------------------
# 2. Drift assertion logic — assess_drift (parametrized)
# ---------------------------------------------------------------------------

class TestAssessDrift:
    # -- insufficient data ---------------------------------------------------

    def test_zero_checkpoints_all_skipped(self) -> None:
        results = assess_drift([])
        assert len(results) == 3
        for r in results:
            assert r.get("skipped") is True
            assert r["passed"] is True

    def test_one_checkpoint_all_skipped(self) -> None:
        results = assess_drift([_checkpoint(idx=0)])
        assert all(r.get("skipped") for r in results)

    # -- memory_growth -------------------------------------------------------

    @pytest.mark.parametrize("rss_last,should_pass", [
        (100.0, True),   # no growth
        (109.9, True),   # borderline pass (9.9% growth)
        (110.0, True),   # exactly at threshold (≤ 1.10)
        (110.1, False),  # borderline fail (10.1% growth)
        (200.0, False),  # clear fail (100% growth)
    ])
    def test_memory_growth(self, rss_last: float, should_pass: bool) -> None:
        first = _checkpoint(idx=0, rss_mb=100.0)
        last = _checkpoint(idx=1, rss_mb=rss_last)
        results = assess_drift([first, last])
        mem = next(r for r in results if r["name"] == "memory_growth")
        assert mem["passed"] is should_pass, f"rss_last={rss_last} expected passed={should_pass}"

    # -- p99_stability -------------------------------------------------------

    @pytest.mark.parametrize("p99_last,should_pass", [
        (200.0, True),   # no change
        (299.9, True),   # borderline pass (49.95% increase)
        (300.0, True),   # exactly at threshold (≤ 1.50)
        (300.1, False),  # borderline fail
        (500.0, False),  # clear fail (150% increase)
    ])
    def test_p99_stability(self, p99_last: float, should_pass: bool) -> None:
        first = _checkpoint(idx=0, p99_ms=200.0)
        last = _checkpoint(idx=1, p99_ms=p99_last)
        results = assess_drift([first, last])
        p99 = next(r for r in results if r["name"] == "p99_stability")
        assert p99["passed"] is should_pass, f"p99_last={p99_last} expected passed={should_pass}"

    def test_p99_zero_first_is_skipped(self) -> None:
        first = _checkpoint(idx=0, p99_ms=0.0)
        last = _checkpoint(idx=1, p99_ms=0.0)
        results = assess_drift([first, last])
        p99 = next(r for r in results if r["name"] == "p99_stability")
        assert p99.get("skipped") is True

    # -- error_rate ----------------------------------------------------------

    @pytest.mark.parametrize("failures,total,should_pass", [
        (0, 1000, True),      # zero errors
        (0, 0, True),         # no requests (skipped / trivially passes)
        (1, 1001, True),      # just under 0.1 %
        (1, 999, False),      # just over 0.1 %
        (10, 100, False),     # 10% clear fail
    ])
    def test_error_rate(self, failures: int, total: int, should_pass: bool) -> None:
        first = _checkpoint(idx=0, total_failures=0, total_requests=500)
        last = _checkpoint(idx=1, total_failures=failures, total_requests=total)
        results = assess_drift([first, last])
        err = next(r for r in results if r["name"] == "error_rate")
        if total == 0:
            assert err.get("skipped") is True
        else:
            assert err["passed"] is should_pass, (
                f"failures={failures}/{total} expected passed={should_pass}"
            )


# ---------------------------------------------------------------------------
# 3. JUnit XML emission shape
# ---------------------------------------------------------------------------

class TestBuildJunit:
    def _all_pass_results(self) -> list[dict]:
        first = _checkpoint(idx=0)
        last = _checkpoint(idx=1)
        return assess_drift([first, last])

    def _failing_results(self) -> list[dict]:
        first = _checkpoint(
            idx=0, rss_mb=100.0, p99_ms=100.0, total_failures=0, total_requests=1000
        )
        last = _checkpoint(
            idx=1, rss_mb=250.0, p99_ms=400.0, total_failures=50, total_requests=1000
        )
        return assess_drift([first, last])

    def test_root_tag_is_testsuites(self) -> None:
        root = build_junit(self._all_pass_results())
        assert root.tag == "testsuites"

    def test_suite_name_is_soak_test(self) -> None:
        root = build_junit(self._all_pass_results())
        suite = _suite_from_root(root)
        assert suite.get("name") == "soak-test"

    def test_all_pass_zero_failures(self) -> None:
        root = build_junit(self._all_pass_results())
        suite = _suite_from_root(root)
        assert int(suite.get("failures", "0")) == 0
        assert _failures_in(suite) == []

    def test_all_fail_has_failure_elements(self) -> None:
        results = self._failing_results()
        failing = [r for r in results if not r["passed"] and not r.get("skipped")]
        root = build_junit(results)
        suite = _suite_from_root(root)
        assert len(_failures_in(suite)) == len(failing)

    def test_failure_element_has_actual_and_threshold(self) -> None:
        first = _checkpoint(idx=0, rss_mb=100.0)
        last = _checkpoint(idx=1, rss_mb=300.0)
        results = assess_drift([first, last])
        root = build_junit(results)
        suite = _suite_from_root(root)
        mem_fail = next(
            (f for tc in suite.findall("testcase")
             if tc.get("name") == "memory_growth"
             for f in tc.findall("failure")),
            None,
        )
        assert mem_fail is not None
        text = mem_fail.text or ""
        assert "actual" in text
        assert "threshold" in text

    def test_three_testcases(self) -> None:
        root = build_junit(self._all_pass_results())
        suite = _suite_from_root(root)
        assert len(suite.findall("testcase")) == 3

    def test_testcase_names(self) -> None:
        root = build_junit(self._all_pass_results())
        suite = _suite_from_root(root)
        names = {tc.get("name") for tc in suite.findall("testcase")}
        assert names == {"memory_growth", "p99_stability", "error_rate"}


# ---------------------------------------------------------------------------
# 4. Skipped suite (_emit_skipped_suite)
# ---------------------------------------------------------------------------

class TestSkippedSuite:
    def test_skipped_suite_structure(self) -> None:
        root = _emit_skipped_suite("soak not run")
        assert root.tag == "testsuites"
        suite = _suite_from_root(root)
        assert suite.get("name") == "soak-test"
        assert int(suite.get("tests", "0")) == 1
        assert int(suite.get("failures", "0")) == 0
        assert int(suite.get("skipped", "0")) == 1

    def test_skipped_message_propagated(self) -> None:
        reason = "compose-target not up"
        root = _emit_skipped_suite(reason)
        suite = _suite_from_root(root)
        sk_elems = _skipped_in(suite)
        assert len(sk_elems) == 1
        assert reason in (sk_elems[0].get("message") or "")


# ---------------------------------------------------------------------------
# 5. CLI skipped mode — main()
# ---------------------------------------------------------------------------

class TestMainSkippedMode:
    def test_missing_checkpoints_emits_skip(self, tmp_path: Path) -> None:
        out = tmp_path / "soak-test.xml"
        rc = junit_from_soak.main([
            "--checkpoints", str(tmp_path / "missing.jsonl"),
            "--output", str(out),
        ])
        assert rc == 0
        root = ET.parse(out).getroot()
        suite = _suite_from_root(root)
        assert int(suite.get("skipped", "0")) >= 1

    def test_explicit_skipped_flag(self, tmp_path: Path) -> None:
        out = tmp_path / "soak-test.xml"
        rc = junit_from_soak.main([
            "--skipped", "--reason", "admin chose to skip",
            "--output", str(out),
        ])
        assert rc == 0
        root = ET.parse(out).getroot()
        suite = _suite_from_root(root)
        assert int(suite.get("skipped", "0")) >= 1
