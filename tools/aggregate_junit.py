#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""L9 aggregator — collapse JUnit XML across all layers into report.json + index.html.

Reads every ``target/gates/L*.xml`` (and named sub-tool report files) and emits:

- ``target/gates/report.json`` (validates against
  ``tests/security/audit-schemas/gate-report.schema.json``)
- ``target/gates/index.html`` (operator dashboard, via tools/render_dashboard.py)

Exit code 0 iff every required layer is green and present. Missing layer
XML files are findings, NOT Python exceptions.
Missing *sub-tool* XMLs are marked skipped in the layer card but do NOT
flip the layer red on their own (the combined layer XML is the authority).

Discovers ALL named sub-tool XMLs across L0..L8 and surfaces them
in the per-layer tool drill-down.  render_dashboard.py is the sole
HTML renderer; this module delegates to it.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import importlib.util
import json
import os
import subprocess
import sys
import time
from dataclasses import dataclass, field
from html import escape as h
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

# Layer (number, slug, display name). Order matters — that's the L0→L9 order.
LAYERS: list[tuple[int, str, str]] = [
    (0, "static-fast",           "Static fast"),
    (1, "license-bill",          "License"),
    (2, "unit-integration",      "Unit + integration"),
    (3, "security-static",       "Security static"),
    (4, "container-supplychain", "Container + supply chain"),
    (5, "dast-offline",          "DAST offline"),
    (6, "dast-online",           "DAST online"),
    (7, "llm-security",          "LLM security"),
    (8, "auth-session",          "Auth + session + crypto"),
    (9, "gate-report",           "Gate report"),
]

# Layers that, when missing, flip the gate red. L9 itself is always green
# because the aggregator IS L9 — the act of writing this file proves it ran.
REQUIRED_LAYERS = {0, 1, 2, 3, 4, 5, 6, 7, 8}

# Named sub-tool XML files that live alongside the combined layer XML.
# Format: {layer_number: [filename_relative_to_gates_dir, ...]}
# A missing sub-tool XML is surfaced as a skipped entry — it does NOT flip
# the layer red on its own; the combined layer XML is the gate authority.
SUB_TOOL_XMLS: dict[int, list[str]] = {
    0: [
        "L0-pyright.xml",
        "L0-ruff.xml",
        "L0-hadolint.xml",
    ],
    4: [
        "L4-trivy.xml",
        "L4-syft.xml",
        "L4-dockle.xml",
    ],
    5: [
        "L5-schemathesis.xml",
        "L5-idor-grid.xml",
    ],
    6: [
        "L6-zap.xml",
        "L6-nuclei.xml",
        # L6-dos sub-tools — produced by the .L6-dos-passed recipe
        "L6-dos-redos.xml",
        "L6-dos-decompression.xml",
        "L6-dos-jsonbomb.xml",
        "L6-dos-slowloris.xml",
    ],
    7: [
        "L7-garak-sse.xml",
        "L7-garak-json.xml",
        "L7-llm-top10.xml",
    ],
    8: [
        "L8-auth-session.xml",
        "L8-auth-runtime-pytest.xml",
    ],
}


@dataclass
class ToolStats:
    """Per-tool counts within a layer's JUnit aggregate."""
    tests: int = 0
    failures: int = 0
    errors: int = 0
    skipped: int = 0
    xml_path: str = ""


@dataclass
class LayerResult:
    layer: int
    name: str
    ok: bool = True
    tools: dict[str, ToolStats] = field(default_factory=dict)
    duration_s: float = 0.0
    missing: bool = False
    stub: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "name": self.name,
            "ok": self.ok,
            "tools": {
                k: {
                    "tests": v.tests,
                    "failures": v.failures,
                    "errors": v.errors,
                    "skipped": v.skipped,
                    "xml_path": v.xml_path,
                }
                for k, v in self.tools.items()
            },
            "duration_s": round(self.duration_s, 3),
            "missing": self.missing,
            "stub": self.stub,
        }


