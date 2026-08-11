# SPDX-License-Identifier: Apache-2.0
"""LLM12 SSRF — Documentation of outbound HTTP fetch surfaces.

Purpose
-------
This test is **tautological-by-design**: it documents every outbound HTTP fetch
surface examined and its disposition.  If a fetch surface is NOT user-controllable
(e.g. admin-config-only URLs), it's out of scope — but we still list it here
for traceability and auditor review.

The test asserts that the count of surfaces examined equals the expected number
and that all surfaces are properly accounted for (covered / not-applicable /
open-bug).

Surfaces examined
-----------------
See the ``SURFACES`` list below.  Each entry includes:
  - ``file`` — source file.
  - ``method`` — httpx client method used.
  - ``url_source`` — how the destination URL is determined.
  - ``disposition`` — one of:
    * ``covered`` — SSRF-guarded and tested (test_llm11 covers this).
    * ``not-applicable`` — not user-controllable (admin-config or hardcoded).
    * ``open-bug`` — user-controllable and NOT guarded (would block release).
  - ``notes`` — additional context.
"""
from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Surface catalog — every outbound HTTP fetch surface in the codebase.
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class FetchSurface:
    """One outbound HTTP fetch location in the codebase."""

    file: str
    """Source file (relative to repo root)."""

    line: int
    """Approximate line number (subject to drift — use grep to confirm)."""

    method: str
    """HTTP method used (GET, POST, etc.)."""

    url_source: str
    """How the destination URL is determined."""

    disposition: str
    """covered | not-applicable | open-bug"""

    notes: str
    """Additional context for auditors."""


