#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Check license declarations in syft SBOMs and emit JUnit XML.

Parses both SPDX-JSON and CycloneDX-JSON SBOMs (produced by ``anchore/syft``
in the L4 container-supplychain layer) and classifies every package's declared
license against the same copyleft blocklist used by ``junit_from_pip_licenses.py``.

Purpose: pip-licenses only sees Python dependencies installed in the virtualenv.
The deploy image (Chainguard-based) includes OS-level packages (glibc, musl,
busybox, etc.) that pip-licenses never inspects. The SBOM covers the full
software bill of materials — Python + OS + any other component syft finds.

Gate policy (same as L3 pip-licenses):

  Blocked license families (would re-encumber Apache-2.0 distribution):
    GPL-2.0, GPL-3.0, LGPL-*, AGPL-*, SSPL-*, EUPL-*, OSL-*

  Permissive / compatible: MIT, BSD-*, Apache-2.0, ISC, PSF, Unlicense,
  CC0, MPL-2.0, WTFPL, Zlib, Public Domain → PASS.

  Unknown / ambiguous → ``<skipped>`` (informational; don't gate-block).

One ``<testcase>`` per package-in-SBOM. Empty SBOM → 0 testcases (not a
failure; the structural SBOM validation lives in ``junit_from_sbom.py``).

Graceful skip when no SBOM files are found (L4 not yet run): emits a
synthetic ``sbom-licenses-l4-not-run`` skipped testcase.

CLI::

    uv run python tools/junit_from_sbom_licenses.py \\
        --sbom target/gates/sbom.spdx.json \\
        --sbom target/gates/sbom.cyclonedx.json \\
        --output target/gates/L1-sbom-licenses.xml

    # Skip mode (L4 not yet run):
    uv run python tools/junit_from_sbom_licenses.py \\
        --sbom target/gates/sbom.spdx.json \\
        --output target/gates/L1-sbom-licenses.xml
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

_SUITE_NAME = "l1-sbom-licenses"

# ---- license classification (mirrors junit_from_pip_licenses.py) -----------

_BLOCKED_FRAGMENTS: list[str] = [
    "GPL-2",
    "GPL-3",
    "LGPL-2",
    "LGPL-3",
    "AGPL",
    "SSPL",
    "Commons-Clause",
    "OSL-3",
    "EUPL",
]

_ALLOWED_FRAGMENTS: list[str] = [
    "MIT",
    "BSD",
    "Apache",
    "ISC",
    "PSF",
    "Python",
    "Unlicense",
    "CC0",
    "WTFPL",
    "Zlib",
    "MPL-2",
    "Public Domain",
]

# Sentinel values that indicate no license info available.
_UNKNOWN_SENTINELS: frozenset[str] = frozenset(
    {"UNKNOWN", "", "N/A", "NONE", "NOT FOUND", "NOASSERTION"}
)


def _is_blocked(license_text: str) -> bool:
    upper = license_text.upper()
    for frag in _ALLOWED_FRAGMENTS:
        if frag.upper() in upper:
            return False
    for frag in _BLOCKED_FRAGMENTS:
        if frag.upper() in upper:
            return True
    return False


def _is_unknown(license_text: str) -> bool:
    return license_text.strip().upper() in _UNKNOWN_SENTINELS


def _normalise_license(raw: Any) -> str:
    """Flatten a license field to a single string for classification.

    SPDX ``licenseConcluded`` / ``licenseDeclared`` may be:
      - a plain SPDX expression string: ``"MIT"``
      - ``"NOASSERTION"`` / ``"NONE"``

    CycloneDX ``licenses`` field may be:
      - a list of objects: ``[{"license": {"id": "MIT"}}, ...]``
      - a list of objects: ``[{"expression": "MIT OR Apache-2.0"}, ...]``
    """
    if raw is None:
        return "UNKNOWN"
    if isinstance(raw, str):
        return raw.strip() or "UNKNOWN"
    if isinstance(raw, list):
        parts: list[str] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            if "expression" in item and isinstance(item["expression"], str):
                parts.append(item["expression"].strip())
            elif "license" in item and isinstance(item["license"], dict):
                lic_obj = item["license"]
                # prefer "id" (SPDX), fall back to "name"
                val = lic_obj.get("id") or lic_obj.get("name") or ""
                if isinstance(val, str) and val.strip():
                    parts.append(val.strip())
        return " AND ".join(parts) if parts else "UNKNOWN"
    return "UNKNOWN"


# ---- origin detection -------------------------------------------------------

def _detect_format(doc: dict[str, Any]) -> str:
    if "spdxVersion" in doc or "SPDXID" in doc:
        return "spdx"
    if str(doc.get("bomFormat", "")).lower() == "cyclonedx":
        return "cyclonedx"
    if "components" in doc and "specVersion" in doc:
        return "cyclonedx"
    return "unknown"


def _pkg_origin(pkg: dict[str, Any], fmt: str) -> str:
    """Best-effort: classify package as 'python' or 'os' or 'other'."""
    if fmt == "spdx":
        # syft annotates the package type via externalRefs or spdxId prefix
        for ref in pkg.get("externalRefs", []):
            cat = str(ref.get("referenceCategory", "")).lower()
            ref_type = str(ref.get("referenceType", "")).lower()
            if "python" in ref_type or "pypi" in ref_type:
                return "python"
            if cat in {"package-manager"} and "pypi" in str(
                ref.get("referenceLocator", "")
            ).lower():
                return "python"
        name = str(pkg.get("name", "")).lower()
        # OS packages often have names like libssl, libc, musl, busybox
        if any(
            name.startswith(pfx)
            for pfx in ("lib", "musl", "busybox", "glibc", "ncurses")
        ):
            return "os"
        return "other"
    if fmt == "cyclonedx":
        pkg_type = str(pkg.get("type", "")).lower()
        if pkg_type == "library":
            # CycloneDX purl gives the ecosystem
            purl = str(pkg.get("purl", "")).lower()
            if purl.startswith("pkg:pypi"):
                return "python"
            if any(
                purl.startswith(f"pkg:{t}")
                for t in ("deb", "rpm", "apk", "alpine")
            ):
                return "os"
        return "other"
    return "other"


# ---- SPDX extraction --------------------------------------------------------

def _extract_spdx(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a list of package dicts with keys: name, version, license, origin."""
    packages = doc.get("packages", [])
    if not isinstance(packages, list):
        return []
    result: list[dict[str, Any]] = []
    for pkg in packages:
        if not isinstance(pkg, dict):
            continue
        name = str(pkg.get("name") or "unknown")
        version = str(pkg.get("versionInfo") or "unknown")
        # SPDX prefers licenseConcluded; fall back to licenseDeclared
        lic_raw = pkg.get("licenseConcluded") or pkg.get("licenseDeclared") or ""
        lic = _normalise_license(lic_raw)
        origin = _pkg_origin(pkg, "spdx")
        result.append({"name": name, "version": version, "license": lic, "origin": origin})
    return result


# ---- CycloneDX extraction ---------------------------------------------------

def _extract_cyclonedx(doc: dict[str, Any]) -> list[dict[str, Any]]:
    """Return a list of package dicts with keys: name, version, license, origin."""
    components = doc.get("components", [])
    if not isinstance(components, list):
        return []
    result: list[dict[str, Any]] = []
    for comp in components:
        if not isinstance(comp, dict):
            continue
        name = str(comp.get("name") or "unknown")
        version = str(comp.get("version") or "unknown")
        lic = _normalise_license(comp.get("licenses"))
        origin = _pkg_origin(comp, "cyclonedx")
        result.append({"name": name, "version": version, "license": lic, "origin": origin})
    return result


# ---- public API -------------------------------------------------------------

def extract_packages(doc: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    """Return (format_name, packages) from a parsed SBOM document.

    Each package dict has keys: name, version, license, origin.
    Returns ("unknown", []) for unrecognised documents.
    """
    fmt = _detect_format(doc)
    if fmt == "spdx":
        return fmt, _extract_spdx(doc)
    if fmt == "cyclonedx":
        return fmt, _extract_cyclonedx(doc)
    return "unknown", []


def convert(paths: list[Path]) -> ET.Element:
    """Build the JUnit ``<testsuite>`` from a list of SBOM file paths.

    Gracefully skips files that don't exist (L4 not yet run).
    Empty / unreadable SBOMs produce 0 testcases (not failures — the
    structural check lives in ``junit_from_sbom.py``).
    """
    all_packages: list[dict[str, Any]] = []
    missing_count = 0

    for path in paths:
        if not path.is_file():
            missing_count += 1
            continue
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            missing_count += 1
            continue
        if not raw.strip():
            continue
        try:
            doc = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(doc, dict):
            continue
        _fmt, pkgs = extract_packages(doc)
        # Deduplicate by (name, version) across multiple SBOM formats.
        seen_keys = {(p["name"], p["version"]) for p in all_packages}
        for pkg in pkgs:
            key = (pkg["name"], pkg["version"])
            if key not in seen_keys:
                all_packages.append(pkg)
                seen_keys.add(key)

    # All paths missing → graceful skip (L4 not yet run)
    if missing_count == len(paths) and paths:
        return _emit_l4_not_run()

    fail_count = sum(
        1 for p in all_packages if not _is_unknown(p["license"]) and _is_blocked(p["license"])
    )
    skip_count = sum(1 for p in all_packages if _is_unknown(p["license"]))
    test_count = max(len(all_packages), 1)

    suite = ET.Element("testsuite", {
        "name": _SUITE_NAME,
        "tests": str(test_count),
        "failures": str(fail_count),
        "errors": "0",
        "skipped": str(skip_count),
    })

    if not all_packages:
        ET.SubElement(suite, "testcase", {
            "name": "no-packages-found",
            "classname": "sbom-licenses",
        })
        return suite

    for pkg in sorted(all_packages, key=lambda p: p["name"].lower()):
        name = pkg["name"]
        version = pkg["version"]
        lic = pkg["license"]
        origin = pkg["origin"]

        tc = ET.SubElement(suite, "testcase", {
            "name": f"{name}-{version}",
            "classname": f"sbom-licenses.{origin}",
        })

        if _is_unknown(lic):
            sk = ET.SubElement(tc, "skipped", {
                "message": (
                    f"unknown license: {name}=={version} origin={origin}"
                    " (investigate manually)"
                ),
            })
            sk.text = f"package={name} version={version} license={lic} origin={origin}"
        elif _is_blocked(lic):
            fail = ET.SubElement(tc, "failure", {
                "message": f"blocked license: {lic} on {name}=={version} [{origin}]",
                "type": "license-incompatible",
            })
            fail.text = (
                f"package={name} version={version} license={lic} origin={origin}\n"
                f"Reason: {lic} is copyleft and would re-encumber lm-chat's Apache-2.0 "
                f"distribution. Replace or vendor this dependency."
            )
        else:
            so = ET.SubElement(tc, "system-out")
            so.text = f"OK: {name}=={version} license={lic} origin={origin}"

    return suite


def _emit_l4_not_run() -> ET.Element:
    """Synthetic skipped suite when no SBOM files are present."""
    suite = ET.Element("testsuite", {
        "name": _SUITE_NAME,
        "tests": "1",
        "failures": "0",
        "errors": "0",
        "skipped": "1",
    })
    tc = ET.SubElement(suite, "testcase", {
        "name": "sbom-licenses-l4-not-run",
        "classname": "sbom-licenses",
    })
    sk = ET.SubElement(tc, "skipped", {
        "message": (
            "SBOM files not found — L4 container-supplychain layer has not run yet. "
            "Run `make target/gates/.L4-passed` first to generate SBOMs."
        ),
    })
    sk.text = (
        "Expected: target/gates/sbom.spdx.json and/or target/gates/sbom.cyclonedx.json\n"
        "These are produced by anchore/syft in the L4 layer. "
        "L1 SBOM license check is SKIPPED (not failed) until L4 has run."
    )
    return suite


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--sbom",
        action="append",
        default=[],
        type=Path,
        help="Path to an SBOM JSON file (repeatable; accepts SPDX or CycloneDX).",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path to write JUnit XML (default: stdout).",
    )
    args = p.parse_args(argv)

    suite = convert(list(args.sbom))

    root = ET.Element("testsuites", {
        "name": _SUITE_NAME,
        "tests": suite.get("tests", "0"),
        "failures": suite.get("failures", "0"),
        "errors": "0",
    })
    root.append(suite)

    tree = ET.ElementTree(root)
    ET.indent(tree, space="  ")

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        tree.write(args.output, encoding="utf-8", xml_declaration=True)
    else:
        sys.stdout.write(ET.tostring(root, encoding="unicode", xml_declaration=True))
        sys.stdout.write("\n")

    failures = int(suite.get("failures", "0") or "0")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
