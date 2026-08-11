# SPDX-License-Identifier: Apache-2.0
"""JSON Schema constants for canonical SSE envelope shapes.

Single source of truth shared by BE pytest (this file) and FE vitest
(web/src/types/sub-error-schema.json).  Both sides validate against the
same schema file; BE loads it from disk so the JSON file is the SSOT.

Usage
-----
    from tests.contracts.sse_envelope_schemas import validate_sub_error

    validate_sub_error({"code": "some_error", "message": "details"})
    # Raises jsonschema.ValidationError if the payload violates the contract.
"""
from __future__ import annotations

import json
from pathlib import Path

import jsonschema

# ---------------------------------------------------------------------------
# Schema loading — the JSON file is the canonical definition; load once.
# ---------------------------------------------------------------------------

_SCHEMA_PATH = (
    Path(__file__).parent.parent.parent
    / "web" / "src" / "types" / "sub-error-schema.json"
)

with _SCHEMA_PATH.open() as _fh:
    SUB_ERROR_SCHEMA: dict = json.load(_fh)  # type: ignore[type-arg]

_validator = jsonschema.Draft202012Validator(SUB_ERROR_SCHEMA)


def validate_sub_error(payload: dict) -> None:  # type: ignore[type-arg]
    """Assert *payload* conforms to the sub.error envelope schema.

    Args:
        payload: The parsed JSON body of a ``sub.error`` SSE event.

    Raises:
        jsonschema.ValidationError: when *payload* violates the contract.
    """
    _validator.validate(payload)
