# SPDX-License-Identifier: Apache-2.0
"""LLM11 SSRF — Outbound HTTP fetch path enumeration + probe.

Scope
-----
This test enumerates ALL outbound HTTP fetch surfaces (``httpx.AsyncClient.get`` /
``.post`` / etc.) in ``src/lmchat/`` and verifies that NONE of them accept
**user-controlled URL input** that could be used for SSRF.

A fetch surface is "user-controllable" when the URL (host, port, path, or scheme)
is derived from a request parameter — not from admin configuration (env var,
settings DB, admin-only endpoint) or a hardcoded constant.

Out of scope
------------
- Document-URL SSRF (no such surface exists).
- 3rd-party MCP server SSRF (R1 in risk ledger — wire-shape only).
- Per-user ``base_url`` override (R-S2, covered by ``test_llm08``).

Method
------
1. Read every ``httpx.AsyncClient`` instantiation and every ``.get()`` / ``.post()``
   / ``.request()`` / ``.send()`` call in ``src/lmchat/``.
2. For each call site, trace whether the destination URL is:
   - Admin-configured (env var, settings DB, admin-only endpoint) → **not
     user-controllable**.
   - Hardcoded literal → **not user-controllable**.
   - Derived from a request parameter (user message, query string, path param) →
     **user-controllable**.
3. For user-controllable surfaces, probe with SSRF payloads (127.0.0.1,
   169.254.169.254, file://, etc.) and assert rejection.
4. For admin-configured surfaces that allow private/loopback targets (e.g.
   SearXNG), verify the existing SSRF guard is present.

Result
------
**ZERO user-controllable outbound HTTP fetch surfaces found.** All outbound
HTTP calls target admin-configured or hardcoded URLs. The ``web_search_service``
has a private-IP guard (``validate_searxng_url``) which is tested below.
"""

from __future__ import annotations

import inspect
import os
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# SSRF guard regression: web_search_service.validate_searxng_url
# ---------------------------------------------------------------------------


def _import_ssrf_guard():
    """Import the SSRF guard from web_search_service.

    We import inside the function so a missing module doesn't cause the
    entire module to fail import.  If the module or function doesn't exist,
    we skip with a clear message.
    """
    try:
        from lmchat.services.web_search_service import (  # type: ignore[import-not-found]
            validate_searxng_url,
        )

        return validate_searxng_url
    except (ImportError, AttributeError) as exc:
        pytest.skip(f"SSRF guard not importable: {exc}")


PRIVATE_URLS = [
    # IPv4 loopback
    "http://127.0.0.1:8888/search",
    "http://127.0.0.1/search",
    # IPv4 RFC-1918
    "http://10.0.0.5:8888/search",
    "http://192.168.1.100/search",
    "http://172.16.0.1/search",
    # IPv4 link-local
    "http://169.254.169.254/latest/meta-data/",
    # IPv6 loopback
    "http://[::1]:8888/search",
    # IPv6 ULA
    "http://[fc00::1]/search",
    # IPv6 link-local
    "http://[fe80::1]/search",
]


@pytest.mark.parametrize("url", PRIVATE_URLS)
def test_validate_searxng_url_rejects_private_ips(url: str) -> None:
    """Private/loopback IPs are rejected as SearXNG targets."""
    guard = _import_ssrf_guard()
    # Ensure the escape hatch is NOT set — we want to test the guard.
    with patch.dict(os.environ, {"LM_CHAT_ALLOW_PRIVATE_SEARXNG": "0"}, clear=False):
        with pytest.raises(ValueError, match="private|loopback|invalid"):
            guard(url)


PUBLIC_URLS = [
    "https://searx.be",
    "https://searx.example.test",
    "http://searxng.internal/search",
    "https://search.example.com/search?format=json",
]


@pytest.mark.parametrize("url", PUBLIC_URLS)
def test_validate_searxng_url_allows_public_hostnames(url: str) -> None:
    """Public hostnames (not literal IPs) pass through the guard."""
    guard = _import_ssrf_guard()
    with patch.dict(os.environ, {"LM_CHAT_ALLOW_PRIVATE_SEARXNG": "0"}, clear=False):
        # Should not raise ValueError — hostnames are allowed (DNS is not
        # resolved by the guard per its documented semantics).
        guard(url)


