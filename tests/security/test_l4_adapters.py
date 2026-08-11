# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the L4 JUnit adapters.

Covers:

- ``tools/junit_from_trivy.py``     vulnerability / secret / misconfig conversion
- ``tools/junit_from_sbom.py``       SPDX + CycloneDX validation + sha256 metadata
- ``tools/junit_from_dockle.py``    Dockerfile-hardening linter conversion

Each adapter must handle (a) known-good JSON, (b) known-bad
JSON, (c) ``--skipped`` graceful-degradation mode, and (d) malformed input
without crashing the gate.
"""
from __future__ import annotations

import importlib.util as _ilu
import json
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"


def _load_module(name: str, file: Path):
    spec = _ilu.spec_from_file_location(name, file)
    assert spec is not None and spec.loader is not None
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


junit_from_trivy = _load_module(
    "junit_from_trivy", TOOLS_DIR / "junit_from_trivy.py",
)
junit_from_sbom = _load_module(
    "junit_from_sbom", TOOLS_DIR / "junit_from_sbom.py",
)
junit_from_dockle = _load_module(
    "junit_from_dockle", TOOLS_DIR / "junit_from_dockle.py",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_xml(path: Path) -> ET.Element:
    return ET.parse(path).getroot()


def _attr_int(elem: ET.Element, key: str) -> int:
    return int(elem.get(key, "0") or "0")


# ---------------------------------------------------------------------------
# Trivy adapter
# ---------------------------------------------------------------------------

class TestTrivyAdapter:
    """junit_from_trivy.py — vulnerability / secret / misconfig conversion."""

    KNOWN_GOOD = {
        "ArtifactName": "deploy-lmchat:latest",
        "ArtifactType": "container_image",
        "Results": [
            {
                "Target": "deploy-lmchat:latest (debian 12)",
                "Class": "os-pkgs",
                "Type": "debian",
                "Vulnerabilities": [
                    {
                        "VulnerabilityID": "CVE-2024-9999",
                        "PkgName": "libfoo",
                        "Severity": "CRITICAL",
                        "Title": "libfoo overflow",
                        "InstalledVersion": "1.2.3",
                        "FixedVersion": "1.2.4",
                    },
                    {
                        "VulnerabilityID": "CVE-2024-0001",
                        "PkgName": "libbar",
                        "Severity": "MEDIUM",
                        "Title": "libbar info leak",
                        "InstalledVersion": "0.5",
                        "FixedVersion": "",
                    },
                ],
                "Secrets": [
                    {
                        "RuleID": "aws-access-key-id",
                        "Severity": "HIGH",
                        "Title": "AWS access key",
                        "Match": "AKIA...redacted...",
                    }
                ],
                "Misconfigurations": [
                    {
                        "ID": "DS002",
                        "Severity": "LOW",
                        "Title": "Image runs as root",
                        "Resolution": "USER nonroot",
                    }
                ],
            }
        ],
    }

    EMPTY_REPORT = {"ArtifactName": "x", "Results": []}

    def test_known_good_yields_failures_for_critical_high(self, tmp_path: Path) -> None:
        suite = junit_from_trivy.convert(self.KNOWN_GOOD)
        # 4 distinct ids: CVE-2024-9999 (CRIT), CVE-2024-0001 (MED),
        # aws-access-key-id (HIGH), DS002 (LOW). Expect failures=2 (CRIT + HIGH).
        assert _attr_int(suite, "tests") == 4
        assert _attr_int(suite, "failures") == 2
        assert _attr_int(suite, "skipped") == 2

        cases = list(suite.iter("testcase"))
        names = {c.get("name") for c in cases}
        assert "CVE-2024-9999" in names
        assert "aws-access-key-id" in names
        assert "DS002" in names

        # The CRITICAL CVE should have a <failure>; the LOW misconfig a <skipped>.
        crit = next(c for c in cases if c.get("name") == "CVE-2024-9999")
        assert crit.find("failure") is not None
        low = next(c for c in cases if c.get("name") == "DS002")
        assert low.find("skipped") is not None

    def test_empty_report_yields_no_findings_testcase(self, tmp_path: Path) -> None:
        suite = junit_from_trivy.convert(self.EMPTY_REPORT)
        assert _attr_int(suite, "tests") == 1
        assert _attr_int(suite, "failures") == 0
        cases = list(suite.iter("testcase"))
        assert len(cases) == 1
        assert cases[0].get("name") == "no-findings"

    def test_skipped_mode_emits_graceful_skip(self, tmp_path: Path) -> None:
        out = tmp_path / "trivy.xml"
        rc = junit_from_trivy.main(["--skipped", "--output", str(out)])
        assert rc == 0
        root = _parse_xml(out)
        assert root.tag == "testsuites"
        suite = root.find("testsuite")
        assert suite is not None
        case = suite.find("testcase")
        assert case is not None
        assert case.get("name") == "trivy-not-installed"
        assert case.find("skipped") is not None

    def test_malformed_json_returns_nonzero(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("not a json document", encoding="utf-8")
        out = tmp_path / "out.xml"
        rc = junit_from_trivy.main(["--input", str(bad), "--output", str(out)])
        assert rc == 1


# ---------------------------------------------------------------------------
# Syft / SBOM adapter
# ---------------------------------------------------------------------------

class TestSbomAdapter:
    """junit_from_sbom.py — SBOM validation + sha256 metadata."""

    SPDX_GOOD = {
        "spdxVersion": "SPDX-2.3",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "deploy-lmchat",
        "packages": [
            {"name": "libfoo", "versionInfo": "1.2.3"},
            {"name": "libbar", "versionInfo": "0.5"},
        ],
    }

    CDX_GOOD = {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "components": [
            {"name": "libfoo", "version": "1.2.3", "type": "library"},
        ],
    }

    def test_known_good_spdx_and_cyclonedx(self, tmp_path: Path) -> None:
        spdx = tmp_path / "sbom.spdx.json"
        cdx = tmp_path / "sbom.cyclonedx.json"
        spdx.write_text(json.dumps(self.SPDX_GOOD), encoding="utf-8")
        cdx.write_text(json.dumps(self.CDX_GOOD), encoding="utf-8")

        suite = junit_from_sbom.convert([spdx, cdx])
        assert _attr_int(suite, "tests") == 2
        assert _attr_int(suite, "failures") == 0

        cases = list(suite.iter("testcase"))
        # Both files should be recognised: one spdx, one cyclonedx.
        classnames = {c.get("classname") for c in cases}
        assert "syft.spdx" in classnames
        assert "syft.cyclonedx" in classnames

        # sha256 + packages count appear in system-out.
        for c in cases:
            so = c.find("system-out")
            assert so is not None and so.text is not None
            assert "sha256=" in so.text
            assert "packages=" in so.text

    def test_empty_sbom_fails(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.spdx.json"
        empty.write_text(json.dumps({"spdxVersion": "SPDX-2.3", "packages": []}),
                         encoding="utf-8")
        suite = junit_from_sbom.convert([empty])
        assert _attr_int(suite, "failures") == 1
        cases = list(suite.iter("testcase"))
        assert cases[0].find("failure") is not None

    def test_missing_file_fails(self, tmp_path: Path) -> None:
        suite = junit_from_sbom.convert([tmp_path / "does-not-exist.json"])
        assert _attr_int(suite, "failures") == 1

    def test_unknown_format_fails(self, tmp_path: Path) -> None:
        bad = tmp_path / "weird.json"
        bad.write_text(json.dumps({"hello": "world"}), encoding="utf-8")
        suite = junit_from_sbom.convert([bad])
        assert _attr_int(suite, "failures") == 1

    def test_skipped_mode_emits_graceful_skip(self, tmp_path: Path) -> None:
        out = tmp_path / "syft.xml"
        rc = junit_from_sbom.main(["--skipped", "--output", str(out)])
        assert rc == 0
        root = _parse_xml(out)
        case = root.find("testsuite/testcase")
        assert case is not None
        assert case.get("name") == "syft-not-installed"
        assert case.find("skipped") is not None


# ---------------------------------------------------------------------------
# Dockle adapter
# ---------------------------------------------------------------------------

class TestDockleAdapter:
    """junit_from_dockle.py — Dockerfile hardening linter conversion."""

    KNOWN_GOOD = {
        "summary": {"fatal": 1, "warn": 1, "info": 1, "skip": 0, "pass": 1},
        "details": [
            {
                "code": "CIS-DI-0001",
                "title": "Create a user for the container",
                "level": "FATAL",
                "alerts": ["Last user should not be root"],
            },
            {
                "code": "DKL-DI-0006",
                "title": "Avoid latest tag",
                "level": "WARN",
                "alerts": ["Avoid 'latest' tag"],
            },
            {
                "code": "CIS-DI-0005",
                "title": "Enable Content trust",
                "level": "INFO",
                "alerts": ["Enable DOCKER_CONTENT_TRUST"],
            },
            {
                "code": "CIS-DI-0008",
                "title": "Confirm setuid removal",
                "level": "PASS",
                "alerts": [],
            },
        ],
    }

    NO_FINDINGS = {"summary": {"pass": 50}, "details": []}

    def test_known_good_marks_fatal_and_warn_as_failures(self) -> None:
        suite = junit_from_dockle.convert(self.KNOWN_GOOD)
        assert _attr_int(suite, "tests") == 4
        # FATAL + WARN = 2 failures
        assert _attr_int(suite, "failures") == 2
        # INFO = 1 skip; PASS is system-out only
        assert _attr_int(suite, "skipped") == 1

        cases = {c.get("name"): c for c in suite.iter("testcase")}
        assert cases["CIS-DI-0001"].find("failure") is not None
        assert cases["DKL-DI-0006"].find("failure") is not None
        assert cases["CIS-DI-0005"].find("skipped") is not None
        assert cases["CIS-DI-0008"].find("system-out") is not None

    def test_no_details_yields_no_findings(self) -> None:
        suite = junit_from_dockle.convert(self.NO_FINDINGS)
        assert _attr_int(suite, "tests") == 1
        cases = list(suite.iter("testcase"))
        assert cases[0].get("name") == "no-findings"

    def test_skipped_mode_emits_graceful_skip(self, tmp_path: Path) -> None:
        out = tmp_path / "dockle.xml"
        rc = junit_from_dockle.main(["--skipped", "--output", str(out)])
        assert rc == 0
        root = _parse_xml(out)
        case = root.find("testsuite/testcase")
        assert case is not None
        assert case.get("name") == "dockle-not-installed"

    def test_malformed_json_returns_nonzero(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("this is not json", encoding="utf-8")
        out = tmp_path / "out.xml"
        rc = junit_from_dockle.main(["--input", str(bad), "--output", str(out)])
        assert rc == 1


# ---------------------------------------------------------------------------
# Suite-name contract (layer-attribution invariant)
# ---------------------------------------------------------------------------

class TestSuiteNamesMatchLayer:
    """The L9 aggregator attributes findings via testsuite name=l<N>-<tool>."""

    def test_trivy_suite_name(self, tmp_path: Path) -> None:
        out = tmp_path / "trivy.xml"
        junit_from_trivy.main(["--skipped", "--output", str(out)])
        root = _parse_xml(out)
        suite = root.find("testsuite")
        assert suite is not None
        assert suite.get("name") == "l4-trivy"

    def test_syft_suite_name(self, tmp_path: Path) -> None:
        out = tmp_path / "syft.xml"
        junit_from_sbom.main(["--skipped", "--output", str(out)])
        root = _parse_xml(out)
        suite = root.find("testsuite")
        assert suite is not None
        assert suite.get("name") == "l4-syft"

    def test_dockle_suite_name(self, tmp_path: Path) -> None:
        out = tmp_path / "dockle.xml"
        junit_from_dockle.main(["--skipped", "--output", str(out)])
        root = _parse_xml(out)
        suite = root.find("testsuite")
        assert suite is not None
        assert suite.get("name") == "l4-dockle"


# ---------------------------------------------------------------------------
# CLI smoke
# ---------------------------------------------------------------------------

def test_trivy_cli_reads_input_file(tmp_path: Path) -> None:
    inp = tmp_path / "trivy.json"
    inp.write_text(json.dumps(TestTrivyAdapter.KNOWN_GOOD), encoding="utf-8")
    out = tmp_path / "out.xml"
    rc = junit_from_trivy.main(["--input", str(inp), "--output", str(out)])
    assert rc == 0
    root = _parse_xml(out)
    suite = root.find("testsuite")
    assert suite is not None
    assert _attr_int(suite, "failures") == 2


def test_sbom_cli_handles_zero_paths(tmp_path: Path) -> None:
    out = tmp_path / "sbom.xml"
    # Zero --sbom args → emit a no-sboms-provided testcase.
    rc = junit_from_sbom.main(["--output", str(out)])
    assert rc == 0
    root = _parse_xml(out)
    case = root.find("testsuite/testcase")
    assert case is not None
    assert case.get("name") == "no-sboms-provided"


def test_dockle_cli_reads_input_file(tmp_path: Path) -> None:
    inp = tmp_path / "dockle.json"
    inp.write_text(json.dumps(TestDockleAdapter.KNOWN_GOOD), encoding="utf-8")
    out = tmp_path / "out.xml"
    rc = junit_from_dockle.main(["--input", str(inp), "--output", str(out)])
    assert rc == 0
    root = _parse_xml(out)
    suite = root.find("testsuite")
    assert suite is not None
    assert _attr_int(suite, "failures") == 2


# ---------------------------------------------------------------------------
# Marker: ensure pytest module import isn't unused
# ---------------------------------------------------------------------------

def test_module_imports_resolved() -> None:
    """Defensive: each adapter module should have its main + convert symbols."""
    assert callable(junit_from_trivy.main)
    assert callable(junit_from_trivy.convert)
    assert callable(junit_from_sbom.main)
    assert callable(junit_from_sbom.convert)
    assert callable(junit_from_dockle.main)
    assert callable(junit_from_dockle.convert)
    # Silence "unused import" for pytest fixture API.
    assert pytest is not None
