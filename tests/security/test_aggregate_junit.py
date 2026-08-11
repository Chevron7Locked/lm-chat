# SPDX-License-Identifier: Apache-2.0
"""Unit tests for tools/aggregate_junit.py (P15a L9 aggregator).

Covered:
- All layers green -> production-ready
- One layer red (failure) -> FAIL with that layer flagged
- One layer with errors -> FAIL with critical finding
- Missing layer XML -> FAIL with "layer missing" finding
- Skipped testcase -> counted as skipped, doesn't fail the layer
- Stub layer (skipped name='not-yet-implemented') -> recognised; suppresses
  production-ready badge but doesn't flip ok=false
- Malformed XML -> FAIL with parse-error finding
- Schema conformance: every emitted report.json validates
"""
from __future__ import annotations

import importlib.util as _ilu
import json
import sys
from collections.abc import Iterable
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"
SCHEMA_PATH = (
    REPO_ROOT / "tests" / "security" / "audit-schemas" / "gate-report.schema.json"
)

# Import the aggregator dynamically (tools/ isn't on the default sys.path
# and we don't want to add it just for the test). importlib keeps pyright
# happy without a package skeleton under tools/.
_AGG_SPEC = _ilu.spec_from_file_location(
    "aggregate_junit", TOOLS_DIR / "aggregate_junit.py"
)
assert _AGG_SPEC is not None and _AGG_SPEC.loader is not None
aggregate_junit = _ilu.module_from_spec(_AGG_SPEC)
sys.modules["aggregate_junit"] = aggregate_junit
_AGG_SPEC.loader.exec_module(aggregate_junit)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_xml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _green_suite(layer: int, slug: str) -> str:
    return f"""<?xml version='1.0' encoding='utf-8'?>
<testsuites name="l{layer}-{slug}" tests="3" failures="0" errors="0">
  <testsuite name="l{layer}-{slug}" tests="3" failures="0" errors="0" skipped="0" time="0.42">
    <testcase name="case-a" classname="t"/>
    <testcase name="case-b" classname="t"/>
    <testcase name="case-c" classname="t"/>
  </testsuite>
</testsuites>
"""


def _red_suite(layer: int, slug: str) -> str:
    return f"""<?xml version='1.0' encoding='utf-8'?>
<testsuites name="l{layer}-{slug}" tests="2" failures="1" errors="0">
  <testsuite name="l{layer}-{slug}" tests="2" failures="1" errors="0" skipped="0" time="0.1">
    <testcase name="case-a" classname="t"/>
    <testcase name="case-b" classname="t">
      <failure message="boom" type="assert">expected x, got y</failure>
    </testcase>
  </testsuite>
</testsuites>
"""


def _error_suite(layer: int, slug: str) -> str:
    return f"""<?xml version='1.0' encoding='utf-8'?>
<testsuites name="l{layer}-{slug}" tests="1" failures="0" errors="1">
  <testsuite name="l{layer}-{slug}" tests="1" failures="0" errors="1" skipped="0">
    <testcase name="crashy" classname="t">
      <error message="exploded" type="RuntimeError">traceback...</error>
    </testcase>
  </testsuite>
</testsuites>
"""


def _skipped_suite(layer: int, slug: str) -> str:
    """A suite with a skipped case that should NOT fail the layer."""
    return f"""<?xml version='1.0' encoding='utf-8'?>
<testsuites name="l{layer}-{slug}" tests="2" failures="0" errors="0" skipped="1">
  <testsuite name="l{layer}-{slug}" tests="2" failures="0" errors="0" skipped="1">
    <testcase name="passes" classname="t"/>
    <testcase name="hadolint" classname="t">
      <skipped message="binary not on PATH"/>
    </testcase>
  </testsuite>
</testsuites>
"""


def _stub_suite(layer: int, slug: str) -> str:
    return f"""<?xml version='1.0' encoding='utf-8'?>
<testsuites name="l{layer}-{slug}" tests="1" failures="0" errors="0" skipped="1">
  <testsuite name="l{layer}-{slug}" tests="1" failures="0" errors="0" skipped="1">
    <testcase name="not-yet-implemented" classname="stub">
      <skipped message="P15a stub"/>
    </testcase>
  </testsuite>
</testsuites>
"""


# Layer-slug mapping must mirror aggregate_junit.LAYERS.
_SLUGS = {layer: slug for layer, slug, _ in aggregate_junit.LAYERS}


def _populate(
    gates_dir: Path,
    layers: Iterable[int],
    *,
    builder=_green_suite,
) -> None:
    for layer in layers:
        slug = _SLUGS[layer]
        _write_xml(gates_dir / f"L{layer}-{slug}.xml", builder(layer, slug))


def _populate_all_green(gates_dir: Path) -> None:
    _populate(gates_dir, range(0, 9))


@pytest.fixture
def gates_dir(tmp_path: Path) -> Path:
    d = tmp_path / "gates"
    d.mkdir()
    return d


@pytest.fixture
def schema() -> dict:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_all_green_yields_production_ready(gates_dir: Path, schema: dict) -> None:
    _populate_all_green(gates_dir)
    report, exit_code = aggregate_junit.aggregate(gates_dir)
    assert exit_code == 0
    assert report["ok"] is True
    assert report["production_ready"] is True
    assert report["findings"] == []
    Draft202012Validator(schema).validate(report)


