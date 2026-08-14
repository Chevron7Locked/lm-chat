# SPDX-License-Identifier: Apache-2.0
"""Tests for web_search_service — SearXNG happy path, 5xx → DDG (ddgs-backed)
fallback, P12c SSRF validator, and the standalone Brave providers: Brave
Search (``brave``) and Brave Search — LLM Context (``brave_llm``); both
keyed, no chaining to SearXNG/DDG.

DDG uses the ``ddgs`` library (``DDGS().text()``); tests patch
``lmchat.services.web_search_service.DDGS`` rather than mocking an httpx
HTML response — DDG no longer makes an httpx call at all.

Per P8b.2 brief §Item 9 (Tests) and P12c.
"""
from __future__ import annotations

import json
from collections.abc import Generator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from ddgs.exceptions import DDGSException

from lmchat.services.web_search_service import (
    SearchResult,
    WebSearchService,
    WebSearchUnavailable,
    _is_private_ip,
    _parse_brave,
    _parse_brave_llm_context,
    _parse_ddgs_results,
    _parse_searxng,
    _resolve_host_ips,
    _resolve_pinned_target,
    validate_searxng_url,
)


def _patch_ddgs_text(
    *, return_value: list[dict] | None = None, side_effect: BaseException | None = None
):  # noqa: ANN201
    """Patch ``DDGS().text()`` for one test.

    ``DDGS()`` is constructed fresh inside ``_ddg_search``; patching the
    class (so its ``return_value`` is the same mock instance every call)
    lets us control what ``.text(...)`` returns/raises without touching
    httpx at all.
    """
    mock_instance = MagicMock()
    if side_effect is not None:
        mock_instance.text = MagicMock(side_effect=side_effect)
    else:
        mock_instance.text = MagicMock(return_value=return_value or [])
    return patch("lmchat.services.web_search_service.DDGS", return_value=mock_instance)


# P12c: existing tests use localhost URLs.  Allow private SearXNG for the whole
# module so those tests keep working; the SSRF-specific tests below clear it.
@pytest.fixture(autouse=True)
def _allow_private_searxng(monkeypatch: pytest.MonkeyPatch) -> Generator[None]:
    monkeypatch.setenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", "1")
    yield

# ─── Unit tests for parsers ───────────────────────────────────────────────────


def test_parse_searxng_happy_path() -> None:
    """SearXNG JSON response parses into SearchResult objects."""
    body = {
        "results": [
            {"title": "Alpha", "url": "https://a.example.com", "content": "Alpha snippet"},
            {"title": "Beta", "url": "https://b.example.com", "content": "Beta snippet"},
        ]
    }
    results = _parse_searxng(body)
    assert len(results) == 2
    assert results[0].title == "Alpha"
    assert results[0].url == "https://a.example.com"
    assert results[0].snippet == "Alpha snippet"


def test_parse_searxng_missing_url_skipped() -> None:
    """Items with no URL are skipped."""
    body = {"results": [{"title": "No URL", "url": "", "content": "x"}]}
    results = _parse_searxng(body)
    assert results == []


def test_parse_searxng_unexpected_shape_returns_empty(caplog) -> None:  # type: ignore[no-untyped-def]
    """Unexpected body shape logs a warning and returns empty list."""
    body: dict = {"unexpected_key": True}
    with patch("lmchat.services.web_search_service.log") as mock_log:
        results = _parse_searxng(body)
    assert results == []
    mock_log.warning.assert_called_once()


def test_parse_ddgs_results_happy_path() -> None:
    """ddgs' title/href/body dicts parse into SearchResult objects."""
    raw_results = [
        {"title": "Example Title", "href": "https://example.com", "body": "Example snippet text."},
        {"title": "Second", "href": "https://second.example.com", "body": "Second snippet."},
    ]
    results = _parse_ddgs_results(raw_results)
    assert len(results) == 2
    assert results[0].url == "https://example.com"
    assert results[0].title == "Example Title"
    assert results[0].snippet == "Example snippet text."


def test_parse_ddgs_results_empty_list_returns_empty() -> None:
    """An empty results list yields an empty result list."""
    results = _parse_ddgs_results([])
    assert results == []


def test_parse_ddgs_results_missing_url_skipped() -> None:
    """Items with no href are skipped."""
    raw_results = [{"title": "No URL", "href": "", "body": "x"}]
    results = _parse_ddgs_results(raw_results)
    assert results == []


def test_parse_brave_happy_path_strips_html() -> None:
    """Brave JSON response parses into SearchResult, with HTML tags stripped."""
    body = {
        "web": {
            "results": [
                {
                    "title": "<strong>Alpha</strong> result",
                    "url": "https://a.example.com",
                    "description": "Alpha <strong>snippet</strong> text",
                },
                {
                    "title": "Beta",
                    "url": "https://b.example.com",
                    "description": "Beta snippet",
                },
            ]
        }
    }
    results = _parse_brave(body)
    assert len(results) == 2
    assert results[0].title == "Alpha result"
    assert results[0].url == "https://a.example.com"
    assert results[0].snippet == "Alpha snippet text"
    assert results[1].title == "Beta"


def test_parse_brave_missing_url_skipped() -> None:
    """Items with no URL are skipped."""
    body = {"web": {"results": [{"title": "No URL", "url": "", "description": "x"}]}}
    results = _parse_brave(body)
    assert results == []


def test_parse_brave_unexpected_shape_returns_empty(caplog) -> None:  # type: ignore[no-untyped-def]
    """Missing/malformed 'web' key logs a warning and returns empty list."""
    body: dict = {"unexpected_key": True}
    with patch("lmchat.services.web_search_service.log") as mock_log:
        results = _parse_brave(body)
    assert results == []
    mock_log.warning.assert_called_once()


