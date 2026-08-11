# SPDX-License-Identifier: Apache-2.0
"""Quality-mode orchestration — Self-Consistency (SC) and Chain-of-Verification (CoVe).

Algorithms:
- Self-Consistency: Wang et al., "Self-Consistency Improves Chain of
  Thought Reasoning in Language Models," arXiv:2203.11171, 2022.
  We use embedding-cosine as a semantic majority-vote proxy for
  free-form chat responses.  Convergence aligns with Wang 2022's
  majority-vote intent: a draft set is converged
  iff the count of unique pairs with cosine > θ exceeds N*(N-1)/4
  (a strict majority of the N*(N-1)/2 pairwise comparisons).  The
  prior ``max(pairwise_cosines) > θ`` rule was a permissive single-pair
  trigger; the new aggregation requires the majority of pairs to
  agree.  Threshold default 0.85; calibration pending — see
  ``tests/calibration/test_sc_threshold.py``.

- Chain-of-Verification: Dhuliawala et al., "Chain-of-Verification
  Reduces Hallucination in Large Language Models," arXiv:2309.11495,
  2023 — §3 Joint variant. Joint runs all verification steps in one
  context; we extend to three serialised steps (question generation →
  independent answer generation → revision) to isolate each stage.

CHANGELOG-0.5.3 regression contract
-------------------------------------
Every internal adapter call inside CoVe MUST pass the caller-supplied
``integrations`` array verbatim.  The test
``tests/services/test_quality_cove_integrations.py`` pins this
behaviour.  Do not refactor the CoVe implementation without ensuring
that test still passes.

Self-Consistency is deliberately the OPPOSITE and has NO ``integrations``
parameter at all.  SC measures the model's own answer-distribution
stability under repeated i.i.d. sampling of the identical prompt/context
(temperature is the only thing that varies between drafts).  Handing tools
to SC would let the N parallel drafts diverge for reasons that have
nothing to do with the model's own sampling variance — different tool-call
decisions, different retrieved facts, tool latency/rate-limit skew, or (for
stateful tools) genuine side effects firing N times per turn instead of
once — any of which would confound the embedding-cosine convergence signal
this mode exists to produce.  CoVe's tool access is justified precisely
because its verification step exists to ground claims via lookups; SC has
no verification step, so there is nothing for tools to ground.  The test
``tests/services/test_quality_sc_no_integrations.py`` pins this asymmetry.
If SC-with-tools is ever wanted, it must ship as a new, explicitly-named
variant (e.g. a ``self_consistency_grounded``) so the two measurement
semantics are never silently blended.
"""
from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Final

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncEngine

from lmchat.config import get_settings
from lmchat.embedding.client import EmbeddingClient
from lmchat.lmstudio.types import CanonicalChatRequest, CanonicalEvent, CanonicalInputBlock
from lmchat.logging import get_logger
from lmchat.services.lmstudio_adapter import LmstudioAdapter
from lmchat.services.memory_service import (
    NoEmbeddingModelLoadedError,
    resolve_active_embedding_model_key,
)
from lmchat.services.models_service import ModelsService

if TYPE_CHECKING:
    pass  # TYPE_CHECKING block kept for future use

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# CoVe prompt templates — Final constants (never inline magic strings).
# ---------------------------------------------------------------------------

_COVE_VERIFY_QUESTIONS_TMPL: Final[str] = (
    "Read this answer carefully:\n\n"
    "<answer>{answer}</answer>\n\n"
    "List 3-5 concise verification questions that would test the load-bearing "
    "factual claims in the answer. Output one question per line, prefixed "
    "with `- `."
)

_COVE_VERIFY_ANSWER_TMPL: Final[str] = (
    "Question: {question}\n\n"
    "Answer concisely. If you don't know, say \"I don't know.\""
)

_COVE_REVISE_TMPL: Final[str] = (
    "Original answer:\n"
    "<original>{original}</original>\n\n"
    "Verification:\n"
    "{verification_block}\n\n"
    "If the verification reveals errors in the original, write a corrected "
    "answer. If the original is correct, output the original verbatim. "
    "Output only the final answer."
)

# Temperature used for SC drafts.  Must not be 1.0 — that changes the
# convergence regime ("do not use temperature 1.0").
_SC_TEMPERATURE: Final[float] = 0.7


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class QualityModeError(Exception):
    """Base class for all quality-mode errors."""