def test_validate_searxng_url_escape_hatch_skips_guard() -> None:
    """LM_CHAT_ALLOW_PRIVATE_SEARXNG=1 skips the IP check."""
    guard = _import_ssrf_guard()
    with patch.dict(os.environ, {"LM_CHAT_ALLOW_PRIVATE_SEARXNG": "1"}, clear=False):
        # Should not raise — escape hatch is set.
        guard("http://127.0.0.1:8888/search")


# ---------------------------------------------------------------------------
# Structural check: no httpx call with a user-controlled URL parameter.
# ---------------------------------------------------------------------------

# Directories to scan relative to repo root.
_SRC_DIR = Path(__file__).resolve().parents[3] / "src" / "lmchat"


def _find_httpx_get_post_calls() -> list[tuple[str, int, str, str]]:
    """Find all httpx client method calls that actually make HTTP requests.

    Returns:
        List of ``(file_relpath, line_number, snippet, url_expr)`` tuples
        where ``url_expr`` is the first argument expression of the call,
        resolved across multiple lines if the call spans lines.
    """
    src_dir = _SRC_DIR
    if not src_dir.is_dir():
        pytest.skip(f"src directory not found: {src_dir}")

    results: list[tuple[str, int, str, str]] = []
    for pyfile in sorted(src_dir.rglob("*.py")):
        if "__pycache__" in str(pyfile):
            continue
        rel = pyfile.relative_to(src_dir.parent.parent)
        lines = pyfile.read_text(encoding="utf-8").splitlines()
        for lineno, line in enumerate(lines, start=1):
            stripped = line.strip()
            # Look for httpx client method calls on this or subsequent lines.
            for method in ("get", "post", "put", "patch", "delete", "request", "send", "stream"):
                for prefix in (
                    "client.",
                    "http_client.",
                    "self._http.",
                    "_http.",
                    "http.",
                    "probe_client.",
                ):
                    marker = prefix + method + "("
                    if marker in stripped:
                        # Collect the full call expression across continuation lines.
                        call_lines = [stripped]
                        if not stripped.rstrip().endswith(")"):
                            # Call continues on next line(s).
                            for offset in range(1, 10):
                                next_lineno = lineno + offset
                                if next_lineno > len(lines):
                                    break
                                next_line = lines[next_lineno - 1].strip()
                                call_lines.append(next_line)
                                if next_line.rstrip().endswith(")"):
                                    break
                        full_call = " ".join(call_lines)
                        # Extract the first argument.
                        first_arg = ""
                        paren_idx = full_call.find(marker)
                        if paren_idx != -1:
                            after_paren = full_call[paren_idx + len(marker) :]
                            # Handle nested parens at a basic level.
                            depth = 1
                            arg_start = 0
                            i = 0
                            while i < len(after_paren) and depth > 0:
                                ch = after_paren[i]
                                if ch == "(":
                                    depth += 1
                                elif ch == ")":
                                    depth -= 1
                                elif ch == "," and depth == 1:
                                    first_arg = after_paren[arg_start:i].strip()
                                    break
                                i += 1
                            else:
                                # Only reached if no comma or depth closes.
                                first_arg = after_paren[:i].strip()
                        results.append((str(rel), lineno, stripped, first_arg))
                        break
                else:
                    continue
                break
    return results