def test_parse_brave_llm_context_happy_path_joins_snippets() -> None:
    """Brave LLM Context JSON parses grounding.generic into SearchResult,
    HTML-stripping and joining the per-item snippets list with ' … '."""
    body = {
        "grounding": {
            "generic": [
                {
                    "url": "https://a.example.com",
                    "title": "<strong>Alpha</strong>",
                    "snippets": ["First <strong>chunk</strong>.", "Second chunk."],
                },
                {
                    "url": "https://b.example.com",
                    "title": "Beta",
                    "snippets": ["Only chunk."],
                },
            ],
            "poi": None,
            "map": [],
        },
        "sources": {},
    }
    results = _parse_brave_llm_context(body)
    assert len(results) == 2
    assert results[0].title == "Alpha"
    assert results[0].url == "https://a.example.com"
    assert results[0].snippet == "First chunk. … Second chunk."
    assert results[1].snippet == "Only chunk."


def test_parse_brave_llm_context_missing_url_skipped() -> None:
    """Items with no URL are skipped."""
    body = {"grounding": {"generic": [{"title": "No URL", "url": "", "snippets": ["x"]}]}}
    results = _parse_brave_llm_context(body)
    assert results == []


def test_parse_brave_llm_context_unexpected_shape_returns_empty(caplog) -> None:  # type: ignore[no-untyped-def]
    """Missing/malformed 'grounding' key logs a warning and returns empty list."""
    body: dict = {"unexpected_key": True}
    with patch("lmchat.services.web_search_service.log") as mock_log:
        results = _parse_brave_llm_context(body)
    assert results == []
    mock_log.warning.assert_called_once()


# ─── Service tests ────────────────────────────────────────────────────────────


def _make_json_response(body: dict, status_code: int = 200) -> httpx.Response:  # type: ignore[type-arg]
    return httpx.Response(
        status_code=status_code,
        content=json.dumps(body).encode(),
        headers={"content-type": "application/json"},
        request=httpx.Request("GET", "http://test"),
    )


@pytest.mark.asyncio
async def test_searxng_happy_path() -> None:
    """SearXNG search returns results when the instance responds correctly."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(
        return_value=_make_json_response(
            {
                "results": [
                    {
                        "title": f"R{i}",
                        "url": f"https://r{i}.example.com",
                        "content": f"snippet {i}",
                    }
                    for i in range(5)
                ]
            }
        )
    )
    svc = WebSearchService(
        provider="searxng",
        searxng_url="http://localhost:8888/search",
        http_client=mock_http,
    )
    results = await svc.search("test query")
    assert len(results) == 5
    assert all(isinstance(r, SearchResult) for r in results)


@pytest.mark.asyncio
async def test_searxng_5xx_falls_back_to_ddg() -> None:
    """SearXNG 5xx response triggers DDG (ddgs-backed) fallback."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "Server error",
            request=httpx.Request("GET", "http://localhost:8888/search"),
            response=httpx.Response(
                500, request=httpx.Request("GET", "http://localhost:8888/search")
            ),
        )
    )
    ddg_results = [
        {"title": f"DDG {i}", "href": f"https://ddg{i}.example.com", "body": f"Snippet {i}"}
        for i in range(4)
    ]
    svc = WebSearchService(
        provider="searxng",
        searxng_url="http://localhost:8888/search",
        http_client=mock_http,
    )
    svc._last_ddg_ts = 0.0
    with _patch_ddgs_text(return_value=ddg_results):
        results = await svc.search("test query")
    # Should have fallen back to DDG results.
    mock_http.get.assert_called_once()
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_searxng_sparse_results_fall_back_to_ddg() -> None:
    """Fewer than 3 SearXNG results triggers DDG (ddgs-backed) fallback."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(
        return_value=_make_json_response(
            {"results": [{"title": "Only one", "url": "https://one.example.com", "content": "x"}]}
        )
    )
    ddg_results = [
        {"title": "DDG Result", "href": "https://ddg.example.com", "body": "DDG Snippet"}
    ] * 4  # 4 results to satisfy top_n
    svc = WebSearchService(
        provider="searxng",
        searxng_url="http://localhost:8888/search",
        http_client=mock_http,
    )
    svc._last_ddg_ts = 0.0
    with _patch_ddgs_text(return_value=ddg_results) as mock_ddgs_cls:
        await svc.search("test query")
    # Fell back to DDG after sparse SearXNG results.
    mock_http.get.assert_called_once()
    mock_ddgs_cls.return_value.text.assert_called_once()


@pytest.mark.asyncio
async def test_searxng_small_top_n_does_not_trigger_spurious_ddg_fallback() -> None:
    """A small ``top_n`` must not itself look like a sparse SearXNG result.

    The sparse-result threshold is evaluated against the TRUE (pre-cap)
    SearXNG result count. SearXNG returning plenty of results (>= the
    sparse threshold) with a caller-supplied ``top_n`` below that count
    must return the capped SearXNG results directly — no DDG fallback.
    """
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(
        return_value=_make_json_response(
            {
                "results": [
                    {
                        "title": f"R{i}",
                        "url": f"https://r{i}.example.com",
                        "content": f"snippet {i}",
                    }
                    for i in range(5)
                ]
            }
        )
    )
    svc = WebSearchService(
        provider="searxng",
        searxng_url="http://localhost:8888/search",
        http_client=mock_http,
    )
    svc._last_ddg_ts = 0.0
    with _patch_ddgs_text(return_value=[]) as mock_ddgs_cls:
        results = await svc.search("test query", top_n=1)
    # SearXNG genuinely had 5 (>= threshold) results — no DDG fallback,
    # even though top_n=1 is below the sparse-result threshold.
    mock_ddgs_cls.return_value.text.assert_not_called()
    assert len(results) == 1
    assert results[0].url == "https://r0.example.com"


@pytest.mark.asyncio
async def test_ddg_provider_direct() -> None:
    """Provider=ddg calls ddgs directly without touching SearXNG/httpx."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    ddg_results = [
        {"title": "DDG Direct", "href": "https://ddg.example.com", "body": "DDG Snippet"}
    ]
    svc = WebSearchService(provider="ddg", http_client=mock_http)
    # Disable rate limit for tests.
    svc._last_ddg_ts = 0.0
    with _patch_ddgs_text(return_value=ddg_results):
        results = await svc.search("test query")
    assert len(results) == 1
    assert results[0].url == "https://ddg.example.com"
    # provider=ddg never touches httpx at all.
    mock_http.get.assert_not_called()


