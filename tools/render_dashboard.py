#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""HTML dashboard renderer for the production-gate report.

Reads ``target/gates/report.json`` and emits ``target/gates/index.html``.
Invoked by ``tools/aggregate_junit.py`` and directly by the
``make target/gates/.L9-passed`` recipe.

Design constraints:
- No external assets; inline CSS only.
- Plain semantic HTML; no JS framework.
- Must work without network access (standalone HTML).
- No emoji in output.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from html import escape as h
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Inline CSS (dark-mode)
# ---------------------------------------------------------------------------

_CSS = """
:root {
  --bg: #0e1116;
  --fg: #e6edf3;
  --muted: #8b949e;
  --green: #2ea043;
  --yellow: #d29922;
  --red: #f85149;
  --blue: #58a6ff;
  --border: #30363d;
  --card: #161b22;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 2rem;
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
  background: var(--bg); color: var(--fg); line-height: 1.5;
}
h1 { margin: 0 0 0.25rem; font-size: 1.6rem; }
h2 {
  margin-top: 2.5rem; border-bottom: 1px solid var(--border);
  padding-bottom: 0.5rem; font-size: 1.1rem;
  text-transform: uppercase; letter-spacing: 0.05em; color: var(--muted);
}
h3 { margin: 1.5rem 0 0.5rem; font-size: 1rem; color: var(--fg); }
.subhead { color: var(--muted); font-size: 0.9rem; margin-bottom: 1.5rem; }
.status-badge {
  display: inline-block; padding: 0.5rem 1.25rem; border-radius: 6px;
  font-weight: 700; font-size: 1.35rem; margin: 0.75rem 0 1.5rem;
  letter-spacing: 0.04em;
}
.status-badge.green  { background: var(--green); color: #fff; }
.status-badge.yellow { background: var(--yellow); color: #000; }
.status-badge.red    { background: var(--red);  color: #fff; }
table { width: 100%; border-collapse: collapse; margin-top: 0.5rem; }
th, td {
  text-align: left; padding: 0.5rem 0.75rem;
  border-bottom: 1px solid var(--border); vertical-align: top;
}
th { color: var(--muted); font-weight: 600; font-size: 0.8rem; text-transform: uppercase; }
tr:hover td { background: var(--card); }
.pill {
  display: inline-block; padding: 0.15rem 0.55rem; border-radius: 999px;
  font-size: 0.72rem; font-weight: 700; white-space: nowrap;
}
.pill.green  { background: rgba(46,160,67,0.18);  color: var(--green); }
.pill.red    { background: rgba(248,81,73,0.18);   color: var(--red); }
.pill.yellow { background: rgba(210,153,34,0.18);  color: var(--yellow); }
.pill.muted  { background: rgba(139,148,158,0.18); color: var(--muted); }
section { margin-bottom: 2rem; }
.summary-grid {
  display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 1rem; margin: 1rem 0 1.5rem;
}
.summary-card {
  background: var(--card); border: 1px solid var(--border); border-radius: 8px;
  padding: 1rem 1.25rem;
}
.summary-card .label { color: var(--muted); font-size: 0.8rem; text-transform: uppercase; }
.summary-card .value { font-size: 1.5rem; font-weight: 700; margin-top: 0.25rem; }
.histogram { display: flex; gap: 0.5rem; flex-wrap: wrap; margin-top: 0.5rem; }
.histogram .bar {
  display: flex; align-items: center; gap: 0.5rem;
  background: var(--card); border: 1px solid var(--border);
  padding: 0.4rem 0.8rem; border-radius: 6px; font-size: 0.85rem;
}
.swatch {
  display: inline-block; width: 0.75rem; height: 0.75rem;
  border-radius: 2px; flex-shrink: 0;
}
.swatch.critical { background: #b62324; }
.swatch.high     { background: var(--red); }
.swatch.medium   { background: var(--yellow); }
.swatch.low      { background: var(--blue); }
.swatch.info     { background: var(--muted); }
.layer-section { margin-bottom: 2rem; }
.layer-header {
  display: flex; align-items: center; gap: 0.75rem;
  padding: 0.75rem 0; border-bottom: 1px solid var(--border);
}
.layer-num { color: var(--muted); font-size: 0.9rem; min-width: 2.5rem; }
.layer-name { font-weight: 600; flex: 1; }
.layer-dur  { color: var(--muted); font-size: 0.85rem; }
.subtool-table { margin-top: 0.5rem; }
.subtool-table td, .subtool-table th { padding: 0.35rem 0.6rem; }
.findings-list {
  border: 1px solid var(--border); border-radius: 6px;
  max-height: 28rem; overflow-y: auto; margin-top: 0.75rem;
}
.findings-list .row {
  padding: 0.5rem 0.75rem; border-bottom: 1px solid var(--border);
  font-size: 0.87rem;
}
.findings-list .row:last-child { border-bottom: none; }
.meta { color: var(--muted); font-size: 0.8rem; }
code {
  background: var(--card); padding: 0.05rem 0.3rem;
  border-radius: 3px; font-size: 0.83em;
}
a { color: var(--blue); }
.owasp-table td:nth-child(3) { font-family: monospace; font-size: 0.82rem; }
"""