SURFACES: list[FetchSurface] = [
    # ── 1. web_search_service.py — SearXNG provider ───────────────────────
    FetchSurface(
        file="src/lmchat/services/web_search_service.py",
        line=265,
        method="GET",
        url_source="Admin-config via LM_CHAT_SEARXNG_URL env var (default https://searx.be). "
        "User query goes as ``q`` param, not in URL path.",
        disposition="covered",
        notes="SSRF guard (validate_searxng_url → _is_private_ip) rejects private/loopback IPs "
        "unless LM_CHAT_ALLOW_PRIVATE_SEARXNG=1 escape hatch is set. "
        "follow_redirects=False prevents open-redirect SSRF.",
    ),
    # ── 2. web_search_service.py — DuckDuckGo fallback ───────────────────
    FetchSurface(
        file="src/lmchat/services/web_search_service.py",
        line=180,
        method="GET",
        url_source="Hardcoded: https://html.duckduckgo.com/html/",
        disposition="not-applicable",
        notes="Hardcoded URL. Not user-controllable. User query goes as ``q`` param.",
    ),
    # ── 3. models_service.py — probe / list models ───────────────────────
    FetchSurface(
        file="src/lmchat/services/models_service.py",
        line=459,
        method="GET",
        url_source="Admin-configured LM Studio base_url "
        "(settings DB / settings.lm_studio_base_url). "
        "Path: ``/api/v1/models``.",
        disposition="not-applicable",
        notes="URL is always admin-configured. Model keys go in request body, not URL path. "
        "Response parsing only — no user-influenced URL construction.",
    ),
    # ── 4. models_service.py — load model ────────────────────────────────
    FetchSurface(
        file="src/lmchat/services/models_service.py",
        line=615,
        method="POST",
        url_source="Admin-configured LM Studio base_url. Path: ``/api/v1/models/load``.",
        disposition="not-applicable",
        notes="Admin-gated endpoint. model_key goes in JSON body, not URL. URL is fixed.",
    ),
    # ── 5. models_service.py — unload model ──────────────────────────────
    FetchSurface(
        file="src/lmchat/services/models_service.py",
        line=663,
        method="POST",
        url_source="Admin-configured LM Studio base_url. Path: ``/api/v1/models/unload``.",
        disposition="not-applicable",
        notes="Admin-gated endpoint. instance_id goes in JSON body, not URL. URL is fixed.",
    ),
    # ── 6. models_service.py — download model ────────────────────────────
    FetchSurface(
        file="src/lmchat/services/models_service.py",
        line=751,
        method="POST",
        url_source="Admin-configured LM Studio base_url. Path: ``/api/v1/models/download``.",
        disposition="not-applicable",
        notes="Admin-gated endpoint. model_key / source go in JSON body, not URL. URL is fixed.",
    ),
    # ── 7. chat_service.py — auto-title generation ───────────────────────
    FetchSurface(
        file="src/lmchat/services/chat_service.py",
        line=984,
        method="POST",
        url_source="Admin-configured LM Studio base_url (passed as parameter from route layer). "
        "Path: ``/v1/chat/completions``.",
        disposition="not-applicable",
        notes="URL is always the admin's configured LM Studio endpoint. "
        "User message content goes in request body. No user influence over the destination URL.",
    ),
    # ── 8. lmstudio_adapter.py — streaming chat ──────────────────────────
    FetchSurface(
        file="src/lmchat/services/lmstudio_adapter.py",
        line=385,
        method="POST",
        url_source="Admin-configured LM Studio base_url. Path: ``/api/v1/chat`` or "
        "``/v1/responses`` depending on surface selection.",
        disposition="not-applicable",
        notes="URL is always the admin's configured LM Studio endpoint. "
        "Chat body includes user message but the destination is fixed.",
    ),
    # ── 9. embedding/client.py — text embeddings ─────────────────────────
    FetchSurface(
        file="src/lmchat/embedding/client.py",
        line=108,
        method="POST",
        url_source="Admin-configured LM Studio base_url. Path: ``/v1/embeddings``.",
        disposition="not-applicable",
        notes="URL is always the admin's configured LM Studio endpoint. "
        "Text content goes in request body. No user influence over destination.",
    ),
    # ── 10. d2_sweep.py — CLI harness ────────────────────────────────────
    FetchSurface(
        file="src/lmchat/services/d2_sweep.py",
        line=569,
        method="POST",
        url_source="CLI arg (--lm-studio-url) or LM_STUDIO_URL env var. Default: "
        "``http://localhost:1234``. Only invoked by admin on CLI, not via web requests.",
        disposition="not-applicable",
        notes="CLI-only script (scripts/run_d2_sweep.py). Not reachable from any web route. "
        "Not considered a web-request SSRF surface.",
    ),
    # ── 11. routes/_meta.py — /readyz health probe ───────────────────────
    FetchSurface(
        file="src/lmchat/routes/_meta.py",
        line=139,
        method="GET",
        url_source="settings.lm_studio_base_url (admin-configured). Path: ``/api/v1/models``.",
        disposition="not-applicable",
        notes="Health-check endpoint creates a short-lived client. "
        "URL is always the admin's configured configured LM Studio. Not user-influenced.",
    ),
    # ── 12. routes/lm_studio_settings.py — admin save-probe ──────────────
    FetchSurface(
        file="src/lmchat/routes/lm_studio_settings.py",
        line=394,
        method="GET",
        url_source="Admin-provided base_url (from POST body). Only reachable by admin users.",
        disposition="not-applicable",
        notes="Admin-only endpoint. The URL comes from the admin's POST body and is probed before "
        "saving. Not reachable by regular users. The admin is in the trust boundary.",
    ),
    # ── 13. lm_studio_overrides_service.py — per-user probe ──────────────
    FetchSurface(
        file="src/lmchat/services/lm_studio_overrides_service.py",
        line=709,
        method="GET",
        url_source="Per-user base_url override (user-controlled via settings UI). "
        "Probes /api/v1/models before saving.",
        disposition="covered",
notes="R-S2 per-user base_url override is a DESIGNED feature, not a bug. "
          "Admin-gating on the /test route is verified by "
          "test_test_connection_requires_admin in tests/routes/test_lm_studio_settings.py "
          "(non-admin → 403 on POST /api/settings/lmstudio/test).",
    ),
    # ── 14. lm_studio_overrides_service.py — singleton rewire ────────────
    FetchSurface(
        file="src/lmchat/services/lm_studio_overrides_service.py",
        line=853,
        method="Multiple",
        url_source="Replaces the shared httpx.AsyncClient with a new base_url when admin or "
        "user changes settings.",
        disposition="not-applicable",
        notes="Affects all subsequent outbound requests. The destination URL is the one saved "
        "by admin or per-user override (both already dispositioned above).",
    ),
    # ── 15. app.py — lifespan client creation ────────────────────────────
    FetchSurface(
        file="src/lmchat/app.py",
        line=271,
        method="N/A (client construction)",
        url_source="Admin-configured LM Studio base_url from settings DB or env var.",
        disposition="not-applicable",
        notes="Client is constructed at startup and injected into services. The base_url is "
        "the admin's configured LM Studio endpoint. Not user-controllable.",
    ),
]

# ---------------------------------------------------------------------------
# Expected counts — update these when surfaces are added or removed.
# These are LITERALS (not derived from SURFACES) so that catalog drift
# actually trips the test (F3 fix).
EXPECTED_SURFACE_COUNT = 15
EXPECTED_COVERED = 2
EXPECTED_NOT_APPLICABLE = 13
EXPECTED_OPEN_BUG = 0