# ─── mcp-tools-7: failure vs. genuine-empty semantics ────────────────────────
#
# search() must let a caller distinguish "the web genuinely has nothing" from
# "search transport is broken" — the latter must raise WebSearchUnavailable,
# never silently degrade to [].


@pytest.mark.asyncio
async def test_searxng_and_ddg_both_fail_raises_unavailable() -> None:
    """Both backends unreachable → WebSearchUnavailable, never a silent []."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(side_effect=httpx.ConnectError("searxng connection refused"))
    svc = WebSearchService(
        provider="searxng",
        searxng_url="http://localhost:8888/search",
        http_client=mock_http,
    )
    svc._last_ddg_ts = 0.0

    with _patch_ddgs_text(side_effect=DDGSException("ddg connection refused")):
        with pytest.raises(WebSearchUnavailable):
            await svc.search("test query")


@pytest.mark.asyncio
async def test_searxng_non_5xx_error_still_falls_back_to_ddg() -> None:
    """A non-5xx SearXNG error (e.g. 404) is a RECOVERABLE case — it must
    still try DDG before giving up. (Previously this short-circuited straight
    to [] without ever attempting the DDG fallback.)
    """
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(
        side_effect=httpx.HTTPStatusError(
            "Not Found",
            request=httpx.Request("GET", "http://localhost:8888/search"),
            response=httpx.Response(
                404, request=httpx.Request("GET", "http://localhost:8888/search")
            ),
        )
    )
    ddg_results = [
        {"title": f"DDG {i}", "href": f"https://ddg{i}.example.com", "body": f"Snippet {i}"}
        for i in range(4)
    ]
    svc = WebSearchService(
        provider="searxng",
        searxng_url="http://localhost:8888/search",
        http_client=mock_http,
    )
    svc._last_ddg_ts = 0.0

    with _patch_ddgs_text(return_value=ddg_results) as mock_ddgs_cls:
        results = await svc.search("test query")
    mock_ddgs_cls.return_value.text.assert_called_once()
    assert len(results) >= 1


@pytest.mark.asyncio
async def test_both_backends_reachable_zero_hits_is_genuine_empty() -> None:
    """SearXNG and DDG both reachable but return zero hits → [] with NO error.

    This is the control case: a real empty result must stay empty, not get
    reclassified as a failure just because the fallback was also exercised.
    ddgs itself raises ``DDGSException("No results found.")`` for a genuine
    zero-result search (see ``ddgs/ddgs.py::_search_sync``, installed
    version 9.14.4) — ``_ddg_search`` must translate that specific message
    into an empty list, not a ``WebSearchUnavailable``.
    """
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(return_value=_make_json_response({"results": []}))
    svc = WebSearchService(
        provider="searxng",
        searxng_url="http://localhost:8888/search",
        http_client=mock_http,
    )
    svc._last_ddg_ts = 0.0

    with _patch_ddgs_text(side_effect=DDGSException("No results found.")):
        results = await svc.search("test query")
    assert results == []


@pytest.mark.asyncio
async def test_searxng_sparse_but_real_survives_ddg_fallback_failure() -> None:
    """SearXNG returns genuine (if sparse) results; DDG fallback then fails.

    The sparse-but-real SearXNG data must NOT be discarded in favor of an
    error — we have actual results, just not enough to skip the enrichment
    attempt.
    """
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(
        return_value=_make_json_response(
            {"results": [{"title": "Only one", "url": "https://one.example.com", "content": "x"}]}
        )
    )
    svc = WebSearchService(
        provider="searxng",
        searxng_url="http://localhost:8888/search",
        http_client=mock_http,
    )
    svc._last_ddg_ts = 0.0

    with _patch_ddgs_text(side_effect=DDGSException("ddg connection refused")):
        results = await svc.search("test query")
    assert len(results) == 1
    assert results[0].url == "https://one.example.com"


@pytest.mark.asyncio
async def test_ddg_provider_transport_failure_raises_unavailable() -> None:
    """provider=ddg with a ddgs failure raises, rather than returning []."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    svc = WebSearchService(provider="ddg", http_client=mock_http)
    svc._last_ddg_ts = 0.0

    with _patch_ddgs_text(side_effect=DDGSException("connection refused")):
        with pytest.raises(WebSearchUnavailable):
            await svc.search("test query")