@dataclass
class Finding:
    severity: str
    tool: str
    title: str
    layer: int | None = None
    file: str | None = None
    line: int | None = None

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "severity": self.severity,
            "tool": self.tool,
            "title": self.title,
        }
        if self.layer is not None:
            d["layer"] = self.layer
        if self.file is not None:
            d["file"] = self.file
        if self.line is not None:
            d["line"] = self.line
        return d


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _intattr(elem: ET.Element, key: str) -> int:
    """Read an integer attribute; treat missing/garbage as 0."""
    v = elem.get(key)
    if v is None:
        return 0
    try:
        return int(v)
    except ValueError:
        try:
            return int(float(v))
        except ValueError:
            return 0


def _floatattr(elem: ET.Element, key: str) -> float:
    v = elem.get(key)
    if v is None:
        return 0.0
    try:
        return float(v)
    except ValueError:
        return 0.0


def _is_stub(suite: ET.Element) -> bool:
    """A stub layer suite has a single skipped testcase named 'not-yet-implemented'."""
    cases = list(suite.iter("testcase"))
    if len(cases) != 1:
        return False
    skipped = cases[0].find("skipped")
    if skipped is None:
        return False
    name = cases[0].get("name", "")
    return name == "not-yet-implemented"


def parse_layer_xml(layer: int, name: str, xml_path: Path) -> tuple[LayerResult, list[Finding]]:
    """Parse a layer's JUnit XML into a LayerResult + finding list.

    Missing files become a missing-flagged LayerResult + a single critical
    finding. Parse errors become a layer-error LayerResult + a critical finding.
    Neither raises.
    """
    result = LayerResult(layer=layer, name=name)
    findings: list[Finding] = []

    if not xml_path.is_file():
        result.missing = True
        result.ok = False
        findings.append(Finding(
            severity="critical",
            tool="aggregator",
            title=f"layer missing: L{layer} ({name}) — no XML at {xml_path}",
            layer=layer,
        ))
        return result, findings

    try:
        tree = ET.parse(xml_path)
    except ET.ParseError as e:
        result.ok = False
        findings.append(Finding(
            severity="critical",
            tool="aggregator",
            title=f"layer L{layer} XML parse error: {e}",
            layer=layer,
            file=str(xml_path),
        ))
        return result, findings

    root = tree.getroot()
    # Root can be either <testsuites> or a single <testsuite>.
    if root.tag == "testsuites":
        suites = list(root.iter("testsuite"))
    elif root.tag == "testsuite":
        suites = [root]
    else:
        result.ok = False
        findings.append(Finding(
            severity="critical",
            tool="aggregator",
            title=f"layer L{layer} XML has unexpected root tag {root.tag!r}",
            layer=layer,
            file=str(xml_path),
        ))
        return result, findings

    total_dur = 0.0
    layer_ok = True
    is_pure_stub = bool(suites) and all(_is_stub(s) for s in suites)

    for suite in suites:
        tool_name = suite.get("name") or "unknown"
        stats = ToolStats(
            tests=_intattr(suite, "tests"),
            failures=_intattr(suite, "failures"),
            errors=_intattr(suite, "errors"),
            skipped=_intattr(suite, "skipped"),
            xml_path=str(xml_path),
        )
        # Some emitters omit `skipped` attr; count <skipped/> children.
        if stats.skipped == 0:
            stats.skipped = sum(1 for _ in suite.iter("skipped"))
        # Some emitters omit `failures`; count <failure/> children.
        if stats.failures == 0:
            stats.failures = sum(1 for _ in suite.iter("failure"))
        if stats.errors == 0:
            stats.errors = sum(1 for _ in suite.iter("error"))

        result.tools[tool_name] = stats
        total_dur += _floatattr(suite, "time")

        if stats.failures > 0 or stats.errors > 0:
            layer_ok = False

        # Surface each failure/error as a finding.
        for case in suite.iter("testcase"):
            case_name = case.get("name", "?")
            for failure in case.findall("failure"):
                findings.append(Finding(
                    severity="high",
                    tool=tool_name,
                    title=f"{case_name}: {failure.get('message', 'failure')}",
                    layer=layer,
                ))
            for err in case.findall("error"):
                findings.append(Finding(
                    severity="critical",
                    tool=tool_name,
                    title=f"{case_name}: {err.get('message', 'error')}",
                    layer=layer,
                ))

    result.duration_s = total_dur
    result.ok = layer_ok
    result.stub = is_pure_stub
    return result, findings


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, check=False, timeout=2,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    return ""


