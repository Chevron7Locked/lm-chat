# SPDX-License-Identifier: Apache-2.0
"""Unit tests for the L1 license-bill JUnit adapters.

Covers:

- ``tools/junit_from_sbom_licenses.py``  SBOM license gate (SPDX + CycloneDX)

The L1 pip-licenses sub-check reuses ``junit_from_pip_licenses.py`` directly
(already tested in ``test_l3_adapters.py``); no duplication here.

All tests are offline — no binaries, no network, no filesystem side-effects
beyond tmp_path.
"""
from __future__ import annotations

import importlib.util as _ilu
import json
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"


def _load_module(name: str, file: Path):
    spec = _ilu.spec_from_file_location(name, file)
    assert spec is not None and spec.loader is not None
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


junit_from_sbom_licenses = _load_module(
    "junit_from_sbom_licenses", TOOLS_DIR / "junit_from_sbom_licenses.py"
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_xml(path: Path) -> ET.Element:
    return ET.parse(path).getroot()


def _attr_int(elem: ET.Element, key: str) -> int:
    return int(elem.get(key, "0") or "0")


def _write_sbom(path: Path, doc: dict) -> None:
    path.write_text(json.dumps(doc), encoding="utf-8")


# ---------------------------------------------------------------------------
# Minimal fixture SBOMs
# ---------------------------------------------------------------------------

def _spdx_doc(packages: list[dict]) -> dict:
    """Minimal valid SPDX-JSON document."""
    return {
        "spdxVersion": "SPDX-2.3",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "test-sbom",
        "dataLicense": "CC0-1.0",
        "packages": packages,
    }


def _spdx_pkg(name: str, version: str, license_concluded: str) -> dict:
    return {
        "SPDXID": f"SPDXRef-{name}",
        "name": name,
        "versionInfo": version,
        "licenseConcluded": license_concluded,
        "licenseDeclared": license_concluded,
        "externalRefs": [],
    }


def _cdx_doc(components: list[dict]) -> dict:
    """Minimal valid CycloneDX-JSON document."""
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "version": 1,
        "components": components,
    }


def _cdx_comp(name: str, version: str, licenses: list[dict], purl: str = "") -> dict:
    comp: dict = {
        "type": "library",
        "name": name,
        "version": version,
        "licenses": licenses,
    }
    if purl:
        comp["purl"] = purl
    return comp


def _cdx_license(spdx_id: str) -> dict:
    return {"license": {"id": spdx_id}}


# ---------------------------------------------------------------------------
# SPDX parsing
# ---------------------------------------------------------------------------

class TestSpdxParsing:
    """SPDX SBOM parsing and license classification."""

    def test_spdx_mit_passes(self) -> None:
        doc = _spdx_doc([_spdx_pkg("fastapi", "0.100.0", "MIT")])
        fmt, pkgs = junit_from_sbom_licenses.extract_packages(doc)
        assert fmt == "spdx"
        assert len(pkgs) == 1
        assert pkgs[0]["license"] == "MIT"
        assert not junit_from_sbom_licenses._is_blocked("MIT")

    def test_spdx_apache_passes(self) -> None:
        doc = _spdx_doc([_spdx_pkg("requests", "2.31.0", "Apache-2.0")])
        _, pkgs = junit_from_sbom_licenses.extract_packages(doc)
        assert pkgs[0]["license"] == "Apache-2.0"
        assert not junit_from_sbom_licenses._is_blocked("Apache-2.0")

    def test_spdx_bsd_passes(self) -> None:
        doc = _spdx_doc([_spdx_pkg("werkzeug", "3.0.0", "BSD-3-Clause")])
        _, pkgs = junit_from_sbom_licenses.extract_packages(doc)
        assert not junit_from_sbom_licenses._is_blocked(pkgs[0]["license"])

    def test_spdx_gpl_fails(self) -> None:
        doc = _spdx_doc([_spdx_pkg("some-gpl", "1.0", "GPL-3.0-only")])
        _, pkgs = junit_from_sbom_licenses.extract_packages(doc)
        assert junit_from_sbom_licenses._is_blocked(pkgs[0]["license"])

    def test_spdx_lgpl_fails(self) -> None:
        doc = _spdx_doc([_spdx_pkg("lgpl-lib", "2.0", "LGPL-2.1-or-later")])
        _, pkgs = junit_from_sbom_licenses.extract_packages(doc)
        assert junit_from_sbom_licenses._is_blocked(pkgs[0]["license"])

    def test_spdx_noassertion_is_unknown(self) -> None:
        doc = _spdx_doc([_spdx_pkg("mystery", "0.1", "NOASSERTION")])
        _, pkgs = junit_from_sbom_licenses.extract_packages(doc)
        assert junit_from_sbom_licenses._is_unknown(pkgs[0]["license"])


# ---------------------------------------------------------------------------
# CycloneDX parsing
# ---------------------------------------------------------------------------

