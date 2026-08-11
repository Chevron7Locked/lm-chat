#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Unit tests for P15i dashboard deliverables.

Covers:
- report.json schema validation against gate-report.schema.json
- HTML structural assertions (one <section> per layer, finding count matches)
- Empty-gates dir handling (everything skipped)
- Partial-gates handling (some layers present, others missing)
- Sub-tool XML discovery (P15i SUB_TOOL_XMLS extension)
- render_dashboard.py render() function correctness
- Layer cards present for L0..L8 + sub-layers
- Finding count in summary matches sum-of-layers
- OWASP block rendered when coverage data is present
- DEGRADED status when stubs are present
- PRODUCTION-READY suppressed when critical findings exist
"""
from __future__ import annotations

import importlib.util as _ilu
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_DIR = REPO_ROOT / "tools"
SCHEMA_PATH = REPO_ROOT / "tests" / "security" / "audit-schemas" / "gate-report.schema.json"


def _load_module(name: str, path: Path) -> Any:
    spec = _ilu.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = _ilu.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


aggregate_junit = _load_module("aggregate_junit", TOOLS_DIR / "aggregate_junit.py")
render_dashboard = _load_module("render_dashboard", TOOLS_DIR / "render_dashboard.py")

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@pytest.fixture
def gates_dir(tmp_path: Path) -> Path:
    d = tmp_path / "gates"
    d.mkdir()
    return d


def _write_xml(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _green_layer(layer: int, slug: str, duration: float = 0.5) -> str:
    return f"""<?xml version='1.0' encoding='utf-8'?>
<testsuites name="l{layer}-{slug}" tests="3" failures="0" errors="0">
  <testsuite name="l{layer}-{slug}" tests="3" failures="0" errors="0" skipped="0" time="{duration}">
    <testcase name="a" classname="t"/>
    <testcase name="b" classname="t"/>
    <testcase name="c" classname="t"/>
  </testsuite>
</testsuites>"""


def _red_layer(layer: int, slug: str) -> str:
    return f"""<?xml version='1.0' encoding='utf-8'?>
<testsuites name="l{layer}-{slug}" tests="2" failures="1" errors="0">
  <testsuite name="l{layer}-{slug}" tests="2" failures="1" errors="0" skipped="0" time="0.1">
    <testcase name="a" classname="t"/>
    <testcase name="b" classname="t">
      <failure message="boom" type="assert">expected x got y</failure>
    </testcase>
  </testsuite>
</testsuites>"""


def _stub_layer(layer: int, slug: str) -> str:
    return f"""<?xml version='1.0' encoding='utf-8'?>
<testsuites name="l{layer}-{slug}" tests="1" failures="0" errors="0" skipped="1">
  <testsuite name="l{layer}-{slug}" tests="1" failures="0" errors="0" skipped="1">
    <testcase name="not-yet-implemented" classname="stub">
      <skipped message="P15a stub"/>
    </testcase>
  </testsuite>
</testsuites>"""


def _sub_tool_xml(tool: str, tests: int = 2, failures: int = 0) -> str:
    return f"""<?xml version='1.0' encoding='utf-8'?>
<testsuites name="{tool}" tests="{tests}" failures="{failures}" errors="0">
  <testsuite name="{tool}" tests="{tests}" failures="{failures}" errors="0" skipped="0" time="0.2">
    <testcase name="check-1" classname="{tool}"/>
    <testcase name="check-2" classname="{tool}">
      {"<failure message='vuln found'/>" if failures else ""}
    </testcase>
  </testsuite>