@pytest.mark.asyncio
async def test_ddg_provider_ratelimit_raises_unavailable() -> None:
    """provider=ddg with a ddgs RatelimitException raises, not an empty list."""
    from ddgs.exceptions import RatelimitException

    mock_http = MagicMock(spec=httpx.AsyncClient)
    svc = WebSearchService(provider="ddg", http_client=mock_http)
    svc._last_ddg_ts = 0.0

    with _patch_ddgs_text(side_effect=RatelimitException("rate limited")):
        with pytest.raises(WebSearchUnavailable):
            await svc.search("test query")


# ─── Brave provider tests ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_brave_provider_direct_with_key() -> None:
    """provider=brave with a key routes to Brave and returns results."""
    body = {
        "web": {
            "results": [
                {"title": f"R{i}", "url": f"https://r{i}.example.com", "description": f"s{i}"}
                for i in range(3)
            ]
        }
    }
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(return_value=_make_json_response(body))
    svc = WebSearchService(
        provider="brave", http_client=mock_http, brave_api_key="test-brave-key"
    )
    results = await svc.search("test query")
    assert len(results) == 3
    assert all(isinstance(r, SearchResult) for r in results)

    # The subscription token header must be present on the outgoing request.
    _, kwargs = mock_http.get.call_args
    assert kwargs["headers"]["X-Subscription-Token"] == "test-brave-key"


@pytest.mark.asyncio
async def test_brave_provider_no_key_raises_unavailable() -> None:
    """provider=brave with NO key raises WebSearchUnavailable with a clear message."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock()
    svc = WebSearchService(provider="brave", http_client=mock_http)

    with pytest.raises(WebSearchUnavailable, match="LM_CHAT_BRAVE_API_KEY"):
        await svc.search("test query")
    mock_http.get.assert_not_called()


@pytest.mark.asyncio
async def test_brave_provider_non_200_raises_unavailable() -> None:
    """A Brave non-200 response (e.g. 401 invalid key) raises, not an empty list."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(return_value=_make_json_response({}, status_code=401))
    svc = WebSearchService(
        provider="brave", http_client=mock_http, brave_api_key="bad-key"
    )

    with pytest.raises(WebSearchUnavailable):
        await svc.search("test query")


@pytest.mark.asyncio
async def test_brave_provider_transport_failure_raises_unavailable() -> None:
    """provider=brave with a connection error raises, rather than returning []."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    svc = WebSearchService(
        provider="brave", http_client=mock_http, brave_api_key="test-brave-key"
    )

    with pytest.raises(WebSearchUnavailable):
        await svc.search("test query")


@pytest.mark.asyncio
async def test_brave_provider_does_not_chain_to_searxng_or_ddg() -> None:
    """Brave is standalone — a Brave failure must not fall back to SearXNG/DDG."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(return_value=_make_json_response({}, status_code=500))
    svc = WebSearchService(
        provider="brave", http_client=mock_http, brave_api_key="test-brave-key"
    )

    with pytest.raises(WebSearchUnavailable):
        await svc.search("test query")
    # Exactly one call — no fallback attempt to SearXNG or DDG.
    assert mock_http.get.call_count == 1


# ─── Brave LLM Context provider tests ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_brave_llm_provider_direct_with_key() -> None:
    """provider=brave_llm with a key routes to the LLM Context endpoint."""
    body = {
        "grounding": {
            "generic": [
                {
                    "title": f"R{i}",
                    "url": f"https://r{i}.example.com",
                    "snippets": [f"chunk {i}"],
                }
                for i in range(3)
            ]
        }
    }
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(return_value=_make_json_response(body))
    svc = WebSearchService(
        provider="brave_llm", http_client=mock_http, brave_api_key="test-brave-key"
    )
    results = await svc.search("test query", top_n=3)
    assert len(results) == 3
    assert all(isinstance(r, SearchResult) for r in results)

    # Hits the LLM Context endpoint (not the standard web/search one), with
    # the subscription token header and both count params (each set from
    # top_n) present.
    args, kwargs = mock_http.get.call_args
    assert "llm/context" in args[0]
    assert kwargs["headers"]["X-Subscription-Token"] == "test-brave-key"
    assert kwargs["params"]["count"] == 3
    assert kwargs["params"]["maximum_number_of_urls"] == 3


@pytest.mark.asyncio
async def test_brave_llm_search_clamps_query_to_50_words() -> None:
    """A query over 50 words is clamped before being sent — avoids Brave's
    422 on queries over 50 words / 400 chars for this endpoint."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(return_value=_make_json_response({"grounding": {"generic": []}}))
    svc = WebSearchService(
        provider="brave_llm", http_client=mock_http, brave_api_key="test-brave-key"
    )
    long_query = " ".join(f"word{i}" for i in range(60))  # 60 words

    await svc.search(long_query)

    _, kwargs = mock_http.get.call_args
    sent_query = kwargs["params"]["q"]
    assert len(sent_query.split()) == 50


@pytest.mark.asyncio
async def test_brave_llm_provider_no_key_raises_unavailable() -> None:
    """provider=brave_llm with NO key raises WebSearchUnavailable with a clear message."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock()
    svc = WebSearchService(provider="brave_llm", http_client=mock_http)

    with pytest.raises(WebSearchUnavailable, match="LM_CHAT_BRAVE_API_KEY"):
        await svc.search("test query")
    mock_http.get.assert_not_called()