def aggregate(gates_dir: Path) -> tuple[dict[str, Any], int]:
    """Walk gates_dir, build report dict. Returns (report, exit_code)."""
    layer_results: list[LayerResult] = []
    all_findings: list[Finding] = []
    total_wall = 0.0
    started = time.monotonic()

    for layer, slug, name in LAYERS:
        if layer == 9:
            # L9 is this aggregator. It cannot report on itself — it succeeded
            # by virtue of producing this file.
            l9 = LayerResult(layer=9, name=name, ok=True)
            l9.tools["aggregator"] = ToolStats(tests=1)
            layer_results.append(l9)
            continue
        xml_path = gates_dir / f"L{layer}-{slug}.xml"
        lr, fns = parse_layer_xml(layer, name, xml_path)

        # Merge named sub-tool XMLs into the layer result so the
        # dashboard can show per-tool drill-down.  Missing sub-tool XMLs
        # become a single skipped entry — they do NOT override the combined
        # layer XML's pass/fail verdict.
        for sub_name in SUB_TOOL_XMLS.get(layer, []):
            sub_path = gates_dir / sub_name
            tool_key = sub_name.removesuffix(".xml")
            if tool_key in lr.tools:
                # Already parsed from the combined XML — skip duplicate.
                continue
            if not sub_path.is_file():
                lr.tools[tool_key] = ToolStats(
                    tests=1, skipped=1, xml_path=str(sub_path)
                )
                continue
            try:
                sub_tree = ET.parse(sub_path)
            except ET.ParseError:
                lr.tools[tool_key] = ToolStats(
                    tests=1, errors=1, xml_path=str(sub_path)
                )
                continue
            sub_root = sub_tree.getroot()
            sub_suites = (
                list(sub_root.iter("testsuite"))
                if sub_root.tag == "testsuites"
                else [sub_root] if sub_root.tag == "testsuite"
                else []
            )
            agg = ToolStats(xml_path=str(sub_path))
            for ss in sub_suites:
                agg.tests += _intattr(ss, "tests")
                agg.failures += _intattr(ss, "failures")
                agg.errors += _intattr(ss, "errors")
                agg.skipped += _intattr(ss, "skipped")
                if agg.skipped == 0:
                    agg.skipped = sum(1 for _ in ss.iter("skipped"))
                if agg.failures == 0:
                    agg.failures = sum(1 for _ in ss.iter("failure"))
                if agg.errors == 0:
                    agg.errors = sum(1 for _ in ss.iter("error"))
            lr.tools[tool_key] = agg

        layer_results.append(lr)
        all_findings.extend(fns)
        total_wall += lr.duration_s

    # `ok` = every required layer green AND not missing.
    ok = True
    for lr in layer_results:
        if lr.layer in REQUIRED_LAYERS and (not lr.ok or lr.missing):
            ok = False
            break

    # production_ready = ok AND no missing AND no critical findings AND no stubs
    # in required layers. This is a conservative default.
    has_critical = any(f.severity == "critical" for f in all_findings)
    has_stub_required = any(
        lr.layer in REQUIRED_LAYERS and lr.stub for lr in layer_results
    )
    production_ready = ok and not has_critical and not has_stub_required

    report = {
        "ok": ok,
        "wall_clock_seconds": round(total_wall + (time.monotonic() - started), 3),
        "layer_results": [lr.to_dict() for lr in layer_results],
        "findings": [f.to_dict() for f in all_findings],
        "owasp_llm_coverage": [],  # populated by a later stage
        "production_ready": production_ready,
        "generated_at": _dt.datetime.now(_dt.UTC).isoformat(),
        "commit": _git_head(),
    }

    exit_code = 0 if ok else 1
    return report, exit_code


# ---------------------------------------------------------------------------
# HTML dashboard
# ---------------------------------------------------------------------------

