# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the P15d L6 JUnit adapters.

Covers:

- ``tools/junit_from_zap.py``     ZAP baseline JSON → JUnit XML
- ``tools/junit_from_nuclei.py``  Nuclei JSON-lines → JUnit XML

Per P15d plan: each adapter must handle (a) known-good JSON, (b) known-bad
JSON, (c) ``--skipped`` graceful-degradation mode, (d) malformed input
without crashing the gate, (e) the ZAP plugin-id whitelist (PLAN R-28).
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


junit_from_zap = _load_module(
    "junit_from_zap", TOOLS_DIR / "junit_from_zap.py",
)
junit_from_nuclei = _load_module(
    "junit_from_nuclei", TOOLS_DIR / "junit_from_nuclei.py",
)


def _parse_xml(path: Path) -> ET.Element:
    return ET.parse(path).getroot()


def _attr_int(elem: ET.Element, key: str) -> int:
    return int(elem.get(key, "0") or "0")


# ---------------------------------------------------------------------------
# ZAP adapter
# ---------------------------------------------------------------------------

class TestZapAdapter:
    """junit_from_zap.py — ZAP baseline JSON → JUnit XML."""

    # Synthesised from the documented ZAP report schema (ghcr.io/zaproxy/zaproxy
    # 2.18). Verified against `zap-baseline.py -J` output shape.
    KNOWN_GOOD = {
        "@version": "2.18.0",
        "@generated": "Sat, 23 May 2026 12:00:00",
        "site": [
            {
                "@name": "http://localhost:18001",
                "@host": "localhost",
                "@port": "18001",
                "@ssl": "false",
                "alerts": [
                    {
                        "pluginid": "40012",
                        "alert": "Cross Site Scripting (Reflected)",
                        "name": "Cross Site Scripting (Reflected)",
                        "riskcode": "3",
                        "confidence": "2",
                        "riskdesc": "High (Medium)",
                        "desc": "<p>Cross-site Scripting...</p>",
                        "count": "1",
                        "solution": "<p>Validate all input.</p>",
                        "cweid": "79",
                        "wascid": "8",
                        "instances": [
                            {
                                "uri": "http://localhost:18001/api/v0/echo",
                                "method": "GET",
                                "param": "q",
                                "evidence": "<script>alert(1)</script>",
                            }
                        ],
                    },
                    {
                        "pluginid": "10038",
                        "alert": "Content Security Policy (CSP) Header Not Set",
                        "name": "Content Security Policy (CSP) Header Not Set",
                        "riskcode": "2",
                        "confidence": "3",
                        "riskdesc": "Medium (High)",
                        "desc": "<p>CSP missing.</p>",
                        "count": "5",
                        "solution": "<p>Configure CSP.</p>",
                        "cweid": "693",
                        "wascid": "15",
                        "instances": [
                            {"uri": "http://localhost:18001/",
                             "method": "GET", "evidence": ""},
                            {"uri": "http://localhost:18001/login",
                             "method": "GET", "evidence": ""},
                        ],
                    },
                    {
                        "pluginid": "10049",
                        "alert": "Non-Storable Content",
                        "name": "Non-Storable Content",
                        "riskcode": "1",
                        "confidence": "2",
                        "riskdesc": "Low (Medium)",
                        "desc": "<p>...</p>",
                        "count": "10",
                        "solution": "<p>...</p>",
                        "instances": [{"uri": "http://localhost:18001/api",
                                       "method": "GET"}],
                    },
                    {
                        "pluginid": "10015",
                        "alert": "Re-examine Cache-control Directives",
                        "name": "Re-examine Cache-control Directives",
                        "riskcode": "0",
                        "confidence": "3",
                        "riskdesc": "Informational (High)",
                        "desc": "<p>...</p>",
                        "count": "1",
                        "instances": [],
                    },
                ],
            }
        ],
    }

    EMPTY_REPORT = {
        "@version": "2.18.0",
        "site": [
            {"@name": "http://localhost:18001", "alerts": []}
        ],
    }

    def test_known_good_high_and_medium_are_failures(self) -> None:
        suite = junit_from_zap.convert(self.KNOWN_GOOD)
        # 4 plugin ids: 40012 (HIGH), 10038 (MEDIUM), 10049 (LOW), 10015 (INFO)
        assert _attr_int(suite, "tests") == 4
        # HIGH + MEDIUM → 2 failures
        assert _attr_int(suite, "failures") == 2
        # LOW + INFORMATIONAL → 2 skipped
        assert _attr_int(suite, "skipped") == 2

        cases = {c.get("name"): c for c in suite.iter("testcase")}
        assert cases["40012"].find("failure") is not None
        assert cases["40012"].find("failure").get("type") == "zap-high"
        assert cases["10038"].find("failure") is not None
        assert cases["10038"].find("failure").get("type") == "zap-medium"
        assert cases["10049"].find("skipped") is not None
        assert cases["10015"].find("skipped") is not None

    def test_failure_body_contains_instances(self) -> None:
        suite = junit_from_zap.convert(self.KNOWN_GOOD)
        cases = {c.get("name"): c for c in suite.iter("testcase")}
        body = cases["10038"].find("failure").text or ""
        assert "http://localhost:18001/" in body
        assert "http://localhost:18001/login" in body
        assert "cwe=693" in body

    def test_whitelist_demotes_failure_to_skipped(self) -> None:
        # Whitelist plugin id 40012 (the HIGH XSS): it should become skipped
        # rather than failure, even though severity is HIGH.
        suite = junit_from_zap.convert(self.KNOWN_GOOD, whitelist={"40012"})
        cases = {c.get("name"): c for c in suite.iter("testcase")}
        assert cases["40012"].find("failure") is None
        assert cases["40012"].find("skipped") is not None
        # Only the MEDIUM remains as a failure.
        assert _attr_int(suite, "failures") == 1
        assert _attr_int(suite, "skipped") == 3

    def test_empty_report_yields_no_findings(self) -> None:
        suite = junit_from_zap.convert(self.EMPTY_REPORT)
        assert _attr_int(suite, "tests") == 1
        assert _attr_int(suite, "failures") == 0
        cases = list(suite.iter("testcase"))
        assert cases[0].get("name") == "no-findings"

    def test_skipped_mode_emits_graceful_skip(self, tmp_path: Path) -> None:
        out = tmp_path / "zap.xml"
        rc = junit_from_zap.main(["--skipped", "--output", str(out)])
        assert rc == 0
        root = _parse_xml(out)
        suite = root.find("testsuite")
        assert suite is not None
        case = suite.find("testcase")
        assert case is not None
        assert case.get("name") == "zap-not-installed"
        assert case.find("skipped") is not None

    def test_malformed_json_returns_nonzero(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("definitely not json", encoding="utf-8")
        out = tmp_path / "out.xml"
        rc = junit_from_zap.main(["--input", str(bad), "--output", str(out)])
        assert rc == 1

    def test_missing_input_emits_skipped(self, tmp_path: Path) -> None:
        out = tmp_path / "out.xml"
        rc = junit_from_zap.main([
            "--input", str(tmp_path / "does-not-exist.json"),
            "--output", str(out),
        ])
        # Missing input is a graceful skip, not a crash.
        assert rc == 0
        root = _parse_xml(out)
        case = root.find("testsuite/testcase")
        assert case is not None
        assert case.find("skipped") is not None

    def test_whitelist_file_loading(self, tmp_path: Path) -> None:
        wl_path = tmp_path / "zap-baseline.conf"
        wl_path.write_text(
            "# comment line\n"
            "40012  # whitelist the XSS for the test\n"
            "\n"
            "10038\n",
            encoding="utf-8",
        )
        # Use the module-private helper indirectly through main().
        wl = junit_from_zap._load_whitelist(wl_path)
        assert wl == {"40012", "10038"}

    def test_suite_name_matches_layer_contract(self, tmp_path: Path) -> None:
        out = tmp_path / "zap.xml"
        junit_from_zap.main(["--skipped", "--output", str(out)])
        root = _parse_xml(out)
        suite = root.find("testsuite")
        assert suite is not None
        assert suite.get("name") == "l6-zap"


# ---------------------------------------------------------------------------
# Nuclei adapter
# ---------------------------------------------------------------------------

class TestNucleiAdapter:
    """junit_from_nuclei.py — Nuclei JSON-lines → JUnit XML."""

    # Synthesised from the documented Nuclei 3.4 JSON-lines schema.
    KNOWN_GOOD_LINES = [
        {
            "template-id": "panel-detect/grafana-detect",
            "template": "/nuclei-templates/http/exposed-panels/grafana-detect.yaml",
            "info": {
                "name": "Grafana Default Login",
                "severity": "high",
                "tags": ["panel", "grafana"],
                "description": "Grafana panel exposed.",
            },
            "type": "http",
            "host": "http://localhost:18001",
            "matched-at": "http://localhost:18001/login",
        },
        {
            "template-id": "ssl/tls-version",
            "template": "/nuclei-templates/ssl/tls-version.yaml",
            "info": {
                "name": "TLS Version Detection",
                "severity": "info",
                "tags": ["ssl", "tls"],
            },
            "type": "ssl",
            "host": "https://localhost:18001",
            "matched-at": "https://localhost:18001",
        },
        {
            "template-id": "cves/2024/CVE-2024-9999",
            "template": "/nuclei-templates/http/cves/2024/CVE-2024-9999.yaml",
            "info": {
                "name": "Some critical CVE",
                "severity": "critical",
                "tags": ["cve", "cve2024"],
                "description": "Critical RCE in widget v1.",
            },
            "type": "http",
            "host": "http://localhost:18001",
            "matched-at": "http://localhost:18001/admin",
        },
        # Same template id hit on a second URI — should be collapsed.
        {
            "template-id": "panel-detect/grafana-detect",
            "info": {"name": "Grafana Default Login", "severity": "high",
                     "tags": ["panel", "grafana"]},
            "type": "http",
            "host": "http://localhost:18001",
            "matched-at": "http://localhost:18001/grafana/login",
        },
    ]

    def _as_jsonl(self) -> str:
        return "\n".join(json.dumps(r) for r in self.KNOWN_GOOD_LINES) + "\n"

    def test_known_good_critical_and_high_are_failures(self) -> None:
        suite = junit_from_nuclei.convert(self.KNOWN_GOOD_LINES)
        # 3 distinct template-ids (grafana-detect collapsed).
        assert _attr_int(suite, "tests") == 3
        # critical + high → 2 failures; info → 1 skipped.
        assert _attr_int(suite, "failures") == 2
        assert _attr_int(suite, "skipped") == 1

        cases = {c.get("name"): c for c in suite.iter("testcase")}
        assert cases["cves/2024/CVE-2024-9999"].find("failure") is not None
        assert (cases["cves/2024/CVE-2024-9999"].find("failure").get("type")
                == "nuclei-critical")
        assert cases["panel-detect/grafana-detect"].find("failure") is not None
        assert (cases["panel-detect/grafana-detect"].find("failure").get("type")
                == "nuclei-high")
        assert cases["ssl/tls-version"].find("skipped") is not None

    def test_duplicate_template_ids_are_collapsed(self) -> None:
        suite = junit_from_nuclei.convert(self.KNOWN_GOOD_LINES)
        cases = {c.get("name"): c for c in suite.iter("testcase")}
        grafana = cases["panel-detect/grafana-detect"]
        body = grafana.find("failure").text or ""
        # Both URIs should appear in the body.
        assert "http://localhost:18001/login" in body
        assert "http://localhost:18001/grafana/login" in body

    def test_empty_lines_yields_no_findings_testcase(self) -> None:
        suite = junit_from_nuclei.convert([])
        assert _attr_int(suite, "tests") == 1
        assert _attr_int(suite, "failures") == 0
        cases = list(suite.iter("testcase"))
        assert cases[0].get("name") == "no-findings"

    def test_jsonl_parser_ignores_blank_and_malformed_lines(self) -> None:
        raw = (
            "\n"
            "# this is a comment\n"
            '{"template-id":"x","info":{"severity":"critical","name":"X"}}\n'
            "not valid json\n"
            '{"template-id":"y","info":{"severity":"low","name":"Y"}}\n'
        )
        recs = junit_from_nuclei._parse_jsonl(raw)
        assert [r["template-id"] for r in recs] == ["x", "y"]

    def test_skipped_mode_emits_graceful_skip(self, tmp_path: Path) -> None:
        out = tmp_path / "nuclei.xml"
        rc = junit_from_nuclei.main(["--skipped", "--output", str(out)])
        assert rc == 0
        root = _parse_xml(out)
        case = root.find("testsuite/testcase")
        assert case is not None
        assert case.get("name") == "nuclei-not-installed"
        assert case.find("skipped") is not None

    def test_missing_input_emits_skipped(self, tmp_path: Path) -> None:
        out = tmp_path / "out.xml"
        rc = junit_from_nuclei.main([
            "--input", str(tmp_path / "no-such.json"),
            "--output", str(out),
        ])
        assert rc == 0
        root = _parse_xml(out)
        case = root.find("testsuite/testcase")
        assert case is not None
        assert case.find("skipped") is not None

    def test_empty_input_file_yields_no_findings(self, tmp_path: Path) -> None:
        # Nuclei writes nothing when no templates match — that's a legitimate
        # "clean scan" outcome, not a degradation.
        empty = tmp_path / "nuclei.jsonl"
        empty.write_text("", encoding="utf-8")
        out = tmp_path / "out.xml"
        rc = junit_from_nuclei.main(["--input", str(empty), "--output", str(out)])
        assert rc == 0
        root = _parse_xml(out)
        case = root.find("testsuite/testcase")
        assert case is not None
        assert case.get("name") == "no-findings"

    def test_cli_reads_input_file(self, tmp_path: Path) -> None:
        inp = tmp_path / "nuclei.jsonl"
        inp.write_text(self._as_jsonl(), encoding="utf-8")
        out = tmp_path / "out.xml"
        rc = junit_from_nuclei.main(["--input", str(inp), "--output", str(out)])
        assert rc == 0
        root = _parse_xml(out)
        suite = root.find("testsuite")
        assert suite is not None
        assert _attr_int(suite, "failures") == 2

    def test_suite_name_matches_layer_contract(self, tmp_path: Path) -> None:
        out = tmp_path / "nuclei.xml"
        junit_from_nuclei.main(["--skipped", "--output", str(out)])
        root = _parse_xml(out)
        suite = root.find("testsuite")
        assert suite is not None
        assert suite.get("name") == "l6-nuclei"


# ---------------------------------------------------------------------------
# Marker
# ---------------------------------------------------------------------------

def test_module_imports_resolved() -> None:
    """Each adapter module should expose its main + convert symbols."""
    assert callable(junit_from_zap.main)
    assert callable(junit_from_zap.convert)
    assert callable(junit_from_nuclei.main)
    assert callable(junit_from_nuclei.convert)
    assert pytest is not None
