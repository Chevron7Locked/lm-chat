# SPDX-License-Identifier: Apache-2.0
"""lmstudio — LM Studio wire-layer package for lm-chat v1.

Contains:
- types.py      — canonical Pydantic shapes (the SPA-facing contract)
- native.py     — /api/v1/chat encoder + SSE decoder
- compat.py     — /v1/chat/completions encoder + SSE decoder (legacy; kept
                  for non-tool-use paths and future use)
- responses.py  — /v1/responses encoder + SSE decoder (used for
                  client-side tool-use; replaces compat for that path)
"""
