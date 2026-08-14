# SPDX-License-Identifier: Apache-2.0
"""Web search service for lm-chat — SearXNG-first with DuckDuckGo fallback.

Provider selection via ``LM_CHAT_WEB_SEARCH_PROVIDER`` (default ``"searxng"``):

1. **searxng** (default): POST to ``<LM_CHAT_SEARXNG_URL>?q=<query>&format=json``
   (instance must have ``format: json`` enabled). If SearXNG returns 5xx or
   fewer than 3 results, falls back to DDG automatically.
2. **ddg**: keyless default via the ``ddgs`` library (not a raw HTML scrape —
   that gets bot-blocked almost immediately). Synchronous ``DDGS().text()``
   runs in ``asyncio.to_thread``; client-side rate-limited to 1 req/2s.
3. **brave**: keyed API (``LM_CHAT_BRAVE_API_KEY``), GETs
   ``.../res/v1/web/search``. Standalone — no chaining to SearXNG/DDG;
   raises ``WebSearchUnavailable`` on failure or missing key.
4. **brave_llm**: Brave's LLM-optimized endpoint
   (``.../res/v1/llm/context``), same key, returns pre-extracted ranked
   snippets instead of plain links. Standalone, same as ``brave``.

``probe()`` is a lightweight SearXNG health check for the lifespan; a
failed probe never blocks ``search()``, which always falls back to DDG.

``SearchResult`` is a plain Pydantic model (``title``, ``url``, ``snippet``).
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import ipaddress
import socket
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup
from ddgs import DDGS
from ddgs.exceptions import DDGSException
from pydantic import BaseModel

from lmchat.logging import get_logger

if TYPE_CHECKING:
    pass

log = get_logger(__name__)

# ─── SSRF guard ───────────────────────────────────────────────────────────────

# Private/loopback/link-local/unspecified literal IPs are forbidden as
# SearXNG targets unless the admin explicitly sets
# LM_CHAT_ALLOW_PRIVATE_SEARXNG=1 — see _is_private_ip, which classifies
# via stdlib ipaddress address properties (is_private/is_loopback/
# is_link_local/is_unspecified) rather than a hand-rolled network list, so
# an IPv4-mapped IPv6 literal (e.g. "::ffff:192.168.1.1") is caught too.

# Tailscale CGNAT (100.64.0.0/10) — always allowed as a SearXNG target, no
# escape hatch needed. Kept as an EXPLICIT allow-list checked BEFORE
# _is_private_ip in every caller (validate_searxng_url,
# _resolve_pinned_target), rather than relying on _is_private_ip to treat
# it as non-private: whether stdlib IPv4Address.is_private classifies
# 100.64.0.0/10 as private has changed across Python versions, so it is
# this ordering — not is_private's classification of the range — that
# keeps the admin's tailnet self-hosted SearXNG reachable. Never reorder
# the Tailscale check to run after the private-IP check.
_TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")

# getaddrinfo() is a blocking syscall with no built-in timeout. A hostname
# that never registers (e.g. a typo'd ".local"/mDNS name) can hang for
# several real seconds on some resolvers — and validate_searxng_url calls
# this synchronously, including from the (unawaited) admin PATCH route.
# Bound it so a bad hostname can't stall an admin settings save or a
# service (re)construction.
_DNS_RESOLVE_TIMEOUT_SEC = 3.0


def _is_tailscale_ip(host: str) -> bool:
    """Return True if *host* is a literal IP in the Tailscale CGNAT range."""
    try:
        return ipaddress.ip_address(host) in _TAILSCALE_CGNAT
    except ValueError:
        return False


def _is_private_ip(host: str) -> bool:
    """Return True if *host* is a private/loopback/link-local/unspecified
    literal IP address.

    Uses the stdlib's own address-classification properties rather than a
    hand-rolled network list, and unwraps an IPv4-mapped IPv6 literal
    (e.g. ``::ffff:192.168.1.1``) to its underlying IPv4 address first —
    an ``IPv6Address`` is never itself a member of an IPv4
    ``ip_network``, so a manual "address in list-of-networks" check let a
    private target through whenever it was spelled in its IPv4-mapped
    IPv6 form. ``is_unspecified`` also catches the bind-any addresses
    (``0.0.0.0``, ``::``), which no RFC-1918/loopback/link-local range
    covers.

    Only checks literal IPs. Hostnames are resolved by the caller (see
    ``_resolve_host_ips``) into literal IPs first, then each resolved IP
    is checked here — this keeps the IP-membership logic in one place
    regardless of whether the caller has an original literal or a
    resolved address.
    """
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        # Not an IP literal — hostnames like "searxng.internal" pass
        # through; the admin chose the URL.
        return False
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_unspecified


def _resolve_host_ips(host: str) -> list[str]:
    """Resolve *host* to its IP address(es) via the system resolver.

    Returns an empty list when *host* is already a literal IP (nothing to
    resolve — callers check literal IPs directly via ``_is_private_ip``),
    when resolution fails (unresolvable hostname; the connection attempt
    itself will surface that on its own — not a private-IP exposure), or
    when it doesn't complete within ``_DNS_RESOLVE_TIMEOUT_SEC``.
    """
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return []

    # getaddrinfo has no timeout param; run it in a throwaway thread and
    # abandon that thread (don't join) on timeout, so a hung resolver
    # can't block the caller past the bound.
    executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    future = executor.submit(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
    executor.shutdown(wait=False)
    try:
        infos = future.result(timeout=_DNS_RESOLVE_TIMEOUT_SEC)
    except (OSError, concurrent.futures.TimeoutError):
        return []

    seen: set[str] = set()
    ips: list[str] = []
    for _family, _type, _proto, _canonname, sockaddr in infos:
        ip = sockaddr[0]
        # AF_INET/AF_INET6 sockaddrs always have a str host as element 0;
        # the non-str union member (AF_UNIX-style) never occurs for a
        # host-based lookup like this one — narrow for pyright.
        if not isinstance(ip, str):
            continue
        if ip not in seen:
            seen.add(ip)
            ips.append(ip)
    return ips


def validate_searxng_url(url: str) -> None:
    """Validate *url* is safe to use as the SearXNG endpoint.

    Raises ``ValueError`` if the URL targets a private/loopback address —
    checked both as a literal IP and, for hostnames, by resolving DNS and
    checking every returned address — and
    ``LM_CHAT_ALLOW_PRIVATE_SEARXNG=1`` is not set (the escape hatch for
    admins running a local instance, e.g. docker on 127.0.0.1:8888).

    This is a config-time check (construction/reconfigure/admin save); a
    hostname's DNS record can still change afterwards (rebinding), which
    is why ``WebSearchService`` also re-resolves and re-checks immediately
    before each actual connection (see ``_resolve_pinned_target``).
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"SearXNG URL {url!r} must use http or https scheme, got {parsed.scheme!r}."
        )

    # Read from Settings (the SSOT), not os.environ directly — the pydantic
    # field also resolves from .env.local, which never populates os.environ.
    from lmchat.config import get_settings  # noqa: PLC0415

    if get_settings().lm_chat_allow_private_searxng:
        log.info(
            "web_search.ssrf_guard.private_allowed",
            url=url,
            reason="lm_chat_allow_private_searxng=true",
        )
        return

    parsed = urlparse(url)
    host = parsed.hostname or ""
    if _is_tailscale_ip(host):
        # Tailscale net is admin-trusted — always allowed, blanket.
        log.info("web_search.ssrf_guard.tailscale_allowed", url=url, host=host)
        return
    if _is_private_ip(host):
        raise ValueError(
            f"SearXNG URL {url!r} targets a private/loopback address ({host!r}). "
            "Set LM_CHAT_ALLOW_PRIVATE_SEARXNG=1 to allow self-hosted instances."
        )

    # Not a literal IP — resolve the hostname and check every address it
    # returns, so "searxng.internal" pointing at 10.0.0.5 can't bypass the
    # guard just because it isn't a literal IP.
    for ip in _resolve_host_ips(host):
        if _is_tailscale_ip(ip):
            continue
        if _is_private_ip(ip):
            raise ValueError(
                f"SearXNG URL {url!r} hostname {host!r} resolves to a "
                f"private/loopback address ({ip!r}). Set "
                "LM_CHAT_ALLOW_PRIVATE_SEARXNG=1 to allow self-hosted instances."
            )