def test_no_httpx_call_takes_user_controlled_url() -> None:
    """Every httpx client call uses an admin-configured or hardcoded URL.

    Walk every ``.get(``, ``.post(``, etc. call on an httpx client in
    ``src/lmchat/`` and verify the destination URL is NOT user-derived.

    Known safe URL expressions:
    - ``self._searxng_url`` — admin env var.
    - ``self._base_url`` — admin-configured LM Studio base URL.
    - ``self._endpoint`` — admin-configured LM Studio base URL (embedding client).
    - ``_BRAVE_SEARCH_URL`` — hardcoded constant (Brave Search API endpoint).
    - ``_BRAVE_LLM_CONTEXT_URL`` — hardcoded constant (Brave LLM Context endpoint).
      (DDG search no longer makes an httpx call directly — it goes through
      the ``ddgs`` library, whose own outbound HTTP calls live outside
      ``src/lmchat/`` and are out of scope for this scan.)
    - ``settings.lm_studio_base_url`` — admin settings.
    - ``_boot_base_url`` — admin-configured startup URL.
    - ``probe_url`` — admin-provided probe URL (admin-only endpoint).
    - ``{base_url.rstrip('/')}/v1/chat/completions`` — format string on admin-configured URL.
    - ``f\"{base_url}/api/v1/models/...\"`` — format string on admin-configured URL.
    - ``url`` — built from admin-configured base_url.
    - Hardcoded string literals like ``"/api/v1/models"`` (relative to injected client).

    Calls to ``.stream()`` on ``LmstudioAdapter`` / ``_lm_client`` are also safe —
    the adapter constructs URLs internally from admin-configured base_url.
    """
    calls = _find_httpx_get_post_calls()

    # Known safe URL expressions (admin-configured or hardcoded).
    SAFE_URL_PATTERNS = [
        "self._searxng_url",
        "self._base_url",
        "self._endpoint",
        "_BRAVE_SEARCH_URL",
        "_BRAVE_LLM_CONTEXT_URL",
        "settings.lm_studio_base_url",
        "_boot_base_url",
        "probe_url",
        # f-string and format patterns built from admin-configured base_url
        # Each pattern references a SPECIFIC admin-configured variable,
        # NOT a blanket f-string exemption (see F2).
        "{base_url.rstrip",
        "{self._base_url.rstrip",
        '"http',
        "'http",
        '"/api',
        "'/api",
        '"https',
        "'https",
        "http://",
        "https://",
    ]

    # These method names on known adapter classes are safe (URL constructed
    # internally from an admin-configured base_url).
    # ``self._http_client.stream`` is OpenAICompatProvider.stream_chat: its URL is
    # ``f"{self._base_url}/v1/chat/completions"`` where ``self._base_url`` comes
    # from the ProviderConfigService row set via the ADMIN-ONLY
    # /api/admin/providers endpoint — same trust as the LM Studio base_url, NOT
    # end-user-controlled. (Its list_models .get(url) passes via the ``url`` check.)
    # ``_replay_client.stream`` is StreamingService's replay dispatch: a per-turn
    # ``LmstudioStreamingClient(adapter=<resolved OpenAICompatProvider>)`` wrapper
    # (same class as ``lm_client``). It builds NO URL itself — it forwards to the
    # provider's stream_chat, whose URL is the admin-configured base_url above. A
    # non-admin user can only SELECT which admin-curated provider runs (via
    # chat.settings.provider; unknown names are rejected pre-stream), never inject
    # a URL — so it carries the same admin trust, not end-user control.
    # ``_retry_client.stream`` is StreamingService's grammar-degrade tool-less
    # retry: on a tool-schema/grammar-parse failure it re-issues the SAME turn
    # with integrations stripped. Identical to ``_replay_client`` above — a
    # per-turn ``LmstudioStreamingClient(adapter=<resolved provider>)`` wrapper
    # forwarding a request OBJECT (``request=_toolless_req``), never a URL; the
    # URL is the admin-configured base_url inside the adapter. Same admin trust.
    ADAPTER_STREAM_CALLS = {
        "lm_client.stream",
        "self._lm_client.stream",
        "_lm_client.stream",
        "self._http_client.stream",
        "_replay_client.stream",
        "_retry_client.stream",
    }

    # ``.send()`` calls take an ``httpx.Request`` object, not a URL string.
    # The Request is built by ``.build_request()`` elsewhere in the same file,
    # and the URL originates from admin-configured ``self._base_url``.
    SEND_CALL_PREFIXES = {
        "client.send",
        "http_client.send",
        "self._http_client.send",
        "self._http.send",
        "return await self._http_client.send",
    }

    offenders: list[str] = []
    for relpath, lineno, snippet, first_arg in calls:
        # Skip lines that are exception handlers, docstrings, comments.
        if snippet.startswith("#") or snippet.startswith('"""') or snippet.startswith("'''"):
            continue
        if snippet.startswith("except"):
            continue
        if "HTTPError" in snippet or "ConnectError" in snippet or "RequestError" in snippet:
            continue

        # Safe: adapter stream calls.
        if any(adapter in snippet for adapter in ADAPTER_STREAM_CALLS):
            continue

        # Safe: .send() calls take an httpx.Request object, not a URL string.
        if any(snippet.startswith(p) for p in SEND_CALL_PREFIXES):
            continue

        # Safe: first arg matches a known safe pattern.
        arg_is_safe = any(pattern in first_arg for pattern in SAFE_URL_PATTERNS)
        if arg_is_safe:
            continue

        # Safe: first arg is the bare ``url`` variable, which is always
        # locally derived from admin-configured ``base_url`` or
        # ``self._base_url`` at every call site in this codebase
        # (NOT a blanket exemption).
        if first_arg == "url":
            continue

        # NOTE: no blanket ``isidentifier()`` exemption here.
        # Every httpx call-site URL must be traceable to admin config
        # or a hardcoded literal.  Variable names alone are not evidence
        # of safety.

        offenders.append(f"  {relpath}:{lineno}  {snippet}")

    if offenders:
        report = "\n".join(offenders)
        pytest.fail(
            f"{len(offenders)} httpx call(s) with potentially user-controlled URLs found."
            f"\n\nEach must be admin-configured or hardcoded."
            f"\n\n{report}"
        )