@pytest.mark.asyncio
async def test_brave_llm_provider_non_200_raises_unavailable() -> None:
    """A Brave LLM Context non-200 response raises, not an empty list."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(return_value=_make_json_response({}, status_code=401))
    svc = WebSearchService(
        provider="brave_llm", http_client=mock_http, brave_api_key="bad-key"
    )

    with pytest.raises(WebSearchUnavailable):
        await svc.search("test query")


@pytest.mark.asyncio
async def test_brave_llm_provider_transport_failure_raises_unavailable() -> None:
    """provider=brave_llm with a connection error raises, rather than returning []."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    svc = WebSearchService(
        provider="brave_llm", http_client=mock_http, brave_api_key="test-brave-key"
    )

    with pytest.raises(WebSearchUnavailable):
        await svc.search("test query")


@pytest.mark.asyncio
async def test_brave_llm_provider_does_not_chain_to_searxng_or_ddg() -> None:
    """brave_llm is standalone — a failure must not fall back to SearXNG/DDG."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(return_value=_make_json_response({}, status_code=500))
    svc = WebSearchService(
        provider="brave_llm", http_client=mock_http, brave_api_key="test-brave-key"
    )

    with pytest.raises(WebSearchUnavailable):
        await svc.search("test query")
    # Exactly one call — no fallback attempt to SearXNG or DDG.
    assert mock_http.get.call_count == 1


def test_reconfigure_preserves_brave_api_key() -> None:
    """reconfigure() (provider/searxng_url rebind) does not clear the Brave key."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    svc = WebSearchService(
        provider="brave", http_client=mock_http, brave_api_key="test-brave-key"
    )
    svc.reconfigure(provider="ddg")
    assert svc._brave_api_key == "test-brave-key"


@pytest.mark.asyncio
async def test_probe_returns_true_on_searxng_success() -> None:
    """probe() returns True when the SearXNG instance responds with JSON."""
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(
        return_value=_make_json_response({"results": []})
    )
    svc = WebSearchService(
        provider="searxng",
        searxng_url="http://localhost:8888/search",
        http_client=mock_http,
    )
    ok = await svc.probe()
    assert ok is True


@pytest.mark.asyncio
async def test_probe_returns_false_on_searxng_failure() -> None:
    """probe()'s return value is purely diagnostic (startup logging): a
    SearXNG connection error is reported via ``False``, but has no other
    effect — ``search()`` always falls back to the keyless DDG backend
    regardless of what probe() returns.
    """
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
    svc = WebSearchService(
        provider="searxng",
        searxng_url="http://localhost:8888/search",
        http_client=mock_http,
    )
    ok = await svc.probe()
    assert ok is False


def test_available_property_not_reintroduced() -> None:
    """Guards FU-G4 #6: a failed SearXNG probe no longer flips any
    availability flag, so the route's old ``if not svc.available: 503``
    branch was unreachable dead code and was removed along with the
    ``available`` property. If this property (or an ``_available``
    attribute) reappears, some caller is probably resurrecting that dead
    503 path — see ``lmchat.routes.web_search.web_search``, which now maps
    every search failure to 502 instead.
    """
    mock_http = MagicMock(spec=httpx.AsyncClient)
    svc = WebSearchService(provider="ddg", http_client=mock_http)
    assert not hasattr(WebSearchService, "available")
    assert not hasattr(svc, "_available")


# ---------------------------------------------------------------------------
# P12c — SSRF URL validator tests
# ---------------------------------------------------------------------------


def test_ssrf_validator_rejects_loopback_ipv4(monkeypatch: pytest.MonkeyPatch) -> None:
    """validate_searxng_url raises ValueError for 127.x without escape hatch."""
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    with pytest.raises(ValueError, match="private/loopback"):
        validate_searxng_url("http://127.0.0.1:8888/search")


def test_ssrf_validator_rejects_loopback_ipv6(monkeypatch: pytest.MonkeyPatch) -> None:
    """validate_searxng_url raises ValueError for ::1 without escape hatch."""
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    with pytest.raises(ValueError, match="private/loopback"):
        validate_searxng_url("http://[::1]:8888/search")


def test_ssrf_validator_allows_tailscale_without_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Tailscale CGNAT (100.64.0.0/10) host is allowed with NO escape hatch.

    The admin's tailnet is trusted; self-hosted SearXNG on it must work
    without LM_CHAT_ALLOW_PRIVATE_SEARXNG. Loopback/RFC-1918 stay blocked.
    """
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    # Must not raise.
    validate_searxng_url("http://100.64.0.2:7980/search")
    validate_searxng_url("http://100.64.0.1:8888/search")  # range boundary


def test_ssrf_validator_rejects_rfc1918_10(monkeypatch: pytest.MonkeyPatch) -> None:
    """validate_searxng_url raises ValueError for 10.x without escape hatch."""
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    with pytest.raises(ValueError, match="private/loopback"):
        validate_searxng_url("http://10.0.0.5:8888/search")


def test_ssrf_validator_rejects_rfc1918_192_168(monkeypatch: pytest.MonkeyPatch) -> None:
    """validate_searxng_url raises ValueError for 192.168.x without escape hatch."""
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    with pytest.raises(ValueError, match="private/loopback"):
        validate_searxng_url("http://192.168.1.100:8888/search")


def test_ssrf_validator_allows_public_ip(monkeypatch: pytest.MonkeyPatch) -> None:
    """validate_searxng_url passes for a public IP."""
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    # Should not raise — 1.1.1.1 is a public IP.
    validate_searxng_url("https://1.1.1.1/search")


def test_ssrf_validator_allows_public_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    """validate_searxng_url passes for a hostname that resolves to a public IP.

    DNS resolution is mocked — no real network lookup.
    """
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    with patch(
        "lmchat.services.web_search_service._resolve_host_ips",
        return_value=["1.1.1.1"],
    ):
        validate_searxng_url("https://searx.be/search")


def test_ssrf_validator_rejects_hostname_resolving_to_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostname that RESOLVES to a private/loopback IP is rejected without
    the escape hatch — closes the gap where only literal IPs were checked.
    DNS resolution is mocked — no real network lookup.
    """
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    with patch(
        "lmchat.services.web_search_service._resolve_host_ips",
        return_value=["10.0.0.5"],
    ):
        with pytest.raises(ValueError, match="resolves to a"):
            validate_searxng_url("http://searxng.internal.example/search")