class TestCycloneDxParsing:
    """CycloneDX SBOM parsing and license classification."""

    def test_cdx_mit_passes(self) -> None:
        doc = _cdx_doc([
            _cdx_comp("flask", "3.0.0", [_cdx_license("MIT")], purl="pkg:pypi/flask@3.0.0"),
        ])
        fmt, pkgs = junit_from_sbom_licenses.extract_packages(doc)
        assert fmt == "cyclonedx"
        assert pkgs[0]["license"] == "MIT"
        assert pkgs[0]["origin"] == "python"

    def test_cdx_apache_passes(self) -> None:
        doc = _cdx_doc([
            _cdx_comp("uvicorn", "0.29.0", [_cdx_license("Apache-2.0")]),
        ])
        _, pkgs = junit_from_sbom_licenses.extract_packages(doc)
        assert not junit_from_sbom_licenses._is_blocked(pkgs[0]["license"])

    def test_cdx_gpl_fails(self) -> None:
        doc = _cdx_doc([
            _cdx_comp("os-pkg", "1.0", [_cdx_license("GPL-2.0-only")], purl="pkg:apk/os-pkg@1.0"),
        ])
        _, pkgs = junit_from_sbom_licenses.extract_packages(doc)
        assert junit_from_sbom_licenses._is_blocked(pkgs[0]["license"])

    def test_cdx_agpl_fails(self) -> None:
        doc = _cdx_doc([
            _cdx_comp("agpl-svc", "1.0", [_cdx_license("AGPL-3.0-or-later")]),
        ])
        _, pkgs = junit_from_sbom_licenses.extract_packages(doc)
        assert junit_from_sbom_licenses._is_blocked(pkgs[0]["license"])

    def test_cdx_expression_field(self) -> None:
        """CycloneDX licenses with 'expression' key rather than nested license.id."""
        doc = _cdx_doc([
            _cdx_comp("expr-lib", "1.0", [{"expression": "MIT OR Apache-2.0"}]),
        ])
        _, pkgs = junit_from_sbom_licenses.extract_packages(doc)
        assert pkgs[0]["license"] == "MIT OR Apache-2.0"
        assert not junit_from_sbom_licenses._is_blocked(pkgs[0]["license"])

    def test_cdx_empty_licenses_is_unknown(self) -> None:
        doc = _cdx_doc([
            _cdx_comp("no-lic", "1.0", []),
        ])
        _, pkgs = junit_from_sbom_licenses.extract_packages(doc)
        assert junit_from_sbom_licenses._is_unknown(pkgs[0]["license"])

    def test_cdx_origin_python_from_purl(self) -> None:
        doc = _cdx_doc([
            _cdx_comp("pydantic", "2.0.0", [_cdx_license("MIT")], purl="pkg:pypi/pydantic@2.0.0"),
        ])
        _, pkgs = junit_from_sbom_licenses.extract_packages(doc)
        assert pkgs[0]["origin"] == "python"

    def test_cdx_origin_os_from_purl(self) -> None:
        doc = _cdx_doc([
            _cdx_comp("musl", "1.2.3", [_cdx_license("MIT")], purl="pkg:apk/musl@1.2.3"),
        ])
        _, pkgs = junit_from_sbom_licenses.extract_packages(doc)
        assert pkgs[0]["origin"] == "os"


# ---------------------------------------------------------------------------
# convert() — JUnit suite generation
# ---------------------------------------------------------------------------