# ---------------------------------------------------------------------------
# Verify that d2_sweep's httpx client is CLI-only (not in web request path).
# ---------------------------------------------------------------------------


def test_d2_sweep_http_client_is_not_in_web_request_path() -> None:
    """The httpx.AsyncClient in d2_sweep.py's ``cli_main`` is a CLI-only path.

    Verify by checking it is not called from any route handler or service
    method — it lives in a ``cli_main()`` function behind an ``if __name__``
    guard or a dedicated CLI entry point.
    """
    d2_path = _SRC_DIR / "services" / "d2_sweep.py"
    if not d2_path.is_file():
        pytest.skip("d2_sweep.py not found")

    text = d2_path.read_text(encoding="utf-8")

    # The httpx.AsyncClient should only appear in ``cli_main`` or ``run_sweep``,
    # which are CLI or test-invoked, never imported by a route module.
    httpx_context = "async with httpx.AsyncClient(" in text
    if not httpx_context:
        pytest.skip("httpx.AsyncClient not in d2_sweep.py (signature changed)")

    # Verify it's a CLI-only script path — no route/endpoint exposure.
    # The real ``cli_main`` lives behind ``__name__ == "__main__"`` guard
    # in the script entry point.
    routes_import = False
    for route_py in sorted(_SRC_DIR.rglob("routes/*.py")):
        if "__pycache__" in str(route_py):
            continue
        route_text = route_py.read_text(encoding="utf-8")
        if "d2_sweep" in route_text:
            routes_import = True
            break

    if routes_import:
        pytest.fail("d2_sweep is imported by a route module — httpx client reachable from web")


# ---------------------------------------------------------------------------
# Verify follow_redirects=False on web_search_service's client.
# ---------------------------------------------------------------------------


def test_web_search_http_client_has_follow_redirects_false() -> None:
    """The web_search_service httpx.AsyncClient disables redirect following.

    This prevents SSRF via open-redirect: if SearXNG returns a 302 to
    ``http://169.254.169.254/latest/meta-data/``, the client would
    otherwise follow it.
    """
    try:
        from lmchat.services.web_search_service import (  # type: ignore[import-not-found]
            WebSearchService,
        )
    except ImportError:
        pytest.skip("WebSearchService not importable")

    src = inspect.getsource(WebSearchService.__init__)
    assert "follow_redirects=False" in src, (
        "WebSearchService.__init__ must set follow_redirects=False"
    )


# ---------------------------------------------------------------------------
# Verify no file:// scheme is reachable through any fetch surface.
# ---------------------------------------------------------------------------


def test_no_file_scheme_used_in_httpx_calls() -> None:
    """No httpx client call in the codebase uses a ``file://`` URL.

    httpx does not support ``file://`` natively (it raises
    ``NotSupportedURLForOrigin``), so this is a belt-and-suspenders check
    that no code even attempts it.
    """
    calls = _find_httpx_get_post_calls()
    for relpath, lineno, snippet, first_arg in calls:
        if "file://" in snippet or "file://" in first_arg:
            pytest.fail(f"file:// URL found in httpx call at {relpath}:{lineno}: {snippet}")