def test_one_layer_red_failure_flags_fail(gates_dir: Path, schema: dict) -> None:
    _populate_all_green(gates_dir)
    # Overwrite L4 with a red suite.
    _write_xml(
        gates_dir / f"L4-{_SLUGS[4]}.xml",
        _red_suite(4, _SLUGS[4]),
    )
    report, exit_code = aggregate_junit.aggregate(gates_dir)
    assert exit_code != 0
    assert report["ok"] is False
    assert report["production_ready"] is False
    # Locate the L4 layer in the result list.
    l4 = next(lr for lr in report["layer_results"] if lr["layer"] == 4)
    assert l4["ok"] is False
    # The failure surfaced as a 'high' finding.
    sevs = {f["severity"] for f in report["findings"]}
    assert "high" in sevs
    assert any(f.get("layer") == 4 for f in report["findings"])
    Draft202012Validator(schema).validate(report)


def test_one_layer_error_yields_critical_finding(gates_dir: Path, schema: dict) -> None:
    _populate_all_green(gates_dir)
    _write_xml(
        gates_dir / f"L7-{_SLUGS[7]}.xml",
        _error_suite(7, _SLUGS[7]),
    )
    report, exit_code = aggregate_junit.aggregate(gates_dir)
    assert exit_code != 0
    assert report["ok"] is False
    crit = [f for f in report["findings"] if f["severity"] == "critical"]
    assert crit, "expected at least one critical finding for the errored layer"
    Draft202012Validator(schema).validate(report)


def test_missing_layer_xml_is_finding_not_exception(
    gates_dir: Path, schema: dict
) -> None:
    # Only L0..L3 present; L4..L8 missing.
    _populate(gates_dir, range(0, 4))
    report, exit_code = aggregate_junit.aggregate(gates_dir)
    assert exit_code != 0
    missing_layers = [
        lr["layer"] for lr in report["layer_results"] if lr["missing"]
    ]
    assert set(missing_layers) == {4, 5, 6, 7, 8}
    titles = [f["title"] for f in report["findings"]]
    assert any("layer missing" in t for t in titles)
    Draft202012Validator(schema).validate(report)


def test_skipped_testcase_does_not_fail_layer(gates_dir: Path, schema: dict) -> None:
    _populate(gates_dir, [1, 2, 3, 4, 5, 6, 7, 8])
    _write_xml(
        gates_dir / f"L0-{_SLUGS[0]}.xml",
        _skipped_suite(0, _SLUGS[0]),
    )
    report, exit_code = aggregate_junit.aggregate(gates_dir)
    assert exit_code == 0
    l0 = next(lr for lr in report["layer_results"] if lr["layer"] == 0)
    assert l0["ok"] is True
    # The skipped count is preserved.
    tool_stats = next(iter(l0["tools"].values()))
    assert tool_stats["skipped"] >= 1
    Draft202012Validator(schema).validate(report)


def test_stub_layer_is_recognised(gates_dir: Path, schema: dict) -> None:
    _populate(gates_dir, range(0, 9))
    _write_xml(
        gates_dir / f"L1-{_SLUGS[1]}.xml",
        _stub_suite(1, _SLUGS[1]),
    )
    report, _ = aggregate_junit.aggregate(gates_dir)
    l1 = next(lr for lr in report["layer_results"] if lr["layer"] == 1)
    assert l1["stub"] is True
    assert l1["ok"] is True  # stubs don't fail
    # Production-ready is suppressed by the stub in a required layer.
    assert report["production_ready"] is False
    Draft202012Validator(schema).validate(report)


def test_malformed_xml_yields_critical_finding(
    gates_dir: Path, schema: dict
) -> None:
    _populate_all_green(gates_dir)
    # Corrupt L2.
    (gates_dir / f"L2-{_SLUGS[2]}.xml").write_text(
        "<not-xml<<<", encoding="utf-8"
    )
    report, exit_code = aggregate_junit.aggregate(gates_dir)
    assert exit_code != 0
    parse_findings = [
        f for f in report["findings"]
        if "parse error" in f["title"].lower() and f["severity"] == "critical"
    ]
    assert parse_findings
    Draft202012Validator(schema).validate(report)


def test_main_writes_report_and_html(gates_dir: Path, schema: dict) -> None:
    """End-to-end: aggregate_junit.main writes both report.json + index.html."""
    _populate_all_green(gates_dir)
    rc = aggregate_junit.main(["--gates-dir", str(gates_dir)])
    assert rc == 0
    report_path = gates_dir / "report.json"
    html_path = gates_dir / "index.html"
    assert report_path.is_file()
    assert html_path.is_file()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(report)
    html = html_path.read_text(encoding="utf-8")
    assert "PRODUCTION-READY" in html
    assert "Layer results" in html


def test_html_dashboard_renders_findings(gates_dir: Path) -> None:
    """HTML dashboard reflects red status when a layer fails."""
    _populate_all_green(gates_dir)
    _write_xml(
        gates_dir / f"L3-{_SLUGS[3]}.xml",
        _red_suite(3, _SLUGS[3]),
    )
    rc = aggregate_junit.main(["--gates-dir", str(gates_dir)])
    assert rc != 0
    html = (gates_dir / "index.html").read_text(encoding="utf-8")
    assert "FAIL" in html
    # The histogram should mention high severity.
    assert "high" in html.lower()
