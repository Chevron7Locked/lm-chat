# SPDX-License-Identifier: Apache-2.0
"""Tests for _escape_fts5_phrase and _build_fts5_keyword_query.

- ``_escape_fts5_phrase`` wraps the ENTIRE query in double-quotes (phrase).
- ``_build_fts5_keyword_query`` tokenises and builds OR-of-quoted-terms
  for natural-language queries (the F1a fix — default keyword path).
"""
from __future__ import annotations

from lmchat.services.retrieval_service import (
    _build_fts5_keyword_query,
    _escape_fts5_phrase,
)

# ---------------------------------------------------------------------------
# _escape_fts5_phrase — legacy phrase-only path
# ---------------------------------------------------------------------------


def test_escape_fts5_phrase_plain_query() -> None:
    """Plain alphanumeric query is wrapped in double-quotes."""
    result = _escape_fts5_phrase("hello world")
    assert result == '"hello world"'


def test_escape_fts5_phrase_empty_query() -> None:
    """Empty query becomes an empty phrase (matches nothing, no parse error)."""
    result = _escape_fts5_phrase("")
    assert result == '""'


def test_escape_fts5_phrase_operators() -> None:
    """FTS5 boolean operators are neutralised inside the phrase quote."""
    result = _escape_fts5_phrase("foo OR bar")
    assert result == '"foo OR bar"'


def test_escape_fts5_phrase_not_operator() -> None:
    """NOT operator is neutralised."""
    result = _escape_fts5_phrase("foo NOT bar")
    assert result == '"foo NOT bar"'


def test_escape_fts5_phrase_and_operator() -> None:
    """AND operator is neutralised."""
    result = _escape_fts5_phrase("foo AND bar")
    assert result == '"foo AND bar"'


def test_escape_fts5_phrase_internal_double_quotes() -> None:
    """Internal double-quotes are escaped with '' (FTS5 escape convention)."""
    result = _escape_fts5_phrase('he said "hi"')
    assert result == '"he said ""hi"""'


def test_escape_fts5_phrase_parentheses() -> None:
    """Parentheses do not cause a parse error when wrapped in phrase quotes."""
    result = _escape_fts5_phrase("(foo bar)")
    assert result == '"(foo bar)"'


def test_escape_fts5_phrase_asterisk() -> None:
    """Asterisk (prefix match) is neutralised inside phrase quotes."""
    result = _escape_fts5_phrase("foo*")
    assert result == '"foo*"'


def test_escape_fts5_phrase_single_quote() -> None:
    """Single quotes pass through unchanged (not special in FTS5 MATCH)."""
    result = _escape_fts5_phrase("it's fine")
    assert result == '"it\'s fine"'


# ---------------------------------------------------------------------------
# _build_fts5_keyword_query — default keyword path (F1a fix)
# ---------------------------------------------------------------------------


def test_build_keyword_query_single_token() -> None:
    """A single token is wrapped in quotes."""
    assert _build_fts5_keyword_query("BLUEFALCON") == '"BLUEFALCON"'


def test_build_keyword_query_multiple_tokens() -> None:
    """Multiple tokens are OR-joined with each token individually quoted."""
    result = _build_fts5_keyword_query("What does my document say about BLUEFALCON")
    assert result == '"What" OR "does" OR "my" OR "document" OR "say" OR "about" OR "BLUEFALCON"'


def test_build_keyword_query_empty_query() -> None:
    """Empty input returns empty-phrase string (matches nothing)."""
    assert _build_fts5_keyword_query("") == '""'


def test_build_keyword_query_whitespace_only() -> None:
    """Whitespace-only input returns empty-phrase string."""
    assert _build_fts5_keyword_query("   ") == '""'


def test_build_keyword_query_strips_fts5_operators() -> None:
    """FTS5 operator chars ()* are stripped per token; OR/AND/NOT lose meaning inside quotes."""
    result = _build_fts5_keyword_query("(foo bar) OR baz*")
    # Parentheses and * are stripped; OR inside quotes is literal.
    assert result == '"foo" OR "bar" OR "OR" OR "baz"'


def test_build_keyword_query_escapes_double_quotes() -> None:
    """Operator-char regex strips double-quotes from tokens; token is then quoted."""
    result = _build_fts5_keyword_query('he said "hi"')
    # The regex strips " from the token "hi" → hi.  The token is then quoted.
    assert result == '"he" OR "said" OR "hi"'


def test_build_keyword_query_query_with_only_operator_chars() -> None:
    """A query consisting entirely of operator chars returns empty-phrase."""
    assert _build_fts5_keyword_query("()*") == '""'


def test_build_keyword_query_preserves_tokens_after_dropping_empty() -> None:
    """Tokens that become empty after stripping operator chars are skipped."""
    result = _build_fts5_keyword_query("*hello* world")
    assert result == '"hello" OR "world"'