_HTML_STYLE = """
:root {
  --bg: #0e1116;
  --fg: #e6edf3;
  --muted: #8b949e;
  --green: #2ea043;
  --yellow: #d29922;
  --red: #f85149;
  --border: #30363d;
  --card: #161b22;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--fg);
  line-height: 1.5;
}
h1 { margin: 0 0 0.25rem; }
h2 { margin-top: 2rem; border-bottom: 1px solid var(--border); padding-bottom: 0.5rem; }
.subhead { color: var(--muted); font-size: 0.9rem; margin-bottom: 1.5rem; }
.status {
  display: inline-block; padding: 0.5rem 1rem; border-radius: 6px;
  font-weight: 700; font-size: 1.25rem; margin: 1rem 0;
}
.status.green { background: var(--green); color: #fff; }
.status.yellow { background: var(--yellow); color: #000; }
.status.red { background: var(--red); color: #fff; }
table { width: 100%; border-collapse: collapse; }
th, td { text-align: left; padding: 0.5rem 0.75rem; border-bottom: 1px solid var(--border); }
th { color: var(--muted); font-weight: 600; font-size: 0.85rem; text-transform: uppercase; }
tr:hover td { background: var(--card); }
.pill {
  display: inline-block; padding: 0.15rem 0.6rem; border-radius: 999px;
  font-size: 0.75rem; font-weight: 700;
}
.pill.green { background: rgba(46, 160, 67, 0.2); color: var(--green); }
.pill.red { background: rgba(248, 81, 73, 0.2); color: var(--red); }
.pill.muted { background: rgba(139, 148, 158, 0.2); color: var(--muted); }
.histogram { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.5rem; }
.histogram .bar {
  display: flex; align-items: center; gap: 0.5rem;
  background: var(--card); border: 1px solid var(--border);
  padding: 0.4rem 0.8rem; border-radius: 6px;
}
.histogram .swatch {
  display: inline-block; width: 0.75rem; height: 0.75rem; border-radius: 2px;
}
.swatch.critical { background: #b62324; }
.swatch.high { background: var(--red); }
.swatch.medium { background: var(--yellow); }
.swatch.low { background: #58a6ff; }
.swatch.info { background: var(--muted); }
a { color: #58a6ff; }
.findings-list { margin-top: 1rem; max-height: 24rem; overflow-y: auto;
  border: 1px solid var(--border); border-radius: 6px; }
.findings-list .row { padding: 0.5rem 0.75rem; border-bottom: 1px solid var(--border); }
.findings-list .row:last-child { border-bottom: none; }
.findings-list .meta { color: var(--muted); font-size: 0.8rem; }
code { background: var(--card); padding: 0.05rem 0.3rem; border-radius: 3px; font-size: 0.85em; }
"""


def render_html(report: dict[str, Any], gates_dir: Path) -> str:
    """Plain HTML/CSS dashboard. No JS framework."""
    if report["production_ready"]:
        status_class, status_text = "green", "PRODUCTION-READY"
    elif report["ok"]:
        status_class, status_text = "yellow", "DEGRADED"
    else:
        status_class, status_text = "red", "FAIL"

    # Findings histogram by severity.
    sev_counts: dict[str, int] = {}
    for f in report["findings"]:
        sev_counts[f["severity"]] = sev_counts.get(f["severity"], 0) + 1

    # Build table rows.
    rows: list[str] = []
    for lr in report["layer_results"]:
        if lr["missing"]:
            badge = '<span class="pill red">MISSING</span>'
        elif lr["stub"]:
            badge = '<span class="pill muted">STUB</span>'
        elif lr["ok"]:
            badge = '<span class="pill green">PASS</span>'
        else:
            badge = '<span class="pill red">FAIL</span>'

        # Link to the layer XML if it exists.
        for layer_num, slug, _ in LAYERS:
            if layer_num == lr["layer"]:
                xml_rel = f"L{layer_num}-{slug}.xml"
                break
        else:
            xml_rel = ""
        link = (
            f'<a href="{h(xml_rel)}">{h(xml_rel)}</a>'
            if xml_rel and (gates_dir / xml_rel).is_file()
            else '<span class="meta">—</span>'
        )

        tools_summary = ", ".join(
            f"{h(name)} ({s['tests']}t/{s['failures']}f/{s['skipped']}s)"
            for name, s in lr["tools"].items()
        ) or "<span class='meta'>—</span>"

        rows.append(
            f"<tr>"
            f"<td>L{lr['layer']}</td>"
            f"<td>{h(lr['name'])}</td>"
            f"<td>{badge}</td>"
            f"<td>{lr['duration_s']:.2f}s</td>"
            f"<td>{tools_summary}</td>"
            f"<td>{link}</td>"
            f"</tr>"
        )

    # Findings list.
    findings_html: list[str] = []
    for f in report["findings"]:
        sev = h(f["severity"])
        findings_html.append(
            f'<div class="row">'
            f'<span class="swatch {sev}"></span> '
            f'<strong>{sev.upper()}</strong> '
            f'<span class="meta">[{h(f["tool"])}'
            f'{" L" + str(f["layer"]) if f.get("layer") is not None else ""}]</span> '
            f'{h(f["title"])}'
            f"</div>"
        )

    # Histogram bars.
    hist_html = "".join(
        f'<div class="bar"><span class="swatch {h(sev)}"></span>'
        f'<strong>{count}</strong> <span class="meta">{h(sev)}</span></div>'
        for sev, count in sorted(sev_counts.items())
    ) or '<span class="meta">No findings.</span>'

    commit_line = (
        f'commit <code>{h(report["commit"][:12])}</code> · '
        if report.get("commit") else ""
    )

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>lm-chat production-gate report</title>
  <style>{_HTML_STYLE}</style>
