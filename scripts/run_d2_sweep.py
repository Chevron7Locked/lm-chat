#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""D2 RAG-mode threshold empirical sweep — CLI entry point.

Real harness (not a stub). Indexes a corpus end-to-end through the
``documents_service.upload_document`` pipeline, runs hybrid retrieval
across a threshold grid against a hand-labeled gold set, and prints
the recall@k table + the recommended ``_DEFAULT_INLINE_FRACTION``.

Implementation lives in :mod:`lmchat.services.d2_sweep`. This script
is the operator-facing entry point.

Usage:

    python scripts/run_d2_sweep.py \\
        --corpus  ~/smallcode \\
        --gold    /path/to/gold.json \\
        --ctx-window 131000 \\
        --thresholds 2000,4000,8000,16000,32000

First-run flow when the operator doesn't yet have ``relevant_doc_ids``
labels:

1. Run with ``--print-id-map`` and any placeholder gold set
   (``[{"query": "...", "relevant_doc_ids": []}]``).
2. Read the printed ``filename → doc_id`` map.
3. Curate the real gold set with the doc ids that match each query.
4. Re-run without ``--print-id-map``.
"""
from __future__ import annotations

import sys

from lmchat.services.d2_sweep import cli_run

if __name__ == "__main__":
    raise SystemExit(cli_run(sys.argv[1:]))
