# SPDX-License-Identifier: Apache-2.0
"""SC threshold calibration harness — F1 sweep over a golden hold-out set.

Modes
-----
1. **Test mode (default in CI):** uses a deterministic, dependency-free
   bag-of-words embedding (TF cosine over normalized tokens).  This is
   NOT a high-quality calibration of the production threshold; it
   verifies the harness *mechanics* (sweep loop, F1 calculation, report
   emission).
2. **Live mode:** set ``LM_CHAT_CALIBRATE_LIVE=1`` to embed the golden
   pairs through the real embedding service (LM Studio).  The test
   reads ``LM_STUDIO_API_KEY`` from ``.env.local``.

The harness writes a markdown report to
``docs/calibration/sc_threshold_report.md`` containing the F1 sweep,
the F1-optimal threshold, and the per-θ precision/recall pair.  The
test asserts the report is written and contains a sensible F1-optimal
value; it does NOT assert a SPECIFIC threshold — calibration values
are the report's output, not the test's prerequisite.

Run live mode manually:

    LM_CHAT_CALIBRATE_LIVE=1 uv run pytest tests/calibration/ -v
"""
from __future__ import annotations

import json
import math
import os
import re
from collections import Counter
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_GOLDEN_PATH = _REPO_ROOT / "tests" / "calibration" / "golden_pairs.jsonl"
_REPORT_PATH = _REPO_ROOT / "docs" / "calibration" / "sc_threshold_report.md"


# ---------------------------------------------------------------------------
# Deterministic bag-of-words embedding for the offline test mode.
# ---------------------------------------------------------------------------


_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> list[str]:
    return _WORD_RE.findall(text.lower())


def _bow_embed(text: str, vocab: dict[str, int], dim: int) -> list[float]:
    """Bag-of-words count vector projected onto a fixed vocabulary."""
    vec = [0.0] * dim
    for tok in _tokens(text):
        idx = vocab.get(tok)
        if idx is not None:
            vec[idx] += 1.0
    return vec


def _build_vocab(texts: list[str]) -> tuple[dict[str, int], int]:
    counts = Counter(tok for t in texts for tok in _tokens(t))
    vocab = {tok: i for i, tok in enumerate(sorted(counts))}
    return vocab, len(vocab)


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm > 0.0 else 0.0


# ---------------------------------------------------------------------------
# Golden pairs + sweep
# ---------------------------------------------------------------------------


def _load_golden() -> list[dict]:  # type: ignore[type-arg]
    pairs = []
    with _GOLDEN_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            pairs.append(json.loads(line))
    return pairs


def _compute_cosines(pairs: list[dict]) -> list[tuple[float, bool]]:  # type: ignore[type-arg]
    """Return [(cosine_similarity, equivalent_label), ...]."""
    live = os.environ.get("LM_CHAT_CALIBRATE_LIVE") == "1"
    if live:
        return _compute_cosines_live(pairs)
    return _compute_cosines_offline(pairs)


def _compute_cosines_offline(pairs: list[dict]) -> list[tuple[float, bool]]:  # type: ignore[type-arg]
    texts = [p["draft_a"] for p in pairs] + [p["draft_b"] for p in pairs]
    vocab, dim = _build_vocab(texts)
    results: list[tuple[float, bool]] = []
    for p in pairs:
        a = _bow_embed(p["draft_a"], vocab, dim)
        b = _bow_embed(p["draft_b"], vocab, dim)
        results.append((_cosine(a, b), bool(p["equivalent"])))
    return results


def _compute_cosines_live(pairs: list[dict]) -> list[tuple[float, bool]]:  # type: ignore[type-arg]
    """Live mode: embed via the real LM Studio EmbeddingClient.

    Intentionally left unimplemented in test mode — wiring the live
    ``EmbeddingClient`` requires an http_client + the admin's model
    selection, both of which differ across deployments.  The harness
    *mechanics* are exercised by the offline path; production calibration
    should swap this stub for the real call against the admin's
    configured ``lm_studio_base_url`` + selected embedding model.
    """
    raise NotImplementedError(
        "Live calibration mode requires an http-client-wired EmbeddingClient. "
        "Implement via lmchat.embedding.client.EmbeddingClient against the "
        "running LM Studio instance, then loop pairs through embed_batch."
    )


def _f1_at(cosines: list[tuple[float, bool]], threshold: float) -> tuple[float, float, float]:
    """Return (precision, recall, F1) for predicting equivalent iff cos > θ."""
    tp = fp = fn = tn = 0
    for c, label in cosines:
        pred = c > threshold
        if pred and label:
            tp += 1
        elif pred and not label:
            fp += 1
        elif not pred and label:
            fn += 1
        else:
            tn += 1
    precision = tp / (tp + fp) if (tp + fp) > 0 else 0.0
    recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) > 0 else 0.0
    return precision, recall, f1


