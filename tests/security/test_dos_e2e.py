# SPDX-License-Identifier: Apache-2.0
"""End-to-end pytest tests for P15f DoS tools against an in-process TestClient.

Invokes the three pure-Python DoS tools (redos, decompression, json_bomb)
against a real FastAPI TestClient (no external Docker / slowhttptest required).
slowhttptest E2E is skipped here per the PLAN brief (Docker-only).

Each test suite must run in < 30 s total.
"""
from __future__ import annotations

import importlib.util as _ilu
import os
import sys
import time
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"


def _load_module(name: str, file: Path) -> object:
    spec = _ilu.spec_from_file_location(name, file)
    assert spec is not None and spec.loader is not None
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


redos_fuzzer = _load_module("redos_fuzzer", TOOLS_DIR / "redos_fuzzer.py")
decomp_test = _load_module("decompression_bomb_test", TOOLS_DIR / "decompression_bomb_test.py")
json_bomb = _load_module("json_bomb_test", TOOLS_DIR / "json_bomb_test.py")


def _get_suite(root: ET.Element) -> ET.Element:
    suite = root.find("testsuite")
    assert suite is not None
    return suite


def _attr_int(elem: ET.Element, key: str) -> int:
    return int(elem.get(key, "0") or "0")


# ---------------------------------------------------------------------------
# Minimal env so the app can import without a real secret.
# ---------------------------------------------------------------------------

os.environ.setdefault("LM_CHAT_SECRET", "dos-e2e-secret-000000000000000000000000000000")
os.environ.setdefault("LM_STUDIO_BASE_URL", "http://localhost:1234")
os.environ.setdefault("LM_STUDIO_API_KEY", "sk-dos-e2e-placeholder")


# ===========================================================================
# ReDoS fuzzer — runs against pure Python patterns, no server needed
# ===========================================================================

class TestRedosFuzzerE2E:
    """Run the ReDoS fuzzer end-to-end against the real pattern inventory."""

    def test_all_patterns_pass_under_50ms(self, tmp_path: Path) -> None:
        """All lm-chat regexes must complete under 50 ms on pathological inputs."""
        out = tmp_path / "redos.xml"
        t0 = time.perf_counter()
        rc = redos_fuzzer.main(["--output", str(out)])  # type: ignore[attr-defined]
        elapsed = time.perf_counter() - t0
        assert elapsed < 30.0, f"redos_fuzzer took {elapsed:.1f}s (limit 30s)"

        root = ET.parse(out).getroot()
        suite = _get_suite(root)
        failures = _attr_int(suite, "failures")

        # Collect failure details for the assertion message.
        fail_details: list[str] = []
        for tc in suite.findall("testcase"):
            fail_el = tc.find("failure")
            if fail_el is not None:
                name = tc.get("name", "?")
                fail_details.append(f"{name}: {fail_el.get('message', '')}")

        assert rc == 0, (
            f"redos_fuzzer found {failures} pattern(s) exceeding 50ms budget:\n"
            + "\n".join(fail_details)
        )
        assert failures == 0

    def test_skipped_mode_produces_valid_xml(self, tmp_path: Path) -> None:
        out = tmp_path / "redos_skip.xml"
        rc = redos_fuzzer.main(["--skipped", "--output", str(out)])  # type: ignore[attr-defined]
        assert rc == 0
        root = ET.parse(out).getroot()
        suite = _get_suite(root)
        assert _attr_int(suite, "skipped") == 1


# ===========================================================================
# Decompression bomb — runs against in-process TestClient
# ===========================================================================

class TestDecompressionBombE2E:
    """Upload gzip/zip bombs to the in-process app; assert 4xx rejection."""

    def test_bombs_rejected_by_testclient(self, tmp_path: Path) -> None:
        out = tmp_path / "decomp.xml"
        t0 = time.perf_counter()
        rc = decomp_test.main(["--output", str(out)])  # type: ignore[attr-defined]
        elapsed = time.perf_counter() - t0
        assert elapsed < 30.0, f"decompression_bomb_test took {elapsed:.1f}s (limit 30s)"

        root = ET.parse(out).getroot()
        suite = _get_suite(root)
        failures = _attr_int(suite, "failures")

        fail_details: list[str] = []
        for tc in suite.findall("testcase"):
            fail_el = tc.find("failure")
            if fail_el is not None:
                name = tc.get("name", "?")
                fail_details.append(f"{name}: {fail_el.get('message', '')}")

        # If the app could not be booted (missing deps in CI), skip rather than fail.
        # Detect this by checking for the infra-setup case.
        infra_tc = suite.find(".//testcase[@name='infra-setup']")
        if infra_tc is not None and infra_tc.find("failure") is not None:
            pytest.skip("app could not be imported in this environment")

        assert rc == 0, (
            f"decompression_bomb_test: {failures} bomb(s) not rejected:\n"
            + "\n".join(fail_details)
        )

    def test_skipped_mode_xml_valid(self, tmp_path: Path) -> None:
        out = tmp_path / "decomp_skip.xml"
        rc = decomp_test.main(["--skipped", "--output", str(out)])  # type: ignore[attr-defined]
        assert rc == 0
        root = ET.parse(out).getroot()
        assert root.tag == "testsuites"


# ===========================================================================
# JSON bomb — runs against in-process TestClient
# ===========================================================================

class TestJsonBombE2E:
    """Send deeply-nested / massive JSON payloads; assert no 5xx."""

    def test_json_bombs_do_not_cause_5xx(self, tmp_path: Path) -> None:
        out = tmp_path / "jsonbomb.xml"
        t0 = time.perf_counter()
        rc = json_bomb.main(["--output", str(out)])  # type: ignore[attr-defined]
        elapsed = time.perf_counter() - t0
        assert elapsed < 30.0, f"json_bomb_test took {elapsed:.1f}s (limit 30s)"

        root = ET.parse(out).getroot()
        suite = _get_suite(root)
        failures = _attr_int(suite, "failures")

        # Detect infra-setup failure (app couldn't be imported).
        infra_tc = suite.find(".//testcase[@name='infra-setup']")
        if infra_tc is not None and infra_tc.find("failure") is not None:
            pytest.skip("app could not be imported in this environment")

        fail_details: list[str] = []
        for tc in suite.findall("testcase"):
            fail_el = tc.find("failure")
            if fail_el is not None:
                name = tc.get("name", "?")
                fail_details.append(f"{name}: {fail_el.get('message', '')}")

        assert rc == 0, (
            f"json_bomb_test: {failures} endpoint(s) returned 5xx:\n"
            + "\n".join(fail_details)
        )

    def test_skipped_mode_xml_valid(self, tmp_path: Path) -> None:
        out = tmp_path / "jsonbomb_skip.xml"
        rc = json_bomb.main(["--skipped", "--output", str(out)])  # type: ignore[attr-defined]
        assert rc == 0
        root = ET.parse(out).getroot()
        suite = _get_suite(root)
        assert _attr_int(suite, "skipped") == 1


# ===========================================================================
# Slowhttptest — skipped in E2E (Docker-only per PLAN brief)
# ===========================================================================

@pytest.mark.skip(reason="slowhttptest E2E is Docker-only; unit tests cover the adapter parsing")
def test_slowhttptest_e2e_skipped() -> None:
    """Placeholder to document that the slowhttptest E2E is intentionally skipped here."""