</testsuites>"""


_SLUGS: dict[int, str] = {layer: slug for layer, slug, _ in aggregate_junit.LAYERS}


def _populate_all_green(gates_dir: Path) -> None:
    for layer in range(0, 9):
        slug = _SLUGS[layer]
        _write_xml(gates_dir / f"L{layer}-{slug}.xml", _green_layer(layer, slug))


# ---------------------------------------------------------------------------
# 1. Schema validation — all-green report validates
# ---------------------------------------------------------------------------

def test_schema_valid_all_green(gates_dir: Path, schema: dict[str, Any]) -> None:
    _populate_all_green(gates_dir)
    report, _ = aggregate_junit.aggregate(gates_dir)
    Draft202012Validator(schema).validate(report)


# ---------------------------------------------------------------------------
# 2. Schema validation — partial gates (some layers missing)
# ---------------------------------------------------------------------------

def test_schema_valid_partial_gates(gates_dir: Path, schema: dict[str, Any]) -> None:
    # Only L0 and L7 present; others missing.
    _write_xml(gates_dir / "L0-static-fast.xml", _green_layer(0, "static-fast"))
    _write_xml(gates_dir / "L7-llm-security.xml", _green_layer(7, "llm-security"))
    report, _ = aggregate_junit.aggregate(gates_dir)
    Draft202012Validator(schema).validate(report)
    missing = [lr for lr in report["layer_results"] if lr.get("missing")]
    assert len(missing) >= 5  # L1-L6, L8 missing


# ---------------------------------------------------------------------------
# 3. Schema validation — empty gates dir (everything missing)
# ---------------------------------------------------------------------------

def test_schema_valid_empty_gates_dir(gates_dir: Path, schema: dict[str, Any]) -> None:
    report, exit_code = aggregate_junit.aggregate(gates_dir)
    Draft202012Validator(schema).validate(report)
    assert exit_code != 0
    assert report["ok"] is False
    missing_layers = [lr["layer"] for lr in report["layer_results"] if lr.get("missing")]
    # All required layers (0-8) should be missing
    for req in aggregate_junit.REQUIRED_LAYERS:
        assert req in missing_layers


# ---------------------------------------------------------------------------
# 4. HTML has one <section> per layer result
# ---------------------------------------------------------------------------

def test_html_has_section_per_layer(gates_dir: Path) -> None:
    _populate_all_green(gates_dir)
    report, _ = aggregate_junit.aggregate(gates_dir)
    html = render_dashboard.render(report, gates_dir)
    # One <section class="layer-section"> per layer_result entry
    section_count = html.count('class="layer-section"')
    assert section_count == len(report["layer_results"])


# ---------------------------------------------------------------------------
# 5. HTML finding count in summary matches sum-of-layers
# ---------------------------------------------------------------------------

def test_html_finding_count_matches_report(gates_dir: Path) -> None:
    _populate_all_green(gates_dir)
    _write_xml(gates_dir / "L4-container-supplychain.xml", _red_layer(4, "container-supplychain"))
    report, _ = aggregate_junit.aggregate(gates_dir)
    html = render_dashboard.render(report, gates_dir)
    expected = len(report["findings"])
    assert f"Findings ({expected})" in html


# ---------------------------------------------------------------------------
# 6. HTML shows PRODUCTION-READY when all layers green
# ---------------------------------------------------------------------------

def test_html_production_ready_badge(gates_dir: Path) -> None:
    _populate_all_green(gates_dir)
    report, _ = aggregate_junit.aggregate(gates_dir)
    html = render_dashboard.render(report, gates_dir)
    assert "PRODUCTION-READY" in html
    assert 'class="status-badge green"' in html


# ---------------------------------------------------------------------------
# 7. HTML shows FAIL when a required layer is red
# ---------------------------------------------------------------------------

def test_html_fail_badge_when_layer_red(gates_dir: Path) -> None:
    _populate_all_green(gates_dir)
    _write_xml(gates_dir / "L3-security-static.xml", _red_layer(3, "security-static"))
    report, _ = aggregate_junit.aggregate(gates_dir)
    html = render_dashboard.render(report, gates_dir)
    assert "FAIL" in html
    assert 'class="status-badge red"' in html


# ---------------------------------------------------------------------------
# 8. HTML shows DEGRADED when stubs present
# ---------------------------------------------------------------------------

def test_html_degraded_with_stub_layer(gates_dir: Path) -> None:
    _populate_all_green(gates_dir)
    _write_xml(gates_dir / "L2-unit-integration.xml", _stub_layer(2, "unit-integration"))
    report, _ = aggregate_junit.aggregate(gates_dir)
    html = render_dashboard.render(report, gates_dir)
    assert "DEGRADED" in html
    assert report["production_ready"] is False
    assert report["ok"] is True  # stubs don't fail ok


# ---------------------------------------------------------------------------
# 9. Empty gates dir — HTML renders cleanly (no exception)
# ---------------------------------------------------------------------------

def test_html_renders_empty_gates_cleanly(gates_dir: Path) -> None:
    report, _ = aggregate_junit.aggregate(gates_dir)
    html = render_dashboard.render(report, gates_dir)
    assert "<!DOCTYPE html>" in html
    assert "FAIL" in html  # all layers missing = red


# ---------------------------------------------------------------------------
# 10. Sub-tool XML discovery — present sub-tool appears in layer tools dict
# ---------------------------------------------------------------------------

def test_sub_tool_xml_discovery_present(gates_dir: Path, schema: dict[str, Any]) -> None:
    _populate_all_green(gates_dir)
    # Write a real L4-trivy.xml sub-tool XML
    _write_xml(gates_dir / "L4-trivy.xml", _sub_tool_xml("l4-trivy", tests=5, failures=0))
    report, _ = aggregate_junit.aggregate(gates_dir)
    Draft202012Validator(schema).validate(report)
    l4 = next(lr for lr in report["layer_results"] if lr["layer"] == 4)
    # L4-trivy should appear as a tool entry
    tool_keys = set(l4["tools"].keys())
    assert "L4-trivy" in tool_keys


# ---------------------------------------------------------------------------
# 11. Sub-tool XML discovery — missing sub-tool becomes skipped entry
# ---------------------------------------------------------------------------

def test_sub_tool_xml_missing_becomes_skipped(gates_dir: Path) -> None:
    _populate_all_green(gates_dir)
    # L4-trivy.xml not written — should appear as skipped
    report, _ = aggregate_junit.aggregate(gates_dir)
    l4 = next(lr for lr in report["layer_results"] if lr["layer"] == 4)
    tool_keys = set(l4["tools"].keys())
    assert "L4-trivy" in tool_keys
    assert l4["tools"]["L4-trivy"]["skipped"] >= 1
    # Missing sub-tool does NOT flip layer red on its own
    assert l4["ok"] is True


# ---------------------------------------------------------------------------
# 12. OWASP block rendered when owasp_llm_coverage is non-empty
# ---------------------------------------------------------------------------

def test_html_owasp_block_when_coverage_present(gates_dir: Path) -> None:
    _populate_all_green(gates_dir)
    report, _ = aggregate_junit.aggregate(gates_dir)
    # Inject synthetic OWASP coverage
    report["owasp_llm_coverage"] = [
        {"id": "LLM01", "status": "matches", "risk": "Prompt injection", "test": "garak"},
        {
            "id": "LLM03",
            "status": "missing-on-purpose",
            "risk": "Training data poisoning",
            "test": "",
        },
    ]
    html = render_dashboard.render(report, gates_dir)
    assert "LLM01" in html
    assert "LLM03" in html
    assert "missing-on-purpose" in html


# ---------------------------------------------------------------------------
# 13. OWASP block shows placeholder when coverage is empty
# ---------------------------------------------------------------------------

def test_html_owasp_placeholder_when_empty(gates_dir: Path) -> None:
    _populate_all_green(gates_dir)
    report, _ = aggregate_junit.aggregate(gates_dir)
    assert report["owasp_llm_coverage"] == []
    html = render_dashboard.render(report, gates_dir)
    assert "OWASP LLM Top 10 coverage" in html
    assert "not yet populated" in html


# ---------------------------------------------------------------------------
# 14. Layer names L0..L9 all appear in HTML
# ---------------------------------------------------------------------------

def test_html_contains_all_layer_names(gates_dir: Path) -> None:
    _populate_all_green(gates_dir)
    report, _ = aggregate_junit.aggregate(gates_dir)
    html = render_dashboard.render(report, gates_dir)
    for _, _, display_name in aggregate_junit.LAYERS:
        assert display_name in html, f"Expected layer name '{display_name}' in HTML"


# ---------------------------------------------------------------------------
# 15. main() writes both report.json and index.html; both are non-empty
# ---------------------------------------------------------------------------

def test_main_writes_report_and_dashboard(gates_dir: Path, schema: dict[str, Any]) -> None:
    _populate_all_green(gates_dir)
    rc = aggregate_junit.main(["--gates-dir", str(gates_dir)])
    assert rc == 0
    report_path = gates_dir / "report.json"
    html_path = gates_dir / "index.html"
    assert report_path.is_file() and report_path.stat().st_size > 0
    assert html_path.is_file() and html_path.stat().st_size > 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(report)
    html = html_path.read_text(encoding="utf-8")
    assert "<!DOCTYPE html>" in html
    assert "lm-chat production-gate" in html