def test_ssrf_validator_allows_hostname_resolving_to_private_ip_with_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Escape hatch still allows a hostname that resolves to a private IP —
    the toggle must keep working for hostnames, not just literal IPs.
    """
    monkeypatch.setenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", "1")
    with patch(
        "lmchat.services.web_search_service._resolve_host_ips",
        return_value=["10.0.0.5"],
    ) as mock_resolve:
        # Should not raise — escape hatch short-circuits before resolution.
        validate_searxng_url("http://searxng.internal.example/search")
    mock_resolve.assert_not_called()


def test_ssrf_validator_allows_hostname_resolving_to_tailscale_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostname resolving to a Tailscale CGNAT IP is allowed with no
    escape hatch, same as a literal Tailscale IP.
    """
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    with patch(
        "lmchat.services.web_search_service._resolve_host_ips",
        return_value=["100.84.2.118"],
    ):
        validate_searxng_url("http://searxng.tailnet.example/search")


def test_resolve_host_ips_returns_empty_for_literal_ip() -> None:
    """_resolve_host_ips is a no-op for a host that's already a literal IP —
    nothing to resolve, and it must never attempt a DNS lookup for one.
    """
    assert _resolve_host_ips("127.0.0.1") == []
    assert _resolve_host_ips("::1") == []


def test_resolve_host_ips_dedupes_resolved_addresses() -> None:
    """_resolve_host_ips extracts and dedupes IP strings from getaddrinfo,
    preserving first-seen order.
    """
    fake_infos = [
        (2, 1, 6, "", ("203.0.113.1", 0)),
        (2, 1, 6, "", ("203.0.113.1", 0)),  # duplicate
        (10, 1, 6, "", ("2001:db8::1", 0, 0, 0)),
    ]
    with patch(
        "lmchat.services.web_search_service.socket.getaddrinfo",
        return_value=fake_infos,
    ):
        assert _resolve_host_ips("searx.example") == [
            "203.0.113.1",
            "2001:db8::1",
        ]


# ---------------------------------------------------------------------------
# DNS-rebinding guard — _resolve_pinned_target (resolve+reject+pin at
# connect time, closing the window between validate_searxng_url at config
# time and the actual httpx request)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_pinned_target_rejects_dns_rebind_to_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A hostname that now resolves to a private IP (rebound after the
    config-time check passed) is rejected at connect time too.
    """
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    with patch(
        "lmchat.services.web_search_service._resolve_host_ips",
        return_value=["10.0.0.5"],
    ):
        with pytest.raises(WebSearchUnavailable, match="rebinding"):
            await _resolve_pinned_target("http://searxng.rebind.example/search")


@pytest.mark.asyncio
async def test_resolve_pinned_target_pins_public_hostname_to_resolved_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A public hostname is resolved and the request target is pinned to
    the resolved IP, with a Host header + SNI override preserving the
    original hostname (so TLS SNI/cert verification still targets it).
    """
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    with patch(
        "lmchat.services.web_search_service._resolve_host_ips",
        return_value=["1.1.1.1"],
    ):
        target, headers, extensions = await _resolve_pinned_target(
            "https://searx.be/search"
        )
    assert target.host == "1.1.1.1"
    assert target.path == "/search"
    assert headers == {"Host": "searx.be"}
    assert extensions == {"sni_hostname": "searx.be"}


@pytest.mark.asyncio
async def test_resolve_pinned_target_preserves_explicit_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pinning must RETAIN a non-default port — copy_with(host=) changes only
    the host, so the pinned request still targets the configured port (not a
    different service on the resolved IP). Locks in the copy_with(host=)
    behaviour the review flagged as unverifiable.
    """
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    with patch(
        "lmchat.services.web_search_service._resolve_host_ips",
        return_value=["1.1.1.1"],
    ):
        target, headers, extensions = await _resolve_pinned_target(
            "http://searxng.example.com:7980/search"
        )
    assert target.host == "1.1.1.1"
    assert target.port == 7980  # explicit non-default port preserved by the pin
    assert target.path == "/search"
    assert headers == {"Host": "searxng.example.com"}
    assert extensions == {"sni_hostname": "searxng.example.com"}


@pytest.mark.asyncio
async def test_resolve_pinned_target_allows_private_with_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With the escape hatch set, a private/Tailscale target is returned
    unchanged — no resolution attempted, matching the admin's trusted
    self-hosted (e.g. Tailscale) SearXNG setup exactly as before.
    """
    monkeypatch.setenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", "1")
    with patch(
        "lmchat.services.web_search_service._resolve_host_ips"
    ) as mock_resolve:
        target, headers, extensions = await _resolve_pinned_target(
            "http://100.84.2.118:7980/search"
        )
    mock_resolve.assert_not_called()
    assert str(target) == "http://100.84.2.118:7980/search"
    assert headers == {}
    assert extensions == {}


