"""Regression tests for ``_normalize_input`` — mixed-content input array.

The native ``/api/v1/chat`` endpoint on LM Studio requires text parts to
carry their body in the ``content`` field and rejects ``text`` with::

    'input.0.content' is required, Unrecognized key(s) in object: 'text'

The bug we're guarding against (fixed 2026-05-18): the SPA was emitting
``{type: "text", text: "..."}`` because that's the OpenAI-compat shape.
LM Studio's native API rejected it.  The boundary normalizer rewrites
``text`` → ``content`` so any caller using either shape lands on the
canonical wire form.
"""

from __future__ import annotations

import server


def _norm(parts):
    """Convenience wrapper — calls the staticmethod on the handler class."""
    return server.Handler._normalize_input(parts)


# ---------------------------------------------------------------------------
# Text part: ``text`` → ``content`` rewrite
# ---------------------------------------------------------------------------

def test_text_part_with_text_key_is_rewritten_to_content():
    out = _norm([{"type": "text", "text": "hello"}])
    assert out == [{"type": "text", "content": "hello"}]


def test_text_part_with_content_key_passes_through():
    out = _norm([{"type": "text", "content": "hello"}])
    assert out == [{"type": "text", "content": "hello"}]


def test_text_part_with_both_keys_prefers_content():
    """If both are set, ``content`` wins — it's the canonical key."""
    out = _norm([{"type": "text", "text": "old", "content": "new"}])
    assert out == [{"type": "text", "content": "new"}]


def test_message_type_is_also_rewritten():
    """``type=message`` follows the same rule (LM Studio doc shape)."""
    out = _norm([{"type": "message", "text": "hi"}])
    assert out == [{"type": "message", "content": "hi"}]


def test_empty_text_part_is_dropped():
    out = _norm([{"type": "text", "text": ""}])
    assert out == []


def test_missing_body_is_dropped():
    out = _norm([{"type": "text"}])
    assert out == []


# ---------------------------------------------------------------------------
# Image part is untouched
# ---------------------------------------------------------------------------

def test_image_part_with_data_url_passes_through():
    part = {"type": "image", "data_url": "data:image/png;base64,iVBORw0KGgo="}
    assert _norm([part]) == [part]


def test_image_part_with_url_passes_through():
    part = {"type": "image", "url": "https://example.com/cat.jpg"}
    assert _norm([part]) == [part]


def test_image_part_without_url_is_dropped():
    out = _norm([{"type": "image"}])
    assert out == []


# ---------------------------------------------------------------------------
# Mixed-content composition (the actual bug scenario)
# ---------------------------------------------------------------------------

def test_text_plus_image_emits_canonical_shape():
    """The image-attach failure mode that triggered this regression."""
    out = _norm([
        {"type": "text", "text": "what is in this image?"},
        {"type": "image", "data_url": "data:image/png;base64,abc"},
    ])
    assert out == [
        {"type": "text", "content": "what is in this image?"},
        {"type": "image", "data_url": "data:image/png;base64,abc"},
    ]


# ---------------------------------------------------------------------------
# Non-list / malformed inputs
# ---------------------------------------------------------------------------

def test_string_input_returned_unchanged():
    """Plain-string ``input`` is the simple-message path; pass through."""
    assert _norm("just a string") == "just a string"


def test_non_dict_part_is_dropped():
    out = _norm([{"type": "text", "content": "good"}, "garbage", 42])
    assert out == [{"type": "text", "content": "good"}]


def test_part_with_no_type_is_dropped():
    out = _norm([{"text": "untyped"}])
    assert out == []


def test_unknown_type_is_forwarded_untouched():
    """``audio`` / ``video`` etc. — let upstream decide."""
    part = {"type": "audio", "url": "x"}
    assert _norm([part]) == [part]
