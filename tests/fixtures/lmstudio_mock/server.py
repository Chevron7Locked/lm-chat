# SPDX-License-Identifier: Apache-2.0
"""Mock LM Studio server for deterministic LLM tests.

Serves a minimal SSE endpoint at POST /api/v1/chat.
Scripts are loaded from JSONL files under scripts/.

Per docs/audit/2026-06-13-qa-security-suite-PLAN-v3.md §1A.
"""
from __future__ import annotations

import asyncio
import json
import logging
import socket
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, Response, StreamingResponse
from starlette.routing import Route

log = logging.getLogger(__name__)

# Directory containing script JSONL files (next to this file).
_SCRIPTS_DIR: Final[Path] = Path(__file__).resolve().parent / "scripts"


class MockLmStudioState:
    """Shared mutable state for the mock LM Studio server.

    Holds the current script name and parsed events.
    Thread-safe for read; writes happen between tests (no concurrency).
    """

    def __init__(self) -> None:
        self._script_name: str = "happy_text"
        self._events: list[dict[str, object]] = []

    def load_script(self, name: str) -> None:
        """Load a script by name (without .jsonl suffix).

        Args:
            name: Script basename (e.g. ``"happy_text"``).

        Raises:
            FileNotFoundError: If ``scripts/{name}.jsonl`` does not exist.
        """
        path = _SCRIPTS_DIR / f"{name}.jsonl"
        if not path.exists():
            raise FileNotFoundError(f"Script not found: {path}")
        events: list[dict[str, object]] = []
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#"):
                    events.append(json.loads(line))
        self._events = events
        self._script_name = name

    @property
    def events(self) -> list[dict[str, object]]:
        """Return a copy of the current event list."""
        return list(self._events)

    @property
    def script_name(self) -> str:
        return self._script_name


# Global singleton — shared across the server process.
_state = MockLmStudioState()


def _load_default_script() -> None:
    """Load the default (happy_text) script at import time."""
    _state.load_script("happy_text")


# Stub identifiers for embedding model.
_STUB_MODEL_ID: Final[str] = "stub-model-q4"
# Advertise the embedding model under LM Studio's canonical default catalog key
# so memory_service.resolve_active_embedding_model_key resolves it as the loaded
# default (no admin preference set) instead of failing loud. Mirrors real usage:
# LM Studio ships nomic under this key on first launch.
_STUB_EMBEDDING_MODEL_ID: Final[str] = "text-embedding-nomic-embed-text-v1.5"

# Models list response (native LM Studio format).
_MODELS_RESPONSE: Final[dict[str, object]] = {
    "object": "list",
    "data": [
        {
            "id": _STUB_MODEL_ID,
            "object": "model",
            "type": "llm",
            "publisher": "stub",
            "arch": "llama",
            "compatibility_type": "gguf",
            "quantization": "Q4_K_M",
            "state": "not-loaded",
            "max_context_length": 4096,
        },
        {
            "id": _STUB_EMBEDDING_MODEL_ID,
            "object": "model",
            "type": "embedding",
            "publisher": "stub",
            "arch": "bert",
            "compatibility_type": "gguf",
            "quantization": "Q4_K_M",
            "state": "not-loaded",
            "max_context_length": 512,
        },
    ],
    "models": [
        {
            "key": _STUB_MODEL_ID,
            "type": "llm",
            "publisher": "stub",
            "loaded_instances": [
                {"id": _STUB_MODEL_ID, "config": {"context_length": 4096}}
            ],
            "maxContextLength": 4096,
            "capabilities": {
                "vision": False,
                "trained_for_tool_use": False,
            },
        },
        {
            "key": _STUB_EMBEDDING_MODEL_ID,
            "type": "embedding",
            "publisher": "stub",
            "loaded_instances": [
                {"id": _STUB_EMBEDDING_MODEL_ID, "config": {"context_length": 512}}
            ],
            "maxContextLength": 512,
            "capabilities": {
                "vision": False,
                "trained_for_tool_use": False,
            },
        },
    ],
}