@pytest.mark.asyncio
async def test_resolve_pinned_target_noop_for_literal_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A literal public IP is static — nothing to re-resolve, so the target
    is returned unchanged and no DNS lookup is attempted.
    """
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    with patch(
        "lmchat.services.web_search_service._resolve_host_ips"
    ) as mock_resolve:
        target, headers, extensions = await _resolve_pinned_target(
            "https://1.1.1.1/search"
        )
    mock_resolve.assert_not_called()
    assert str(target) == "https://1.1.1.1/search"
    assert headers == {}
    assert extensions == {}


@pytest.mark.asyncio
async def test_resolve_pinned_target_rejects_literal_private_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A literal private/loopback IP at connect time is refused (defense in
    depth vs a private literal reaching this path) — no DNS lookup, and
    WebSearchUnavailable is raised rather than connecting.
    """
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    with patch(
        "lmchat.services.web_search_service._resolve_host_ips"
    ) as mock_resolve:
        with pytest.raises(WebSearchUnavailable):
            await _resolve_pinned_target("http://192.168.1.10/search")
    mock_resolve.assert_not_called()


@pytest.mark.asyncio
async def test_resolve_pinned_target_allows_literal_tailscale_ip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A literal Tailscale CGNAT IP passes the connect-time literal-IP check
    unchanged — Tailscale is allowed before the private-IP reject.
    """
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    with patch(
        "lmchat.services.web_search_service._resolve_host_ips"
    ) as mock_resolve:
        target, _headers, _extensions = await _resolve_pinned_target(
            "http://100.84.2.118:7980/search"
        )
    mock_resolve.assert_not_called()
    assert str(target) == "http://100.84.2.118:7980/search"


@pytest.mark.asyncio
async def test_searxng_search_blocks_dns_rebind_before_connecting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WebSearchService._searxng_search refuses to connect when the
    configured hostname resolves to a private IP at request time — the
    block happens BEFORE ``self._http.get`` is ever called.
    """
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock()
    with patch(
        "lmchat.services.web_search_service._resolve_host_ips",
        return_value=["1.1.1.1"],  # public at construction time
    ):
        svc = WebSearchService(
            provider="searxng",
            searxng_url="http://searxng.rebind.example/search",
            http_client=mock_http,
        )
    with patch(
        "lmchat.services.web_search_service._resolve_host_ips",
        return_value=["192.168.1.50"],  # rebound to private by request time
    ):
        with pytest.raises(WebSearchUnavailable, match="rebinding"):
            await svc._searxng_search("query")
    mock_http.get.assert_not_called()


@pytest.mark.asyncio
async def test_searxng_search_allows_real_tailscale_endpoint_with_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end regression guard for the admin's real setup: a Tailscale
    SearXNG endpoint with the escape hatch set must keep working exactly as
    before — connects to the configured URL unchanged, unpinned.
    """
    monkeypatch.setenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", "1")
    mock_http = MagicMock(spec=httpx.AsyncClient)
    mock_http.get = AsyncMock(return_value=_make_json_response({"results": []}))

    svc = WebSearchService(
        provider="searxng",
        searxng_url="http://100.84.2.118:7980/search",
        http_client=mock_http,
    )
    results = await svc._searxng_search("query")

    assert results == []
    called_url = mock_http.get.call_args.args[0]
    assert str(called_url) == "http://100.84.2.118:7980/search"
    assert mock_http.get.call_args.kwargs["headers"] == {}


def test_ssrf_validator_allows_private_with_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """validate_searxng_url passes for a private IP when escape hatch is set."""
    monkeypatch.setenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", "1")
    # Should not raise — escape hatch is set.
    validate_searxng_url("http://127.0.0.1:8888/search")


def test_ssrf_validator_init_rejects_private_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WebSearchService.__init__ raises ValueError for private URL without escape hatch."""
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    mock_http = MagicMock(spec=httpx.AsyncClient)
    with pytest.raises(ValueError, match="private/loopback"):
        WebSearchService(
            provider="searxng",
            searxng_url="http://127.0.0.1:8888/search",
            http_client=mock_http,
        )


def test_ssrf_validator_follow_redirects_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """WebSearchService uses follow_redirects=False when no client is passed."""
    monkeypatch.setenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", "1")
    # Capture the kwargs passed to httpx.AsyncClient via a lightweight shim.
    captured: dict[str, object] = {}

    class _CapturingClient:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)

    import unittest.mock

    with unittest.mock.patch(
        "lmchat.services.web_search_service.httpx.AsyncClient", _CapturingClient
    ):
        WebSearchService(
            provider="searxng",
            searxng_url="https://searx.be",
        )

    assert captured.get("follow_redirects") is False


# ---------------------------------------------------------------------------
# P0/P1/P2 security-review remediation — IPv4-mapped IPv6 bypass, fail-open
# on DNS timeout, and unspecified-address bypass.
# ---------------------------------------------------------------------------


def test_is_private_ip_rejects_ipv4_mapped_ipv6() -> None:
    """P0: an IPv4-mapped IPv6 literal (e.g. ``::ffff:192.168.1.1``) is an
    IPv6Address and was never itself a member of the old IPv4-only
    network list, so it used to bypass the guard entirely. It must now
    unwrap to its underlying IPv4 address and be classified the same way.
    """
    assert _is_private_ip("::ffff:192.168.1.1") is True
    assert _is_private_ip("::ffff:127.0.0.1") is True
    assert _is_private_ip("::ffff:10.0.0.5") is True


def test_is_private_ip_allows_ipv4_mapped_public() -> None:
    """An IPv4-mapped IPv6 literal wrapping a PUBLIC IPv4 address is not
    flagged — confirms the unwrap doesn't over-block.
    """
    assert _is_private_ip("::ffff:8.8.8.8") is False