</head>
<body>
  <h1>lm-chat production-gate</h1>
  <div class="subhead">
    {commit_line}generated <code>{h(report.get("generated_at", ""))}</code>
    · wall-clock <code>{report["wall_clock_seconds"]:.2f}s</code>
  </div>

  <div class="status {status_class}">{status_text}</div>

  <h2>Layer results</h2>
  <table>
    <thead>
      <tr><th>#</th><th>Layer</th><th>Status</th><th>Duration</th>
          <th>Tools</th><th>JUnit XML</th></tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>

  <h2>Findings ({len(report["findings"])})</h2>
  <div class="histogram">{hist_html}</div>
  <div class="findings-list">{
    ''.join(findings_html)
    or '<div class="row"><span class="meta">No findings recorded.</span></div>'
  }</div>

  <h2>Notes</h2>
  <ul>
    <li>Extended aggregator: all L0-L8 sub-tool XMLs are now discovered
    and surfaced in per-layer drill-down cards. HTML rendering is delegated
    to <code>tools/render_dashboard.py</code>.</li>
    <li>Future work extends this
    dashboard with certification + anti-fabrication primitives
    (sha256 provenance, review attestation, signed in-toto).</li>
  </ul>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--gates-dir",
        type=Path,
        default=Path(os.environ.get("GATES_DIR", "target/gates")),
        help="Directory holding L*-*.xml files (default: target/gates).",
    )
    args = p.parse_args(argv)

    gates_dir: Path = args.gates_dir
    gates_dir.mkdir(parents=True, exist_ok=True)

    report, exit_code = aggregate(gates_dir)

    report_path = gates_dir / "report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")

    # Delegate HTML rendering to tools/render_dashboard.py.
    # Import dynamically so this module stays importable even if render_dashboard
    # is not on sys.path (tests import aggregate_junit directly).
    html_path = gates_dir / "index.html"
    try:
        _render_spec = importlib.util.spec_from_file_location(
            "render_dashboard",
            Path(__file__).parent / "render_dashboard.py",
        )
        if _render_spec is not None and _render_spec.loader is not None:
            _render_mod = importlib.util.module_from_spec(_render_spec)
            _render_spec.loader.exec_module(_render_mod)  # type: ignore[union-attr]
            html_path.write_text(
                _render_mod.render(report, gates_dir), encoding="utf-8"
            )
        else:
            # Fallback: inline renderer (should not happen in normal usage)
            html_path.write_text(render_html(report, gates_dir), encoding="utf-8")
    except Exception:  # noqa: BLE001
        # Never let a rendering failure prevent the report from being written.
        html_path.write_text(render_html(report, gates_dir), encoding="utf-8")

    summary = "PRODUCTION-READY" if report["production_ready"] else (
        "DEGRADED" if report["ok"] else "FAIL"
    )
    print(
        f"L9 aggregator: STATUS={summary} "
        f"ok={report['ok']} layers={len(report['layer_results'])} "
        f"findings={len(report['findings'])}"
    )
    print(f"  report: {report_path}")
    print(f"  html:   {html_path}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