# ---------------------------------------------------------------------------
# Severity ordering for histogram display
# ---------------------------------------------------------------------------

_SEV_ORDER = ["critical", "high", "medium", "low", "info"]

# Layer slug table (mirrors aggregate_junit.LAYERS)
_LAYER_SLUGS: dict[int, str] = {
    0: "static-fast",
    1: "license",
    2: "unit-integration",
    3: "security-static",
    4: "container-supplychain",
    5: "dast-offline",
    6: "dast-online",
    7: "llm-security",
    8: "auth-session",
    9: "gate-report",
}

# Sub-layer display names (for layers that have non-numbered sub-sentinels)
_SUB_LAYER_DISPLAY: dict[str, str] = {
    "L6-dos": "L6-dos (DoS + availability)",
    "L8-auth": "L8-auth (Auth + session + crypto)",
}


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

def _badge(ok: bool, missing: bool, stub: bool) -> str:
    if missing:
        return '<span class="pill red">MISSING</span>'
    if stub:
        return '<span class="pill muted">STUB</span>'
    if ok:
        return '<span class="pill green">PASS</span>'
    return '<span class="pill red">FAIL</span>'


def _sev_badge(sev: str) -> str:
    cls = sev if sev in {"critical", "high", "medium", "low", "info"} else "info"
    return f'<span class="swatch {h(cls)}"></span>'


