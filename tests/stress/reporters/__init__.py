# SPDX-License-Identifier: Apache-2.0
"""Reporters for the stress harness.

- ``slo_report`` — reads Locust's CSV event log, computes p50/p95/p99/TTFT
  per scenario, writes ``target/stress/slo_report.json``.  Excludes
  ``warmup=true`` tagged events from the arithmetic.
- ``junit_xml`` — emits a JUnit-XML pass/fail file per scenario for CI.
"""
