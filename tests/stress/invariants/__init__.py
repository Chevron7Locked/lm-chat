# SPDX-License-Identifier: Apache-2.0
"""Post-run database invariants.

Six pytest modules — all marked ``stress_invariant`` so they do NOT run
in the default unit-tier (the default invocation is ``uv run pytest -m
'not stress_invariant'`` once this marker is registered in
``pyproject.toml``).  ``run_stress.py`` invokes them explicitly via
``uv run pytest -m stress_invariant tests/stress/invariants/`` after the
Locust phase completes.

Each module queries the DB the load just populated and asserts a single
clean invariant.  No partial / soft assertions.
"""