class QualityModeUpstreamError(QualityModeError):
    """Raised when the adapter yields an ``error`` event during generation.

    Attributes:
        error_code: The short machine-readable code from the event's
            ``error`` dict (e.g. ``"upstream_unavailable"``).
        message:    Human-readable detail string from the error event.
    """

    def __init__(self, *, error_code: str, message: str) -> None:
        super().__init__(f"upstream error [{error_code}]: {message}")
        self.error_code: str = error_code
        self.message: str = message


class QualityModeConvergenceFailure(QualityModeError):
    """Raised when SC cannot produce any draft to return.

    In practice this only fires when ``n_drafts=0`` is passed, which is
    a caller bug.  The default ``n_drafts=3`` always produces a result.
    """


# ---------------------------------------------------------------------------
# Public result models
# ---------------------------------------------------------------------------


class SelfConsistencyResult(BaseModel):
    """Intermediate result from a self-consistency run.

    Not required to be public by the spec but exposed for testing and for
    future API surface (callers may want the full draft set).

    Attributes:
        drafts:       All generated draft strings in input order.
        chosen:       The most-central draft (highest mean cosine to peers).
                      On ties the first draft in input order is returned.
        mean_cosine:  The chosen draft's mean pairwise cosine to the other
                      N-1 drafts.  ``0.0`` when ``n_drafts == 1``.
        converged:    ``True`` if any pairwise cosine exceeded the
                      configured threshold (settings.lm_chat_sc_threshold).
    """

    drafts: list[str]
    chosen: str
    mean_cosine: float
    converged: bool


class ChainOfVerificationResult(BaseModel):
    """Result from a chain-of-verification run.

    Attributes:
        initial_answer:        The verbatim answer from Step 1.
        verification_questions: Questions generated in Step 2.
        verification_answers:  Per-question answers from Step 3 (same order
                               as ``verification_questions``).
        revised_answer:        The final answer from Step 4; may equal
                               ``initial_answer`` if no corrections needed.
        converged:             ``True`` if ``revised_answer.strip() ==
                               initial_answer.strip()`` (no revision needed).
    """

    initial_answer: str
    verification_questions: list[str]
    verification_answers: list[str]
    revised_answer: str
    converged: bool


# ---------------------------------------------------------------------------
# Cosine helper (no external dependency — avoids numpy import for N≤~10)
# ---------------------------------------------------------------------------