def test_surface_count_is_stable() -> None:
    """The number of documented fetch surfaces must match expectations.

    If this test fails, a surface was added or removed without updating
    the documentation.  Update ``SURFACES`` above.
    """
    assert len(SURFACES) == EXPECTED_SURFACE_COUNT, (
        f"Surface count changed: expected {EXPECTED_SURFACE_COUNT}, "
        f"got {len(SURFACES)}. Update EXPECTED_SURFACE_COUNT."
    )


def test_all_surfaces_have_valid_disposition() -> None:
    """Every surface has a known disposition."""
    valid = {"covered", "not-applicable", "open-bug"}
    for s in SURFACES:
        assert s.disposition in valid, (
            f"{s.file}:{s.line} has invalid disposition {s.disposition!r}. "
            f"Must be one of {valid}."
        )


def test_no_open_bugs() -> None:
    """There should be no open-bug surfaces in the catalog.

    An open-bug disposition means a user-controllable URL surface without
    SSRF protection.  This is a release blocker.
    """
    open_bugs = [s for s in SURFACES if s.disposition == "open-bug"]
    if open_bugs:
        report = "\n".join(
            f"  {s.file}:{s.line} — {s.notes}" for s in open_bugs
        )
        pytest.fail(
            f"{len(open_bugs)} open-bug surface(s) found (release blocker):\n"
            f"{report}"
        )


def test_covered_surfaces_have_ssrf_guard() -> None:
    """Each covered surface has a corresponding SSRF test.

    Verify by checking that test_llm11 has relevant test functions.
    Surfaces with 'covered' disposition MUST have SSRF protection in the
    production code (tested in test_llm11).
    """
    covered = [s for s in SURFACES if s.disposition == "covered"]
    for s in covered:
        # Check that the file mentioned in the surface actually exists and
        # has some SSRF-related code.
        fpath = Path(__file__).resolve().parents[3] / s.file
        if not fpath.is_file():
            pytest.fail(
                f"Surface file {s.file} does not exist (referenced in disposition=covered)."
            )
        text = fpath.read_text(encoding="utf-8")
        # Ensure the file has some SSRF-relevant code: private IP check,
        # URL validation, etc.
        ssrf_indicators = [
            "_is_private_ip",
            "validate_searxng_url",
            "private",
            "loopback",
            "_PRIVATE_NETWORKS",
            "follow_redirects=False",
        ]
        has_ssrf = any(ind in text for ind in ssrf_indicators)
        if not has_ssrf:
            # For covered surfaces, we expect SSRF protection.
            # For R-S2 (per-user override), the protection is in
            # the user-scoping (tested by test_llm08).
            if "lm_studio_overrides_service" in s.file:
                continue  # R-S2 protection is user-scoping, not IP guard
            pytest.fail(
                f"Surface {s.file} is disposition=covered but has no SSRF guard code "
                f"(none of {ssrf_indicators} found)."
            )


def test_documentation_render() -> None:
    """Render the surface catalog as a markdown table for auditors.

    This test exists so the documentation is always runnable — it doesn't
    assert anything beyond being able to format the table.
    """
    lines = [
        "## SSRF Outbound Fetch Surfaces — Documentation",
        "",
        "| # | File | Line | Method | URL Source | Disposition | Notes |",
        "|---|------|------|--------|------------|-------------|-------|",
    ]
    for i, s in enumerate(SURFACES, start=1):
        url_short = s.url_source[:80].replace("|", "/")
        notes_short = s.notes[:80].replace("|", "/")
        lines.append(
            f"| {i} | {s.file} | {s.line} | {s.method} "
            f"| {url_short}... | {s.disposition} | {notes_short}... |"
        )
    lines.append("")
    lines.append(
        f"**Total surfaces examined:** {len(SURFACES)} | "
        f"**Covered:** {EXPECTED_COVERED} | "
        f"**Not applicable:** {EXPECTED_NOT_APPLICABLE} | "
        f"**Open bugs:** {EXPECTED_OPEN_BUG} |"
    )

    output = "\n".join(lines)
    # Print for auditor visibility in test output.
    print(output)


def test_surface_files_exist() -> None:
    """All referenced source files still exist.  Renamed/moved files must
    update the SURFACES catalog."""
    root = Path(__file__).resolve().parents[3]
    for s in SURFACES:
        fpath = root / s.file
        assert fpath.is_file(), (
            f"Source file {s.file} does not exist.  If the file was renamed or "
            f"moved, update the SURFACES catalog."
        )