class TestConvert:
    """convert(paths) builds the correct JUnit testsuite."""

    def test_empty_paths_all_missing_emits_skip(self, tmp_path: Path) -> None:
        """All paths missing → graceful L4-not-run skip."""
        suite = junit_from_sbom_licenses.convert([tmp_path / "missing.spdx.json"])
        assert suite.get("name") == "l1-sbom-licenses"
        assert _attr_int(suite, "failures") == 0
        assert _attr_int(suite, "skipped") == 1
        tc = suite.find("testcase")
        assert tc is not None
        assert tc.get("name") == "sbom-licenses-l4-not-run"

    def test_zero_paths_emits_no_packages(self) -> None:
        """No paths provided → single placeholder testcase, no failure."""
        suite = junit_from_sbom_licenses.convert([])
        assert _attr_int(suite, "failures") == 0

    def test_gpl_package_in_spdx_becomes_failure(self, tmp_path: Path) -> None:
        sbom = tmp_path / "sbom.spdx.json"
        _write_sbom(sbom, _spdx_doc([
            _spdx_pkg("clean-lib", "1.0", "MIT"),
            _spdx_pkg("gpl-thing", "2.0", "GPL-3.0-only"),
        ]))
        suite = junit_from_sbom_licenses.convert([sbom])
        assert _attr_int(suite, "failures") == 1
        fail_tc = next(
            (c for c in suite.iter("testcase") if "gpl-thing" in (c.get("name") or "")),
            None,
        )
        assert fail_tc is not None
        assert fail_tc.find("failure") is not None

    def test_all_permissive_yields_no_failures(self, tmp_path: Path) -> None:
        sbom = tmp_path / "sbom.spdx.json"
        _write_sbom(sbom, _spdx_doc([
            _spdx_pkg("fastapi", "0.100.0", "MIT"),
            _spdx_pkg("sqlalchemy", "2.0.0", "MIT"),
            _spdx_pkg("uvicorn", "0.29.0", "BSD-2-Clause"),
        ]))
        suite = junit_from_sbom_licenses.convert([sbom])
        assert _attr_int(suite, "failures") == 0
        assert _attr_int(suite, "tests") == 3

    def test_unknown_license_becomes_skipped_not_failure(self, tmp_path: Path) -> None:
        sbom = tmp_path / "sbom.spdx.json"
        _write_sbom(sbom, _spdx_doc([
            _spdx_pkg("mystery", "0.1", "NOASSERTION"),
        ]))
        suite = junit_from_sbom_licenses.convert([sbom])
        assert _attr_int(suite, "failures") == 0
        assert _attr_int(suite, "skipped") == 1

    def test_empty_sbom_yields_no_failure(self, tmp_path: Path) -> None:
        """An SBOM with zero packages → no testcases, no failure."""
        sbom = tmp_path / "empty.spdx.json"
        _write_sbom(sbom, _spdx_doc([]))
        suite = junit_from_sbom_licenses.convert([sbom])
        assert _attr_int(suite, "failures") == 0

    def test_deduplication_across_spdx_and_cdx(self, tmp_path: Path) -> None:
        """Same package in both SPDX and CycloneDX SBOMs → counted once."""
        spdx = tmp_path / "sbom.spdx.json"
        cdx = tmp_path / "sbom.cdx.json"
        pkg = _spdx_pkg("shared-lib", "1.0", "MIT")
        _write_sbom(spdx, _spdx_doc([pkg]))
        _write_sbom(cdx, _cdx_doc([
            _cdx_comp("shared-lib", "1.0", [_cdx_license("MIT")]),
        ]))
        suite = junit_from_sbom_licenses.convert([spdx, cdx])
        # shared-lib should appear exactly once
        names = [tc.get("name", "") for tc in suite.iter("testcase")]
        assert names.count("shared-lib-1.0") == 1

    def test_lgpl_os_package_fails(self, tmp_path: Path) -> None:
        sbom = tmp_path / "sbom.cdx.json"
        _write_sbom(sbom, _cdx_doc([
            _cdx_comp(
                "libssl", "3.0.0", [_cdx_license("LGPL-2.0-only")],
                purl="pkg:apk/libssl@3.0.0",
            ),
        ]))
        suite = junit_from_sbom_licenses.convert([sbom])
        assert _attr_int(suite, "failures") == 1
        tc = suite.find("testcase")
        assert tc is not None
        fail = tc.find("failure")
        assert fail is not None
        assert "os" in (fail.get("message") or "")


# ---------------------------------------------------------------------------
# CLI (main)
# ---------------------------------------------------------------------------

class TestMain:
    """main() CLI entrypoint."""

    def test_main_writes_xml_on_clean_sbom(self, tmp_path: Path) -> None:
        sbom = tmp_path / "sbom.spdx.json"
        out = tmp_path / "L1-sbom-licenses.xml"
        _write_sbom(sbom, _spdx_doc([_spdx_pkg("fastapi", "0.100.0", "MIT")]))
        rc = junit_from_sbom_licenses.main([
            "--sbom", str(sbom),
            "--output", str(out),
        ])
        assert rc == 0
        root = _parse_xml(out)
        suite = root.find("testsuite")
        assert suite is not None
        assert suite.get("name") == "l1-sbom-licenses"

    def test_main_returns_1_on_blocked_license(self, tmp_path: Path) -> None:
        sbom = tmp_path / "sbom.spdx.json"
        out = tmp_path / "L1-sbom-licenses.xml"
        _write_sbom(sbom, _spdx_doc([_spdx_pkg("gpl-lib", "1.0", "GPL-3.0-only")]))
        rc = junit_from_sbom_licenses.main([
            "--sbom", str(sbom),
            "--output", str(out),
        ])
        assert rc == 1

    def test_main_missing_sbom_returns_0_skipped(self, tmp_path: Path) -> None:
        """Missing SBOM → skip (rc=0), not error."""
        out = tmp_path / "L1-sbom-licenses.xml"
        rc = junit_from_sbom_licenses.main([
            "--sbom", str(tmp_path / "nonexistent.spdx.json"),
            "--output", str(out),
        ])
        assert rc == 0
        root = _parse_xml(out)
        suite = root.find("testsuite")
        assert suite is not None
        assert _attr_int(suite, "skipped") == 1
        assert _attr_int(suite, "failures") == 0

    def test_suite_name_is_l1_sbom_licenses(self, tmp_path: Path) -> None:
        """Invariant: testsuite name matches layer attribution."""
        out = tmp_path / "L1-sbom-licenses.xml"
        rc = junit_from_sbom_licenses.main([
            "--sbom", str(tmp_path / "nonexistent.json"),
            "--output", str(out),
        ])
        assert rc == 0
        root = _parse_xml(out)
        suite = root.find("testsuite")
        assert suite is not None
        assert suite.get("name") == "l1-sbom-licenses"