def render(report: dict[str, Any], gates_dir: Path) -> str:  # noqa: C901
    """Render report dict to HTML string."""
    prod_ready: bool = report.get("production_ready", False)
    ok: bool = report.get("ok", False)

    if prod_ready:
        badge_cls, badge_txt = "green", "PRODUCTION-READY"
    elif ok:
        badge_cls, badge_txt = "yellow", "DEGRADED"
    else:
        badge_cls, badge_txt = "red", "FAIL"

    layer_results: list[dict[str, Any]] = report.get("layer_results", [])
    findings: list[dict[str, Any]] = report.get("findings", [])
    owasp: list[dict[str, Any]] = report.get("owasp_llm_coverage", [])

    # Summary counts
    total_tests = sum(
        s["tests"]
        for lr in layer_results
        for s in lr.get("tools", {}).values()
    )
    total_failures = sum(
        s["failures"]
        for lr in layer_results
        for s in lr.get("tools", {}).values()
    )
    total_skipped = sum(
        s["skipped"]
        for lr in layer_results
        for s in lr.get("tools", {}).values()
    )
    layer_count = len(layer_results)
    wall_clock = report.get("wall_clock_seconds", 0.0)
    generated_at = report.get("generated_at", "")
    commit = report.get("commit", "")

    # Findings histogram
    sev_counts: dict[str, int] = {}
    for f in findings:
        sev = f.get("severity", "info")
        sev_counts[sev] = sev_counts.get(sev, 0) + 1

    hist_parts: list[str] = []
    for sev in _SEV_ORDER:
        cnt = sev_counts.get(sev, 0)
        hist_parts.append(
            f'<div class="bar">'
            f'{_sev_badge(sev)}'
            f' <strong>{cnt}</strong>'
            f' <span class="meta">{h(sev)}</span>'
            f'</div>'
        )
    hist_html = "".join(hist_parts) or '<span class="meta">No findings.</span>'

    # Per-layer section cards
    layer_sections: list[str] = []
    for lr in layer_results:
        lnum: int = lr["layer"]
        lname: str = lr["name"]
        ldur: float = lr.get("duration_s", 0.0)
        lmissing: bool = lr.get("missing", False)
        lstub: bool = lr.get("stub", False)
        lok: bool = lr.get("ok", True)
        tools: dict[str, Any] = lr.get("tools", {})

        badge_html = _badge(lok, lmissing, lstub)

        # Link to primary XML if it exists
        slug = _LAYER_SLUGS.get(lnum, "unknown")
        xml_name = f"L{lnum}-{slug}.xml"
        xml_link = (
            f'<a href="{h(xml_name)}">{h(xml_name)}</a>'
            if (gates_dir / xml_name).is_file()
            else f'<span class="meta">{h(xml_name)} (not present)</span>'
        )

        # Sub-tool drill-down table
        if tools:
            rows = []
            for tname, stats in tools.items():
                t = stats.get("tests", 0)
                f_ = stats.get("failures", 0)
                e_ = stats.get("errors", 0)
                sk = stats.get("skipped", 0)
                xml_p = stats.get("xml_path", "")
                if f_ > 0 or e_ > 0:
                    row_cls = ' style="color:var(--red)"'
                elif sk > 0 and t <= sk:
                    row_cls = ' style="color:var(--muted)"'
                else:
                    row_cls = ""
                # Link to per-tool XML if different from layer XML and present
                if xml_p and xml_p != str(gates_dir / xml_name):
                    xp = Path(xml_p)
                    tlink = (
                        f'<a href="{h(xp.name)}">{h(tname)}</a>'
                        if xp.is_file() else h(tname)
                    )
                else:
                    tlink = h(tname)
                rows.append(
                    f"<tr{row_cls}>"
                    f"<td>{tlink}</td>"
                    f"<td>{t}</td><td>{f_}</td><td>{e_}</td><td>{sk}</td>"
                    f"</tr>"
                )
            subtool_html = (
                '<table class="subtool-table">'
                '<thead><tr>'
                '<th>Tool</th><th>Tests</th><th>Fail</th><th>Err</th><th>Skip</th>'
                '</tr></thead><tbody>'
                + "".join(rows)
                + "</tbody></table>"
            )
        else:
            subtool_html = '<span class="meta">No sub-tool data.</span>'

        layer_sections.append(f"""
<section class="layer-section">
  <div class="layer-header">
    <span class="layer-num">L{lnum}</span>
    <span class="layer-name">{h(lname)}</span>
    {badge_html}
    <span class="layer-dur">{ldur:.2f}s</span>
    <span class="meta" style="margin-left:auto">{xml_link}</span>
  </div>
  {subtool_html}
</section>""")

    # Findings list
    findings_items: list[str] = []
    for f in findings:
        sev = f.get("severity", "info")
        tool = f.get("tool", "?")
        title = f.get("title", "")
        layer_tag = f" L{f['layer']}" if f.get("layer") is not None else ""
        file_tag = (
            f' <code>{h(f["file"])}</code>' if f.get("file") else ""
        )
        findings_items.append(
            f'<div class="row">'
            f'{_sev_badge(sev)} '
            f'<strong>{h(sev.upper())}</strong> '
            f'<span class="meta">[{h(tool)}{h(layer_tag)}]</span> '
            f'{h(title)}{file_tag}'
            f'</div>'
        )
    findings_html = "".join(findings_items) or (
        '<div class="row"><span class="meta">No findings recorded.</span></div>'
    )

    # OWASP LLM Top 10 coverage
    if owasp:
        owasp_rows = []
        for entry in owasp:
            eid = entry.get("id", "")
            status = entry.get("status", "")
            risk = entry.get("risk", "")
            test = entry.get("test", "")
            if status == "matches":
                status_badge = '<span class="pill green">matches</span>'
            elif status == "missing-on-purpose":
                status_badge = '<span class="pill muted">missing-on-purpose</span>'
            else:
                status_badge = '<span class="pill red">missing-by-accident</span>'
            owasp_rows.append(
                f"<tr>"
                f"<td><strong>{h(eid)}</strong></td>"
                f"<td>{h(risk)}</td>"
                f"<td>{status_badge}</td>"
                f"<td>{h(test)}</td>"
                f"</tr>"
            )
        owasp_html = (
            '<table class="owasp-table">'
            '<thead><tr><th>ID</th><th>Risk</th><th>Status</th><th>Test</th></tr></thead>'
            "<tbody>" + "".join(owasp_rows) + "</tbody></table>"
        )
    else:
        owasp_html = (
            '<span class="meta">OWASP LLM Top 10 coverage table not yet '
            "populated.</span>"
        )

    # Commit line
    commit_line = (
        f'commit <code>{h(commit[:12])}</code> &middot; ' if commit else ""
    )

    # Summary grid
    summary_grid = f"""
<div class="summary-grid">
  <div class="summary-card">
    <div class="label">Layers</div>
    <div class="value">{layer_count}</div>
  </div>
  <div class="summary-card">
    <div class="label">Total tests</div>
    <div class="value">{total_tests}</div>
  </div>
  <div class="summary-card">
    <div class="label">Failures</div>
    <div class="value" style="color:{
      'var(--red)' if total_failures else 'var(--green)'
    }">{total_failures}</div>
  </div>
  <div class="summary-card">
    <div class="label">Skipped</div>
    <div class="value" style="color:var(--muted)">{total_skipped}</div>
  </div>
  <div class="summary-card">
    <div class="label">Findings</div>
    <div class="value" style="color:{
      'var(--red)' if findings else 'var(--green)'
    }">{len(findings)}</div>
  </div>
  <div class="summary-card">
    <div class="label">Wall-clock</div>
    <div class="value">{wall_clock:.1f}s</div>
  </div>
</div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>lm-chat production-gate report</title>
  <style>{_CSS}</style>
</head>
<body>
  <h1>lm-chat production-gate</h1>
  <div class="subhead">
    {commit_line}generated <code>{h(generated_at)}</code>
  </div>

  <div class="status-badge {badge_cls}">{badge_txt}</div>

  <h2>Summary</h2>
  {summary_grid}

  <h2>Layer results (L0..L9 + sub-layers)</h2>
  {"".join(layer_sections)}

  <h2>Findings ({len(findings)})</h2>
  <div class="histogram">{hist_html}</div>
  <div class="findings-list">{findings_html}</div>

  <h2>OWASP LLM Top 10 coverage</h2>
  {owasp_html}

  <h2>Reading this report</h2>
  <p class="meta">
    Each layer card shows the combined pass/fail for that layer.
    Sub-tool counts are tests / failures / errors / skipped.
    Layer XML links open the raw JUnit XML for drill-down.
    A layer marked STUB has not yet been implemented (P15b-P15h replace stubs).
    A layer marked MISSING had no JUnit XML at aggregation time.
    DEGRADED means all required layers passed but stubs or skipped tools were present.
    PRODUCTION-READY means every required layer is green with no critical findings
    and no stub layers.
  </p>
  <p class="meta">
    Machine-readable counterpart: <code>report.json</code>
    (validates against <code>tests/security/audit-schemas/gate-report.schema.json</code>).
    See <code>docs/runbooks/DEPLOYMENT-GATE.md</code> for the operator runbook.
  </p>
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
        help="Directory holding report.json and L*.xml files (default: target/gates).",
    )
    p.add_argument(
        "--report",
        type=Path,
        default=None,
        help="Path to report.json (default: <gates-dir>/report.json).",
    )
    p.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Path to write index.html (default: <gates-dir>/index.html).",
    )
    args = p.parse_args(argv)

    gates_dir: Path = args.gates_dir
    report_path: Path = args.report or gates_dir / "report.json"
    output_path: Path = args.output or gates_dir / "index.html"

    if not report_path.is_file():
        print(
            f"render_dashboard: error: report.json not found at {report_path}",
            file=sys.stderr,
        )
        return 1

    report: dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
    html = render(report, gates_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    print(f"render_dashboard: wrote {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