async def _healthz(request: Request) -> PlainTextResponse:
    """Health check endpoint — used by the session-scoped fixture to wait for readiness."""
    return PlainTextResponse("ok")


async def _handle_api_models(request: Request) -> JSONResponse:
    """Handle GET /api/v1/models — return model list with embedding model."""
    return JSONResponse(_MODELS_RESPONSE)


async def _handle_embeddings(request: Request) -> JSONResponse:
    """Handle POST /v1/embeddings — return deterministic embeddings.

    Returns a fixed 4-dimensional float vector for any input, matching
    the response shape expected by ``EmbeddingClient``.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    inp = body.get("input", "")
    texts = [inp] if isinstance(inp, str) else inp

    data = [
        {
            "embedding": [0.1, 0.2, -0.1, 0.05],
            "index": i,
        }
        for i in range(len(texts))
    ]
    return JSONResponse({
        "object": "list",
        "data": data,
        "model": body.get("model", _STUB_EMBEDDING_MODEL_ID),
    })


async def _handle_chat(request: Request) -> Response:
    """Handle POST /api/v1/chat — stream SSE events from the loaded script.

    Validates the request body contains a ``model`` field (required) and
    ``input`` (list, optional).  Returns 400 for a missing ``model``.

    Consumes the ``_state.events`` list and yields each event as an SSE frame.
    Special directives:
      - ``_infinite_sleep: true`` — pauses the stream indefinitely.
      - ``_raise_connection_error: true`` — raises ``ConnectionResetError``
        after sending this frame, simulating a mid-stream TCP drop.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return PlainTextResponse("Invalid JSON body", status_code=400)

    if not isinstance(body, dict):
        return PlainTextResponse("Body must be a JSON object", status_code=400)

    model = body.get("model")
    if not model or not isinstance(model, str):
        return PlainTextResponse("Missing required field: model", status_code=400)

    inp = body.get("input")
    if inp is not None and not isinstance(inp, list):
        return PlainTextResponse("input must be a list", status_code=400)

    events = _state.events

    async def _event_stream() -> AsyncIterator[bytes]:
        for ev in events:
            event_name = str(ev.get("event", ""))
            data = ev.get("data", {})
            delay = float(ev.get("delay", 0.01))  # type: ignore[arg-type]

            # Skip internal directive-only events (they carry no SSE frame).
            if event_name.startswith("_"):
                if ev.get("_infinite_sleep"):
                    await asyncio.Event().wait()
                if ev.get("_raise_connection_error"):
                    raise ConnectionResetError(
                        "Mock LM Studio connection reset (truncated_handshake)"
                    )
                continue

            if delay > 0:
                await asyncio.sleep(delay)

            frame = f"event: {event_name}\ndata: {json.dumps(data)}\n\n"
            yield frame.encode("utf-8")

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "cache-control": "no-cache",
            "connection": "keep-alive",
        },
    )


def _find_free_port() -> int:
    """Bind an ephemeral port and return the port number.

    Uses ``socket.socket(socket.AF_INET, socket.SOCK_STREAM).bind(...)``
    per the brief.  The caller must bind uvicorn to the returned port
    before another process claims it (acceptable race for test fixtures).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def create_app() -> Starlette:
    """Create the Starlette ASGI app for the mock LM Studio server.

    Returns:
        A ``Starlette`` ASGI app with a single ``POST /api/v1/chat`` route.
    """
    routes = [
        Route("/api/v1/chat", _handle_chat, methods=["POST"]),
        Route("/api/v1/models", _handle_api_models, methods=["GET"]),
        Route("/v1/embeddings", _handle_embeddings, methods=["POST"]),
        Route("/healthz", _healthz, methods=["GET"]),
    ]
    return Starlette(routes=routes)


# Load default script at import time so the server is immediately usable.
_load_default_script()


__all__ = [
    "MockLmStudioState",
    "_find_free_port",
    "_state",
    "create_app",
]