# Below this many results, SearXNG's result set is too sparse and
# search() enriches with a DDG fallback. Must be evaluated against the
# TRUE (pre-top_n-cap) count so a small top_n doesn't force a spurious
# fallback.
_SEARXNG_SPARSE_RESULT_THRESHOLD = 3

# DDG rate limit: 1 request per 2 seconds, on top of ddgs's own handling —
# a courtesy client-side throttle.
_DDG_MIN_INTERVAL_SEC = 2.0

# Bounds the DDG call so a hanging backend can never stall the whole chat
# turn (composer stays disabled until the turn ends). Two layers: the
# per-request HTTP timeout goes to ddgs; the outer asyncio.wait_for is a
# hard ceiling in case ddgs retries past its own timeout. On breach, fails
# soft (WebSearchUnavailable) so the agentic loop reaches a final answer.
_DDG_HTTP_TIMEOUT_SEC = 10.0
_DDG_CALL_TIMEOUT_SEC = 20.0

# Brave Search API — a real keyed search API; no scraping, no rate limit.
_BRAVE_SEARCH_URL = "https://api.search.brave.com/res/v1/web/search"
_BRAVE_MAX_COUNT = 20  # Brave clamps `count` to 20 results per request.

# Brave Search — LLM Context sub-type. Same key, different endpoint/shape.
_BRAVE_LLM_CONTEXT_URL = "https://api.search.brave.com/res/v1/llm/context"
# `count` and `maximum_number_of_urls` are both 1-50, default 20 — both set
# from top_n and share this clamp bound.
_BRAVE_LLM_CONTEXT_MAX_URLS = 50
# `q` is capped at 400 chars / 50 words server-side (422 if exceeded);
# clamp client-side so an over-long query fails soft rather than 422ing.
_BRAVE_LLM_CONTEXT_MAX_QUERY_WORDS = 50