def _cosine(a: list[float], b: list[float]) -> float:
    """Return the cosine similarity between two equal-length float vectors.

    Uses a pure-Python dot product.  Acceptable for the SC use case (N ≤ ~10
    drafts; embedding dimensionality is handled natively by multiplying
    corresponding elements).  Raises ``ZeroDivisionError`` if either vector
    has zero magnitude, which would indicate a malformed embedding — the
    caller catches this and surfaces it as a convergence failure.

    Args:
        a: First embedding vector.
        b: Second embedding vector (must be same length as ``a``).

    Returns:
        Cosine similarity in ``[-1.0, 1.0]``.
    """
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    mag_a = sum(x * x for x in a) ** 0.5
    mag_b = sum(x * x for x in b) ** 0.5
    return dot / (mag_a * mag_b)


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class QualityModeService:
    """Orchestrates Self-Consistency and Chain-of-Verification quality modes.

    Both modes use the injected ``LmstudioAdapter`` as their only generation
    path — no direct HTTP calls, no OpenAI/Anthropic SDK calls.

    Args:
        adapter:          Shared ``LmstudioAdapter`` instance.
        embedding_client: Shared ``EmbeddingClient`` instance (for SC cosine).
        models_service:   ``ModelsService`` for default embedding-model lookup.
    """

    def __init__(
        self,
        *,
        adapter: LmstudioAdapter,
        embedding_client: EmbeddingClient,
        models_service: ModelsService,
        engine: AsyncEngine | None = None,
    ) -> None:
        self._adapter = adapter
        self._embedding_client = embedding_client
        self._models_service = models_service
        # Optional engine for persisting the embedding-model preference.
        # When None (legacy test paths), the resolver runs without DB persist.
        self._engine: AsyncEngine | None = engine

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    async def _stream_to_string(events: AsyncIterator[CanonicalEvent]) -> str:
        """Concatenate ``message.delta`` content from a canonical event stream.

        Ignores ``reasoning.*``, ``tool_call.*``, ``prompt_processing.*``,
        and ``model_load.*`` events.  Stops naturally when the async iterator
        is exhausted (``chat.end`` causes the adapter to close the iterator).

        Args:
            events: Async iterator of :class:`~lmchat.lmstudio.types.CanonicalEvent`.

        Returns:
            Concatenated content string from all ``message.delta`` events.

        Raises:
            QualityModeUpstreamError: If an ``error`` event is encountered.
        """
        parts: list[str] = []
        async for ev in events:
            if ev.type == "error":
                raise QualityModeUpstreamError(
                    error_code=ev.error.get("code", "unknown") if ev.error else "unknown",
                    message=ev.error.get("message", "") if ev.error else "",
                )
            if ev.type == "message.delta" and ev.content:
                parts.append(ev.content)
        return "".join(parts)

    async def _generate(
        self,
        *,
        prompt: str,
        model_id: str,
        temperature: float | None = None,
        integrations: list[str] | None = None,
        system_prompt: str | None = None,
    ) -> str:
        """Fire a single generation and return the complete response string.

        Builds a :class:`~lmchat.lmstudio.types.CanonicalChatRequest` with the
        given parameters and drains the event stream.

        Args:
            prompt:        User-turn text.
            model_id:      LM Studio model key.
            temperature:   Sampling temperature; ``None`` lets LM Studio use
                           its default.
            integrations:  MCP integration IDs to pass verbatim to the
                           adapter.  ``None`` passes an empty list (native
                           surface, no integrations).
            system_prompt: The assembled system prompt for this turn (project
                           instructions, date/context, persona, RAG, and prior
                           history composed by the caller). Threaded onto EVERY
                           internal generation so quality modes answer WITH the
                           full turn context instead of the bare user text
                           (T1-7: the modes were context-blind/amnesiac).
                           ``None`` preserves the legacy no-system-prompt call.

        Returns:
            Complete generated text string.

        Raises:
            QualityModeUpstreamError: On upstream error event.
        """
        req = CanonicalChatRequest(
            model=model_id,
            system_prompt=system_prompt,
            input=[CanonicalInputBlock(type="text", content=prompt)],
            temperature=temperature,
            integrations=integrations or [],
        )
        events = self._adapter.stream_chat(req)
        return await self._stream_to_string(events)

    async def _default_embedding_model_id(self) -> str:
        """Return the stable, persisted embedding model key.

        Delegates to :func:`~lmchat.services.memory_service.resolve_active_embedding_model_key`
        (single source of truth for all new-indexing paths).

        When ``engine`` is available (production path), the chosen key is
        persisted to ``server_lm_studio_default`` so subsequent calls are
        deterministic.  When ``engine`` is None (test/legacy path), the
        resolver picks deterministically but does NOT write to the DB.

        Returns:
            Model key string for the active embedding model.

        Raises:
            NoEmbeddingModelLoadedError: If the preferred model is not loaded,
                or if no embedding model is loaded at all.
        """
        if self._engine is not None:
            return await resolve_active_embedding_model_key(
                engine=self._engine,
                models_service=self._models_service,
                persist_default=True,
            )

        # No engine available — deterministic pick without DB persist.
        # Mirrors the Step 4 logic: sort by key, take first.
        # list_loaded() returns the full catalog; filter on loaded_instance_ids
        # so an in-catalog-but-unloaded embedder can't win the sort.
        loaded = await self._models_service.list_loaded()
        embedding_models = [
            m for m in loaded if m.type == "embedding" and m.loaded_instance_ids
        ]
        if not embedding_models:
            raise NoEmbeddingModelLoadedError(
                "No embedding model is currently loaded in LM Studio. "
                "Load an embedding model to use self-consistency quality mode."
            )
        return sorted(m.key for m in embedding_models)[0]

    # ------------------------------------------------------------------
    # Self-Consistency
    # ------------------------------------------------------------------

    async def self_consistency(
        self,
        *,
        prompt: str,
        n_drafts: int = 3,
        model_id: str,
        system_prompt: str | None = None,
    ) -> str:
        """Generate N drafts and return the most semantically central one.

        Deliberately tool-free — this method has NO ``integrations``
        parameter and never forwards tools to its drafts.  See the module
        docstring's "CHANGELOG-0.5.3 regression contract" section for the
        full rationale: SC measures the model's OWN answer-distribution
        stability under repeated i.i.d. sampling of the identical
        prompt/context, and tool calls would introduce variance unrelated
        to that intrinsic signal (divergent tool decisions/results across
        the N parallel drafts, and side effects fired N times instead of
        once for stateful tools). This is the opposite of
        ``chain_of_verification``, whose tool access is justified by its
        verification step. Do not "helpfully" add integrations threading
        here without updating both this docstring and
        ``tests/services/test_quality_sc_no_integrations.py``.

        Algorithm (Wang et al. 2022 — embedding-cosine variant for free-form):

        1. Fire ``n_drafts`` generation requests in parallel at temperature
           ``_SC_TEMPERATURE`` (0.7).
        2. Embed all drafts via ``embedding_client.embed_batch``.
        3. Compute pairwise cosine similarity (N×N matrix, ignore diagonal).
        4. Convergence: ``True`` iff
           ``(count of unique pairs with cosine > θ) > N*(N-1)/4``
           — majority of unique pairs (aligns with Wang 2022
           majority-vote intent).  N*(N-1)/2 is the total unique-pair count.
        5. Return the most-central draft = highest mean cosine to all other
           N-1 drafts.  Ties are broken by earliest input order.

        Edge cases:
        - ``n_drafts == 0`` → raises :exc:`QualityModeConvergenceFailure`.
        - ``n_drafts == 1`` → returns the single draft with
          ``converged=False`` (no pair to test).

        Args:
            prompt:        The user prompt to answer.
            n_drafts:      Number of independent generations.  Defaults to 3.
            model_id:      LM Studio model key to generate with.
            system_prompt: Assembled turn context (project/persona/date/RAG/
                           history) applied to every draft. ``None`` = legacy
                           context-blind behaviour.

        Returns:
            The chosen draft string.

        Raises:
            QualityModeConvergenceFailure: If ``n_drafts == 0``.
            QualityModeUpstreamError:      If any generation returns an error
                                           event.
        """
        if n_drafts == 0:
            raise QualityModeConvergenceFailure("n_drafts must be >= 1")

        log.info(
            "quality_modes.sc.start",
            model_id=model_id,
            n_drafts=n_drafts,
        )

        # --- Step 1: parallel generation ---
        # NOTE: deliberately no `integrations=` kwarg here — SC is tool-free
        # by design (see module + method docstrings). Do not add it.
        tasks = [
            self._generate(
                prompt=prompt,
                model_id=model_id,
                temperature=_SC_TEMPERATURE,
                system_prompt=system_prompt,
            )
            for _ in range(n_drafts)
        ]
        drafts: list[str] = await asyncio.gather(*tasks)

        if n_drafts == 1:
            log.info(
                "quality_modes.sc.single_draft",
                model_id=model_id,
                converged=False,
            )
            return drafts[0]

        # --- Step 2: embed ---
        embedding_model_id = await self._default_embedding_model_id()
        vectors = await self._embedding_client.embed_batch(
            texts=drafts,
            model_id=embedding_model_id,
        )

        # --- Step 3: pairwise cosine ---
        n = len(drafts)
        # Build N×N matrix (symmetric; diagonal is 1.0 but we ignore it).
        cosines: list[list[float]] = [[0.0] * n for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                c = _cosine(vectors[i], vectors[j])
                cosines[i][j] = c
                cosines[j][i] = c

        # Collect UNIQUE pairs (upper triangle) for the majority-vote
        # convergence test.  Total unique pairs = N*(N-1)/2;
        # majority threshold = N*(N-1)/4 (strict-greater-than → at least
        # ⌊N*(N-1)/4⌋ + 1 pairs above θ).
        unique_pairs = [cosines[i][j] for i in range(n) for j in range(i + 1, n)]

        settings = get_settings()
        threshold = settings.lm_chat_sc_threshold
        max_cosine = max(unique_pairs) if unique_pairs else 0.0
        above_threshold = sum(1 for c in unique_pairs if c > threshold)
        majority_required = (n * (n - 1)) / 4.0
        converged = above_threshold > majority_required

        # --- Step 4: pick most-central draft ---
        # Mean cosine to the other N-1 drafts.  Ties → first in input order
        # (stable because we iterate in range(n) order).
        mean_cosines = [
            sum(cosines[i][j] for j in range(n) if j != i) / (n - 1)
            for i in range(n)
        ]
        best_idx = mean_cosines.index(max(mean_cosines))
        chosen = drafts[best_idx]

        log.info(
            "quality_modes.sc.result",
            model_id=model_id,
            n_drafts=n_drafts,
            converged=converged,
            max_cosine=round(max_cosine, 4),
            chosen_mean_cosine=round(mean_cosines[best_idx], 4),
            chosen_index=best_idx,
            above_threshold=above_threshold,
            majority_required=majority_required,
        )

        return chosen

    # ------------------------------------------------------------------
    # Chain-of-Verification
    # ------------------------------------------------------------------

    async def chain_of_verification(
        self,
        *,
        prompt: str,
        model_id: str,
        integrations: list[str] | None = None,
        system_prompt: str | None = None,
    ) -> ChainOfVerificationResult:
        """Run a chain-of-verification pass over ``prompt``.

        Implements Dhuliawala et al. 2023 §3 Joint variant in four steps:

        1. **Initial answer** — generate the answer for ``prompt``.
        2. **Verification questions** — ask the model to produce 3-5 questions
           that test the load-bearing claims in the initial answer.
        3. **Verification answers** — answer each question independently, in
           parallel.
        4. **Revision** — present the original answer + all Q&A pairs and ask
           for a corrected answer (or the original verbatim if correct).

        CRITICAL — ``integrations`` passthrough (CHANGELOG-0.5.3 regression):
        Every adapter call in all four steps passes the caller-supplied
        ``integrations`` value verbatim.  This is non-negotiable; the test
        ``test_cove_passes_integrations_to_every_internal_call`` pins it.
        ``system_prompt`` is threaded the SAME way — every step sees the full
        turn context.

        Args:
            prompt:        The user prompt to answer and verify.
            model_id:      LM Studio model key to use for all steps.
            integrations:  MCP integration IDs.  Passed to EVERY internal
                           adapter call.  ``None`` is passed as-is (the
                           adapter treats it as an empty integrations list).
            system_prompt: Assembled turn context (project/persona/date/RAG/
                           history). Passed to EVERY internal step so the
                           answer AND the verification pass share the same
                           context. ``None`` = legacy context-blind behaviour.

        Returns:
            A :class:`ChainOfVerificationResult` with all step outputs.

        Raises:
            QualityModeUpstreamError: If any generation step returns an error
                                      event.
        """
        log.info(
            "quality_modes.cove.start",
            model_id=model_id,
            has_integrations=bool(integrations),
        )

        # --- Step 1: initial answer ---
        log.info("quality_modes.cove.step1.initial_answer", model_id=model_id)
        initial_answer = await self._generate(
            prompt=prompt,
            model_id=model_id,
            integrations=integrations,
            system_prompt=system_prompt,
        )

        # --- Step 2: verification questions ---
        log.info("quality_modes.cove.step2.verification_questions", model_id=model_id)
        vq_prompt = _COVE_VERIFY_QUESTIONS_TMPL.format(answer=initial_answer)
        vq_raw = await self._generate(
            prompt=vq_prompt,
            model_id=model_id,
            integrations=integrations,
            system_prompt=system_prompt,
        )

        # Parse: strip leading "- " prefix from each non-empty line.
        verification_questions = [
            line[2:] if line.startswith("- ") else line
            for line in vq_raw.splitlines()
            if line.strip()
        ]

        if not verification_questions:
            log.warning(
                "quality_modes.cove.empty_verification_questions",
                model_id=model_id,
                note="No questions parsed; returning initial answer as revised (converged=True).",
            )
            return ChainOfVerificationResult(
                initial_answer=initial_answer,
                verification_questions=[],
                verification_answers=[],
                revised_answer=initial_answer,
                converged=True,
            )

        # --- Step 3: verification answers (parallel) ---
        log.info(
            "quality_modes.cove.step3.verification_answers",
            model_id=model_id,
            n_questions=len(verification_questions),
        )
        va_tasks = [
            self._generate(
                prompt=_COVE_VERIFY_ANSWER_TMPL.format(question=q),
                model_id=model_id,
                integrations=integrations,
                system_prompt=system_prompt,
            )
            for q in verification_questions
        ]
        verification_answers: list[str] = await asyncio.gather(*va_tasks)

        # --- Step 4: revision ---
        log.info("quality_modes.cove.step4.revision", model_id=model_id)
        verification_block = "\n".join(
            f"- Q: {q}\n  A: {a}"
            for q, a in zip(verification_questions, verification_answers, strict=False)
        )
        revise_prompt = _COVE_REVISE_TMPL.format(
            original=initial_answer,
            verification_block=verification_block,
        )
        revised_answer = await self._generate(
            prompt=revise_prompt,
            model_id=model_id,
            integrations=integrations,
            system_prompt=system_prompt,
        )

        converged = revised_answer.strip() == initial_answer.strip()

        log.info(
            "quality_modes.cove.result",
            model_id=model_id,
            n_questions=len(verification_questions),
            converged=converged,
        )

        return ChainOfVerificationResult(
            initial_answer=initial_answer,
            verification_questions=verification_questions,
            verification_answers=list(verification_answers),
            revised_answer=revised_answer,
            converged=converged,
        )