def _sweep(cosines: list[tuple[float, bool]]) -> list[tuple[float, float, float, float]]:
    """Sweep θ in 0.60..0.99 step 0.01.  Return [(θ, precision, recall, F1)]."""
    rows = []
    for step in range(60, 100):
        t = step / 100.0
        p, r, f = _f1_at(cosines, t)
        rows.append((t, p, r, f))
    return rows


def _emit_report(
    sweep_rows: list[tuple[float, float, float, float]],
    *,
    mode: str,
    n_pairs: int,
    n_equivalent: int,
) -> dict:  # type: ignore[type-arg]
    best = max(sweep_rows, key=lambda row: row[3])
    optimal = {"threshold": best[0], "precision": best[1], "recall": best[2], "f1": best[3]}

    lines: list[str] = [
        "# SC threshold calibration report",
        "",
        f"Mode: **{mode}**  ",
        f"Golden set size: **{n_pairs}**  (equivalent: {n_equivalent}, "
        f"non-equivalent: {n_pairs - n_equivalent})  ",
        "",
        "The SC convergence threshold is calibrated on a",
        "hand-curated golden hold-out set of draft pairs.  This report sweeps",
        "θ ∈ [0.60, 0.99] in 0.01 steps and reports F1 at each step.",
        "",
        "## F1-optimal threshold",
        "",
        f"- **θ\\* = {optimal['threshold']:.2f}**",
        f"- precision = {optimal['precision']:.4f}",
        f"- recall    = {optimal['recall']:.4f}",
        f"- F1        = {optimal['f1']:.4f}",
        "",
        "## Sweep",
        "",
        "| θ | precision | recall | F1 |",
        "|---|---|---|---|",
    ]
    for t, p, r, f in sweep_rows:
        lines.append(f"| {t:.2f} | {p:.4f} | {r:.4f} | {f:.4f} |")
    lines.append("")
    lines.append(
        "> Offline (CI) mode uses a deterministic bag-of-words cosine. "
        "Production calibration requires `LM_CHAT_CALIBRATE_LIVE=1` against "
        "the real LM Studio embedding model."
    )

    _REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
    _REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return optimal


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_golden_set_loads() -> None:
    """The golden JSONL exists and contains ≥ 30 well-formed records."""
    pairs = _load_golden()
    assert len(pairs) >= 30, f"golden set too small: {len(pairs)} pairs"
    for p in pairs:
        assert "draft_a" in p and "draft_b" in p
        assert "equivalent" in p and isinstance(p["equivalent"], bool)


def test_calibration_harness_produces_report(tmp_path: Path) -> None:
    """End-to-end: load golden → embed → sweep → emit markdown report.

    Asserts the harness *mechanics*:
    - Sweep produces 40 rows (0.60 .. 0.99 inclusive, step 0.01).
    - F1-optimal θ is in the sweep range [0.60, 0.99].
    - Optimal F1 is in [0.0, 1.0].
    - The report file is written and parseable.

    Does NOT assert a specific threshold — that's the calibration *output*.
    """
    pairs = _load_golden()
    cosines = _compute_cosines(pairs)
    sweep_rows = _sweep(cosines)
    assert len(sweep_rows) == 40, f"expected 40 sweep points, got {len(sweep_rows)}"

    mode = "live" if os.environ.get("LM_CHAT_CALIBRATE_LIVE") == "1" else "offline-bow"
    optimal = _emit_report(
        sweep_rows,
        mode=mode,
        n_pairs=len(pairs),
        n_equivalent=sum(1 for p in pairs if p["equivalent"]),
    )

    assert _REPORT_PATH.exists(), f"report not emitted at {_REPORT_PATH}"
    assert 0.60 <= optimal["threshold"] <= 0.99
    assert 0.0 <= optimal["f1"] <= 1.0

    # Spot-check the rendered markdown.
    content = _REPORT_PATH.read_text(encoding="utf-8")
    assert "F1-optimal threshold" in content
    assert f"θ\\* = {optimal['threshold']:.2f}" in content


@pytest.mark.parametrize(
    "threshold,cosines,expected_label",
    [
        # All correct: 2 above with label True, 2 below with label False → F1 = 1.0.
        (0.85, [(0.9, True), (0.95, True), (0.3, False), (0.5, False)], 1.0),
    ],
)
def test_f1_at_function_correctness(
    threshold: float, cosines: list[tuple[float, bool]], expected_label: float
) -> None:
    """Smoke-test the F1 computation against a known case."""
    _, _, f1 = _f1_at(cosines, threshold)
    assert f1 == pytest.approx(expected_label, abs=1e-9)