# ─── Public model ─────────────────────────────────────────────────────────────


class SearchResult(BaseModel):
    """One web search result."""

    title: str
    url: str
    snippet: str


# ─── Parsers ──────────────────────────────────────────────────────────────────


def _parse_searxng(body: dict) -> list[SearchResult]:  # type: ignore[type-arg]
    """Parse a SearXNG JSON response body into ``SearchResult`` objects.
    Empty list on parse failure.
    """
    raw_results = body.get("results")
    if not isinstance(raw_results, list):
        log.warning("web_search.searxng.unexpected_shape", keys=list(body.keys()))
        return []

    out: list[SearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or ""
        url = item.get("url") or ""
        snippet = item.get("content") or item.get("snippet") or ""
        if not url:
            continue
        out.append(SearchResult(title=str(title), url=str(url), snippet=str(snippet)))
    return out


def _parse_ddgs_results(raw_results: list[dict]) -> list[SearchResult]:  # type: ignore[type-arg]
    """Map ``ddgs`` ``DDGS().text()`` result dicts into ``SearchResult`` objects.

    Keys are ``title``/``href``/``body`` (``ddgs.results.TextResult``, per
    installed ``ddgs==9.14.4``). Already normalized by ddgs (HTML-stripped,
    entities unescaped) — no extra stripping needed here. Items with no
    URL are dropped.
    """
    out: list[SearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or ""
        url = item.get("href") or ""
        snippet = item.get("body") or ""
        if not url:
            continue
        out.append(SearchResult(title=str(title), url=str(url), snippet=str(snippet)))
    return out


def _strip_html_tags(text: str) -> str:
    """Strip HTML tags from *text* (Brave's title/description may embed them,
    e.g. ``<strong>`` around matched query terms).

    Reuses ``BeautifulSoup`` (already a project dependency) rather than a
    hand-rolled regex, for tag-aware correctness.
    """
    return BeautifulSoup(text, "html.parser").get_text()


def _clamp_query_words(query: str, max_words: int) -> str:
    """Truncate *query* to at most *max_words* whitespace-separated words.

    Brave's LLM Context endpoint rejects (422) queries over 50 words / 400
    chars; clamping client-side avoids that failure mode outright instead
    of surfacing it as a ``WebSearchUnavailable``.
    """
    words = query.split()
    if len(words) <= max_words:
        return query
    return " ".join(words[:max_words])


def _parse_brave(body: dict) -> list[SearchResult]:  # type: ignore[type-arg]
    """Parse a Brave Search API JSON response body. Empty list on parse
    failure.
    """
    web = body.get("web")
    raw_results = web.get("results") if isinstance(web, dict) else None
    if not isinstance(raw_results, list):
        log.warning("web_search.brave.unexpected_shape", keys=list(body.keys()))
        return []

    out: list[SearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or ""
        url = item.get("url") or ""
        snippet = item.get("description") or ""
        if not url:
            continue
        out.append(
            SearchResult(
                title=_strip_html_tags(str(title)),
                url=str(url),
                snippet=_strip_html_tags(str(snippet)),
            )
        )
    return out


def _parse_brave_llm_context(body: dict) -> list[SearchResult]:  # type: ignore[type-arg]
    """Parse a Brave LLM Context API JSON response body.

    Shape: ``{"grounding": {"generic": [{"url", "title", "snippets": [...]}]}}``.
    Each item's ``snippets`` (ranked text chunks) are HTML-stripped and
    joined with ``" … "`` into one ``SearchResult.snippet``. Empty list on
    parse failure.
    """
    grounding = body.get("grounding")
    raw_results = grounding.get("generic") if isinstance(grounding, dict) else None
    if not isinstance(raw_results, list):
        log.warning("web_search.brave_llm.unexpected_shape", keys=list(body.keys()))
        return []

    out: list[SearchResult] = []
    for item in raw_results:
        if not isinstance(item, dict):
            continue
        title = item.get("title") or ""
        url = item.get("url") or ""
        snippets = item.get("snippets") or []
        if not url:
            continue
        chunks = snippets if isinstance(snippets, list) else []
        snippet = " … ".join(_strip_html_tags(str(chunk)) for chunk in chunks)
        out.append(
            SearchResult(
                title=_strip_html_tags(str(title)),
                url=str(url),
                snippet=snippet,
            )
        )
    return out


# ─── Service ──────────────────────────────────────────────────────────────────


class WebSearchUnavailable(Exception):
    """Raised when web search fails at the transport/backend level — every
    configured backend failed to respond at all (non-2xx, connection error,
    timeout). Distinct from a genuine zero-result search; routes should map
    this to 502/503 so callers can tell "the web has nothing" apart from
    "search is broken right now".
    """


async def _resolve_pinned_target(
    url: str,
) -> tuple[httpx.URL, dict[str, str], dict[str, str]]:
    """Resolve *url*'s host and return a request target pinned to a
    validated IP, plus a ``Host`` header and SNI override that preserve
    the original hostname for the TLS handshake and cert check.

    Re-resolves and re-checks right before the actual connection, closing
    most of the window between ``validate_searxng_url`` (config time) and
    the request itself — the window in which a hostname's DNS record
    could change (rebinding) to point at a private target.

    One residual gap remains, and it's narrow: the DNS answer is looked
    up here and then reused for one connection attempt, so a rebind that
    races the resolution itself (rather than happening between config
    time and request time) is not eliminated — httpx has no lower-level
    hook to resolve and connect as one atomic step. That is not a
    regression versus no pinning at all (the prior behavior for every
    request), just a bound on how much of the window this closes.

    No-op (URL unchanged, empty headers/extensions) when the host is
    already a literal IP (static — nothing to re-resolve; already vetted
    at config time), or when ``lm_chat_allow_private_searxng`` is set
    (private/Tailscale targets are explicitly trusted — pinning would
    only add overhead to the admin's own trusted endpoint).

    Fails CLOSED — raises instead of connecting unpinned — when
    resolution returns no address, whether because it genuinely failed
    or because it didn't complete within ``_DNS_RESOLVE_TIMEOUT_SEC``:
    with no resolved address there is nothing to validate, and falling
    back to an unpinned connect would hand the destination decision to
    httpx's own (uncapped, unchecked) resolver — defeating the guard
    right at the moment it matters most (an admin-configured hostname
    whose DNS just became unreachable or slow).

    Raises:
        WebSearchUnavailable: If resolution returns no address (DNS
            failure or timeout), or if a resolved IP is private/loopback
            and not Tailscale-exempt.
    """
    from lmchat.config import get_settings  # noqa: PLC0415

    parsed = httpx.URL(url)
    host = parsed.host

    if get_settings().lm_chat_allow_private_searxng:
        return parsed, {}, {}

    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        # Literal IP: there is no DNS to pin, but still re-validate here
        # (defense-in-depth against a private literal reaching the connect
        # path if config-time validation was ever bypassed). A Tailscale
        # literal is allowed; any other private/loopback/unspecified is
        # refused — mirroring the resolved-IP loop below.
        if not _is_tailscale_ip(host) and _is_private_ip(host):
            log.error(
                "web_search.ssrf_guard.private_literal_blocked",
                url=url,
                host=host,
            )
            raise WebSearchUnavailable(
                f"SearXNG host {host!r} is a private/loopback address; "
                "refusing to connect. Set LM_CHAT_ALLOW_PRIVATE_SEARXNG=1 "
                "to allow."
            )
        return parsed, {}, {}

    ips = await asyncio.to_thread(_resolve_host_ips, host)
    if not ips:
        log.error(
            "web_search.ssrf_guard.resolve_failed",
            url=url,
            host=host,
        )
        raise WebSearchUnavailable(
            f"SearXNG host {host!r} could not be resolved (DNS lookup "
            "failed or timed out) at connect time; refusing to connect "
            "to an unvalidated target."
        )

    for ip in ips:
        if _is_tailscale_ip(ip):
            continue
        if _is_private_ip(ip):
            log.error(
                "web_search.ssrf_guard.rebind_blocked",
                url=url,
                host=host,
                resolved_ip=ip,
            )
            raise WebSearchUnavailable(
                f"SearXNG host {host!r} resolved to a private/loopback "
                f"address ({ip!r}) at connect time; refusing to connect "
                "(possible DNS rebinding). Set "
                "LM_CHAT_ALLOW_PRIVATE_SEARXNG=1 to allow."
            )

    pinned = parsed.copy_with(host=ips[0])
    return pinned, {"Host": host}, {"sni_hostname": host}


class WebSearchService:
    """Provider-agnostic web search service.

    Args:
        provider:      ``"searxng"``, ``"ddg"``, ``"brave"``, or ``"brave_llm"``.
        brave_api_key: Empty by default — a missing key raises
                       ``WebSearchUnavailable`` at search time rather than
                       failing construction, so an admin can switch
                       providers before supplying a key.
    """

    def __init__(
        self,
        *,
        provider: str = "searxng",
        searxng_url: str = "https://searx.be",
        http_client: httpx.AsyncClient | None = None,
        brave_api_key: str = "",
    ) -> None:
        self._provider = provider
        self._searxng_url = searxng_url
        self._brave_api_key = brave_api_key

        # Validate SearXNG URL at construction (raises ValueError for
        # private/loopback IPs unless the escape hatch is set).
        if provider == "searxng":
            validate_searxng_url(searxng_url)

        # follow_redirects=False: SearXNG returns JSON directly; following
        # redirects could expose SSRF via open redirects.
        self._http: httpx.AsyncClient = http_client or httpx.AsyncClient(
            timeout=10.0, follow_redirects=False
        )
        self._owns_http = http_client is None
        # Monotonic timestamp of the last DDG request for rate limiting.
        self._last_ddg_ts: float = 0.0

    # ------------------------------------------------------------------
    # Startup probe
    # ------------------------------------------------------------------

    async def probe(self) -> bool:
        """Check that the configured SearXNG instance responds with JSON.

        On failure, logs ERROR and returns ``False`` — this never blocks
        the route: ``search()`` always falls back to DDG when SearXNG
        errors. Purely diagnostic for startup logging.
        """
        # DDG is keyless (no probe needed) and always-on as a fallback for
        # the searxng provider, so nothing below needs to gate the caller.
        if self._provider != "searxng":
            return True

        try:
            url, extra_headers, extensions = await _resolve_pinned_target(
                self._searxng_url
            )
            resp = await self._http.get(
                url,
                params={"q": "test", "format": "json"},
                headers=extra_headers,
                extensions=extensions,
            )
            if resp.status_code == 200:
                body = resp.json()
                if isinstance(body, dict):
                    log.info("web_search.searxng.probe_ok", url=self._searxng_url)
                    return True
            log.error(
                "web_search.searxng.probe_failed",
                url=self._searxng_url,
                status=resp.status_code,
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "web_search.searxng.probe_error",
                url=self._searxng_url,
                error=str(exc),
            )
        return False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def search(self, query: str, top_n: int = 5) -> list[SearchResult]:
        """Search the web and return at most *top_n* results.

        SearXNG falls back to DDG (one attempt) on 5xx or fewer than
        ``_SEARXNG_SPARSE_RESULT_THRESHOLD`` results. Both Brave sub-types
        are standalone — never chain to SearXNG/DDG; they raise
        ``WebSearchUnavailable`` instead.

        Returns:
            Empty ONLY on a genuine zero-result search from a reachable
            backend — never used to paper over a transport failure.

        Raises:
            WebSearchUnavailable: if every configured backend failed to
                respond, or provider is brave/brave_llm with no API key.
        """
        if self._provider == "ddg":
            return await self._ddg_search(query, top_n)

        if self._provider in ("brave", "brave_llm"):
            if not self._brave_api_key:
                raise WebSearchUnavailable(
                    "Brave search requires an API key — set LM_CHAT_BRAVE_API_KEY."
                )
            if self._provider == "brave_llm":
                return await self._brave_llm_search(query, top_n)
            return await self._brave_search(query, top_n)

        # SearXNG path — DDG fallback on error or sparse results (checked
        # against the TRUE uncapped count, see threshold constant above).
        searxng_results: list[SearchResult] | None = None
        try:
            results = await self._searxng_search(query)
            if len(results) >= _SEARXNG_SPARSE_RESULT_THRESHOLD:
                return results[:top_n]
            # Sparse but not an error — try DDG to enrich, keeping these
            # real results in hand in case DDG is unreachable.
            searxng_results = results
            log.info(
                "web_search.searxng.sparse_fallback_to_ddg",
                query_len=len(query),
                result_count=len(results),
            )
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                log.warning(
                    "web_search.searxng.5xx_fallback_to_ddg",
                    status=exc.response.status_code,
                )
            else:
                log.warning(
                    "web_search.searxng.error_fallback_to_ddg",
                    status=exc.response.status_code,
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("web_search.searxng.error_fallback_to_ddg", error=str(exc))

        try:
            return await self._ddg_search(query, top_n)
        except WebSearchUnavailable:
            if searxng_results is not None:
                # SearXNG gave genuine (if sparse) data; a DDG fallback
                # failure doesn't erase that — not "the web has nothing".
                log.warning(
                    "web_search.ddg_fallback_failed_using_sparse_searxng",
                    query_len=len(query),
                )
                return searxng_results[:top_n]
            log.error("web_search.both_backends_unavailable", query_len=len(query))
            raise

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    async def _searxng_search(self, query: str) -> list[SearchResult]:
        """Query SearXNG and return the full (uncapped) parsed result list.

        Callers cap to ``top_n`` themselves; the uncapped count is what the
        sparse-result fallback threshold must be evaluated against (a small
        ``top_n`` must not itself look like a sparse SearXNG response).
        """
        url, extra_headers, extensions = await _resolve_pinned_target(
            self._searxng_url
        )
        resp = await self._http.get(
            url,
            params={"q": query, "format": "json"},
            headers=extra_headers,
            extensions=extensions,
        )
        resp.raise_for_status()
        body = resp.json()
        return _parse_searxng(body)

    async def _ddg_search(self, query: str, top_n: int) -> list[SearchResult]:
        """Query DuckDuckGo via the ``ddgs`` library (keyless default).

        ``DDGS().text()`` is synchronous; runs in a worker thread via
        ``asyncio.to_thread``.

        Returns:
            Empty ONLY when ddgs was reachable and genuinely found zero
            results.

        Raises:
            WebSearchUnavailable: on rate limit, timeout, or any other
                ddgs failure — a transport failure, not an empty result.
        """
        # Enforce 1 req/2s, a courtesy on top of ddgs's own anti-bot handling.
        now = asyncio.get_event_loop().time()
        wait_s = _DDG_MIN_INTERVAL_SEC - (now - self._last_ddg_ts)
        if wait_s > 0:
            await asyncio.sleep(wait_s)

        try:
            raw_results = await asyncio.wait_for(
                asyncio.to_thread(
                    lambda: DDGS(timeout=_DDG_HTTP_TIMEOUT_SEC).text(
                        query, max_results=top_n
                    )
                ),
                timeout=_DDG_CALL_TIMEOUT_SEC,
            )
        except TimeoutError as exc:
            # wait_for cancels the awaiting coroutine; the orphaned worker
            # thread finishes on its own. Fail soft so the loop terminates.
            log.warning("web_search.ddg.timeout", timeout_s=_DDG_CALL_TIMEOUT_SEC)
            raise WebSearchUnavailable("DuckDuckGo search timed out") from exc
        except DDGSException as exc:
            # ddgs (v9.14.4) raises this SAME class for "zero results" and
            # for a real failure — distinguished only by message text.
            # Rate limit/timeout subclasses have different messages and
            # correctly fall through to WebSearchUnavailable below.
            if str(exc) == "No results found.":
                self._last_ddg_ts = asyncio.get_event_loop().time()
                return []
            log.warning("web_search.ddg.request_error", error=str(exc))
            raise WebSearchUnavailable("DuckDuckGo search request failed") from exc
        except Exception as exc:  # noqa: BLE001
            log.warning("web_search.ddg.request_error", error=str(exc))
            raise WebSearchUnavailable("DuckDuckGo search request failed") from exc

        self._last_ddg_ts = asyncio.get_event_loop().time()
        return _parse_ddgs_results(raw_results)[:top_n]


    async def _brave_search(self, query: str, top_n: int) -> list[SearchResult]:
        """Query the Brave Search API.

        Returns:
            Empty ONLY when Brave was reachable and genuinely returned
            zero parseable results.

        Raises:
            WebSearchUnavailable: on request failure or a non-200 status
                (401 = bad key, 429 = rate limit, 422 = bad params) — a
                transport failure, not an empty result.
        """
        count = max(1, min(top_n, _BRAVE_MAX_COUNT))
        try:
            resp = await self._http.get(
                _BRAVE_SEARCH_URL,
                params={"q": query, "count": count, "safesearch": "moderate"},
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": self._brave_api_key,
                },
            )
            if resp.status_code != 200:
                log.warning("web_search.brave.non_200", status=resp.status_code)
                raise WebSearchUnavailable(
                    f"Brave search returned status {resp.status_code}"
                )
            return _parse_brave(resp.json())[:top_n]
        except WebSearchUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("web_search.brave.request_error", error=str(exc))
            raise WebSearchUnavailable("Brave search request failed") from exc


    async def _brave_llm_search(self, query: str, top_n: int) -> list[SearchResult]:
        """Query the Brave Search — LLM Context API (``/res/v1/llm/context``).

        Same auth/transport-failure contract as ``_brave_search``; only the
        endpoint, params, and response shape differ.

        Returns:
            Empty ONLY when Brave was reachable and genuinely returned
            zero parseable results.

        Raises:
            WebSearchUnavailable: on request failure or a non-200 status —
                a transport failure, not an empty result.
        """
        # `q` is capped at 50 words / 400 chars server-side — clamp before
        # sending so an over-long query fails soft instead of 422ing.
        clamped_query = _clamp_query_words(query, _BRAVE_LLM_CONTEXT_MAX_QUERY_WORDS)
        count = max(1, min(top_n, _BRAVE_LLM_CONTEXT_MAX_URLS))
        try:
            resp = await self._http.get(
                _BRAVE_LLM_CONTEXT_URL,
                params={
                    "q": clamped_query,
                    "count": count,
                    "maximum_number_of_urls": count,
                },
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "gzip",
                    "X-Subscription-Token": self._brave_api_key,
                },
            )
            if resp.status_code != 200:
                log.warning("web_search.brave_llm.non_200", status=resp.status_code)
                raise WebSearchUnavailable(
                    f"Brave LLM Context search returned status {resp.status_code}"
                )
            return _parse_brave_llm_context(resp.json())[:top_n]
        except WebSearchUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("web_search.brave_llm.request_error", error=str(exc))
            raise WebSearchUnavailable("Brave LLM Context search request failed") from exc

    async def aclose(self) -> None:
        """Close the HTTP client if owned by this instance."""
        if self._owns_http:
            await self._http.aclose()

    def reconfigure(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        provider: str | None = None,
        searxng_url: str | None = None,
    ) -> None:
        """Rebind the shared HTTP client (and optionally provider/url) after
        a singleton rewire or admin settings change.

        Called by ``rewire_singletons`` (new LM Studio URL/key — without
        rebinding, ``_http`` would point at an old, eventually-closed
        client and raise ``httpx.RuntimeError``) and by
        ``patch_app_settings`` (provider/searxng_url changes take effect
        without a restart).

        Raises:
            ValueError: If *searxng_url* fails SSRF validation.
        """
        if http_client is not None and not self._owns_http:
            self._http = http_client

        if provider is not None:
            self._provider = provider

        if searxng_url is not None:
            validate_searxng_url(searxng_url)
            self._searxng_url = searxng_url