def test_ssrf_validator_rejects_ipv4_mapped_private_ipv6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P0 end-to-end: validate_searxng_url rejects an IPv4-mapped private
    IPv6 literal without the escape hatch.
    """
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    with pytest.raises(ValueError, match="private/loopback"):
        validate_searxng_url("http://[::ffff:192.168.1.1]:8888/search")


def test_is_private_ip_rejects_unspecified_addresses() -> None:
    """P2: 0.0.0.0 and :: (the IPv4/IPv6 "bind any" / unspecified
    addresses) are rejected — no RFC-1918/loopback/link-local range
    covered these under the old hand-rolled network list.
    """
    assert _is_private_ip("0.0.0.0") is True
    assert _is_private_ip("::") is True


def test_ssrf_validator_rejects_unspecified_ipv4(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2 end-to-end: validate_searxng_url rejects 0.0.0.0."""
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    with pytest.raises(ValueError, match="private/loopback"):
        validate_searxng_url("http://0.0.0.0:8888/search")


def test_ssrf_validator_rejects_unspecified_ipv6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P2 end-to-end: validate_searxng_url rejects ::."""
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    with pytest.raises(ValueError, match="private/loopback"):
        validate_searxng_url("http://[::]:8888/search")


@pytest.mark.asyncio
async def test_resolve_pinned_target_fails_closed_on_dns_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1: when resolution returns nothing — which is exactly what a DNS
    timeout collapses to, per ``_resolve_host_ips``'s own contract —
    ``_resolve_pinned_target`` must fail CLOSED (raise
    ``WebSearchUnavailable``) rather than falling back to an unpinned
    connect that lets httpx's own (uncapped, unchecked) resolver decide
    the destination.
    """
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    with patch(
        "lmchat.services.web_search_service._resolve_host_ips",
        return_value=[],
    ):
        with pytest.raises(WebSearchUnavailable, match="could not be resolved"):
            await _resolve_pinned_target("http://searxng.slowdns.example/search")


@pytest.mark.asyncio
async def test_resolve_pinned_target_fails_closed_on_real_dns_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """P1 end-to-end: a genuinely slow resolver — exceeding
    ``_DNS_RESOLVE_TIMEOUT_SEC``, not just a mocked empty return — must
    still raise ``WebSearchUnavailable``. The timeout is shrunk so the
    test stays fast; the resolver thread sleeps well past it, which
    reproduces the real ``concurrent.futures.TimeoutError`` path inside
    ``_resolve_host_ips`` instead of only the empty-list mock above.
    """
    import time

    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    monkeypatch.setattr(
        "lmchat.services.web_search_service._DNS_RESOLVE_TIMEOUT_SEC", 0.05
    )

    def _slow_getaddrinfo(*args: object, **kwargs: object) -> list[object]:
        time.sleep(0.5)
        return []

    with patch(
        "lmchat.services.web_search_service.socket.getaddrinfo",
        side_effect=_slow_getaddrinfo,
    ):
        with pytest.raises(WebSearchUnavailable, match="could not be resolved"):
            await _resolve_pinned_target("http://searxng.slowdns.example/search")


# ---------------------------------------------------------------------------
# Regression guards — the Tailscale CGNAT allow and the
# LM_CHAT_ALLOW_PRIVATE_SEARXNG escape hatch must keep working exactly as
# before, for both the pre-existing categories and the newly-caught ones.
# ---------------------------------------------------------------------------


def test_ssrf_validator_allows_tailscale_cgnat_literal_without_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the admin's real Tailscale SearXNG IP (100.84.2.118)
    must still be allowed with NO escape hatch set. The explicit
    ``_is_tailscale_ip`` short-circuit runs BEFORE ``_is_private_ip`` in
    ``validate_searxng_url`` — this must hold regardless of whether the
    running stdlib's ``ipaddress.is_private`` classifies 100.64.0.0/10
    (CGNAT) as private, which has varied across Python versions.
    """
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    validate_searxng_url("http://100.84.2.118:7980/search")  # must not raise


@pytest.mark.asyncio
async def test_resolve_pinned_target_allows_hostname_resolving_to_tailscale_cgnat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: connect-time pinning also allows a hostname that
    resolves to Tailscale CGNAT (100.84.2.118) with NO escape hatch set —
    the ``_is_tailscale_ip`` short-circuit inside the resolved-IP loop in
    ``_resolve_pinned_target`` must run before ``_is_private_ip`` there
    too, same ordering guarantee as the config-time validator.
    """
    monkeypatch.delenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", raising=False)
    with patch(
        "lmchat.services.web_search_service._resolve_host_ips",
        return_value=["100.84.2.118"],
    ):
        target, headers, extensions = await _resolve_pinned_target(
            "http://searxng.tailnet.example:7980/search"
        )
    assert target.host == "100.84.2.118"
    assert headers == {"Host": "searxng.tailnet.example"}


def test_ssrf_validator_allows_ipv4_mapped_private_with_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the escape hatch still allows the newly-caught
    IPv4-mapped-private category, same as it already does for plain
    private IPv4/IPv6 literals.
    """
    monkeypatch.setenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", "1")
    validate_searxng_url("http://[::ffff:192.168.1.1]:8888/search")  # must not raise


def test_ssrf_validator_allows_unspecified_with_escape_hatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Regression: the escape hatch still allows the newly-caught
    unspecified-address category.
    """
    monkeypatch.setenv("LM_CHAT_ALLOW_PRIVATE_SEARXNG", "1")
    validate_searxng_url("http://0.0.0.0:8888/search")  # must not raise
    validate_searxng_url("http://[::]:8888/search")  # must not raise
