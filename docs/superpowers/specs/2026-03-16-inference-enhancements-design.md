# lm-chat Inference Enhancements Design Spec
**Date:** 2026-03-16
**Features:** Self-Consistency (SC) · Chain of Verification (CoVe)
**Status:** Approved

---

## 0. Context

lm-chat targets local LLM users on hardware where token cost = electricity, not money. SC and CoVe are not "premium modes" to be invoked sparingly — they are toggleable capabilities that run whenever enabled. Both are strictly opt-in but treated as persistent settings, not per-message invocations.

**Research basis:**
- SC: Wang et al. 2022 (arXiv:2203.11171), Universal SC — Chen et al. 2023 (arXiv:2311.17311), CGES early stopping — 2025 (arXiv:2511.02603)
- CoVe: Dhuliawala et al. 2023 (arXiv:2309.11495)

---

## 1. Self-Consistency (SC)

### What It Does
Generates N independent responses to the same message, then uses a synthesis call to select the most consistent answer. Based on Universal Self-Consistency (USC) — the only SC variant that works for open-ended chat (original SC requires normalizable short answers; USC uses an LLM-based selection step instead).

### When It Helps vs. Hurts

**Helps:** Multi-step reasoning, factual questions, technical explanations, code generation without execution, analysis tasks.

**Does not help (auto-disabled):**
- Creative Writing preset — SC selects the most average response, penalizing creative outliers
- Trivial/casual messages (detected heuristically: <15 words, no question mark or reasoning trigger words)

### Settings
- Global toggle in global settings panel
- Per-chat override in 3rd column settings panel
- Default: **off**

When enabled via per-chat settings, the chat-level value takes precedence over global.

### Algorithm

**Parameters:**
- N = 3 (parallel samples)
- Temperature = 0.7 for candidate generation (diversity without incoherence)
- Early exit: all 3 requests fire in parallel. If the first 2 responses that return (arrival order, not submission order) have >80% token overlap, skip the synthesis call and stream the first-returned response directly. The 3rd request may still be running but its result is discarded — this saves one synthesis call, not one generation.
- Synthesis temperature = 0.0 (deterministic selection)
- All candidate calls: `store: false`, `integrations: []`

**Synthesis prompt (USC standard):**
```
The user asked: "{original_question}"

Here are 3 independent responses generated for this question:

--- Response 1 ---
{response_1}

--- Response 2 ---
{response_2}

--- Response 3 ---
{response_3}

Review all 3 responses. Return the single response that is most consistent with the majority of the others — the one that best represents the central, agreed-upon answer. Return only the selected response text, verbatim. Do not add commentary or explain your choice.
```

**For factual queries**, append to the synthesis prompt:
```
Prefer the response that is most specific and factually detailed while still being consistent with the majority position.
```

**Flow:**
```
User sends message
  ↓
[SC enabled?]
  → No: normal single request path
  → Yes:
       1. Send SSE headers immediately (200 + text/event-stream)
       2. Emit: event: status / data: {"text": "Generating response 1 of 3..."}
       3. Fire 3 parallel non-streaming requests (store:false)
       ↓
       [First 2 responses back — overlap check]
       → >80% overlap: skip synthesis, stream first-returned response as synthetic SSE
       → <80% overlap: wait for response 3
                        Emit: event: status / data: {"text": "Selecting most consistent response..."}
                        Build synthesis payload (strip previous_response_id)
                        Open streaming connection to LM Studio with synthesis payload
                        Proxy SSE stream to client as normal
```

**Integration point in `_handle_chat_stream`:**
SC/CoVe intercept before the LM Studio request is opened. The SSE headers are moved to fire immediately at the start of the function (before any blocking calls), enabling status events to be emitted during processing:

```python
# Send SSE headers FIRST (moved earlier for SC/CoVe pre-stream phase)
self.send_response(200)
self.send_header("Content-Type", "text/event-stream")
self.send_header("Cache-Control", "no-cache")
self.send_header("Connection", "close")
self.send_header("X-Accel-Buffering", "no")
self._send_security_headers()
self.end_headers()

def emit_status(text):
    msg = f"event: status\ndata: {json.dumps({'text': text})}\n\n"
    self.wfile.write(msg.encode())
    self.wfile.flush()

# SC/CoVe preprocessing modifies payload["input"] and strips previous_response_id
# before the normal streaming request is opened below.
```

Both `_self_consistency` and `_chain_of_verification` share the same return contract: bare `str` | `dict` | `None`. The caller in `_handle_chat_stream` uses a shared branching helper and handles the combined SC+CoVe mode explicitly:

```python
# SC+CoVe combined: CoVe runs first, SC runs on CoVe's synthesis payload.
# SC alone or CoVe alone run independently.
if cove_enabled and sc_enabled:
    cove_result = self._chain_of_verification(payload, user_id)
    if isinstance(cove_result, dict):
        # CoVe produced a synthesis payload — run SC on it
        result = self._self_consistency(cove_result, user_id)
    else:
        # CoVe short-circuited (str) or failed (None) — use CoVe result directly
        result = cove_result
elif cove_enabled:
    result = self._chain_of_verification(payload, user_id)
elif sc_enabled:
    result = self._self_consistency(payload, user_id)
else:
    result = None  # normal single-request path

# Shared result handler
if isinstance(result, str):
    # Early-exit / no-VQ path: emit the complete response as a single synthetic SSE event,
    # then send the [DONE] sentinel. No LM Studio streaming connection needed.
    text = result or ""
    delta = json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}})
    self.wfile.write(f"data: {delta}\n\n".encode())
    self.wfile.write(b"data: [DONE]\n\n")
    self.wfile.flush()
    return
elif isinstance(result, dict):
    # Synthesis payload: replaces original payload for the stream-open below.
    payload = result
# None or not sc/cove: payload is unchanged, normal stream-open proceeds.
```

After this block, the existing SSE proxy loop (`for raw_line in resp: self.wfile.write(raw_line)`) runs unchanged.

**Reasoning models (Qwen3.5, DeepSeek-R1):**
- Thinking tokens are generated per candidate — higher per-candidate cost
- Consistency comparison is on the post-`</think>` content only
- N=3 remains appropriate; N=5 is excessive for reasoning models
- Token overlap check uses only the message portion, not the thinking portion

**Context overflow guard:** Before synthesis call, check that `len(response_1) + len(response_2) + len(response_3) + len(synthesis_prompt)` fits within the model's `context_length`. If not, truncate each candidate to `(context_length * 0.2)` chars before synthesis.

### Progress UI

Status text shown in the existing streaming status area (where "Generating..." currently appears):

```
Generating response 1 of 3...
Generating response 2 of 3...
Generating response 3 of 3...
Selecting most consistent response...
```

Or if early exit triggers:
```
Generating...
✓ Consistent answer found
```

No spinner — text only, matches existing streaming indicator style.

### Server Implementation

```python
def _self_consistency(self, payload, user_id, n=3, temperature=0.7):
    """USC self-consistency: N parallel candidates → synthesis."""
    import concurrent.futures

    base = {**payload, "store": False, "integrations": [], "temperature": temperature,
            "stream": False}  # candidate calls are non-streaming; explicit override prevents
                               # any inherited stream:True from a combined CoVe+SC call

    # Fire N candidates in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=n) as ex:
        futures = [ex.submit(self._lmstudio_chat, base, user_id) for _ in range(n)]
        candidates = []
        for f in concurrent.futures.as_completed(futures):
            try:
                candidates.append(self._extract_content(f.result()))
            except Exception:
                pass  # one failure: continue with remaining

    if len(candidates) < 2:
        # Fallback: not enough candidates, return first or re-raise
        return candidates[0] if candidates else None

    # Early exit: first two agree closely
    if _token_overlap(candidates[0], candidates[1]) > 0.80:
        return candidates[0]

    # USC synthesis — strip previous_response_id (this is a fresh standalone call)
    synthesis = self._build_usc_synthesis_prompt(payload["input"], candidates)
    result_payload = {**base, "input": synthesis, "temperature": 0.0,
                      "stream": True}  # caller will stream this
    result_payload.pop("previous_response_id", None)
    return result_payload  # caller opens streaming connection with this payload


def _token_overlap(a, b):
    """Simple token overlap ratio between two strings."""
    ta, tb = set(a.lower().split()), set(b.lower().split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)
```

---

## 2. Chain of Verification (CoVe)

### What It Does
Catches factual hallucinations by running a 4-step pipeline: draft → extract verification questions → answer each question **independently** (no draft in context) → synthesize corrected final response.

Research shows 50-70% reduction in factual hallucinations on biographical, historical, and technical queries (Dhuliawala et al. 2023).

### The Independence Constraint
**Critical:** Step 3 verification calls must have zero exposure to the draft. If the model sees its draft while answering verification questions, it anchors to the draft's claims and reproduces the same hallucinations. Each verification call is a fresh API call with a clean system prompt and no conversation history.

### When It Helps vs. Hurts

**Helps:** Biographical facts, historical events, technical specifications, named entity attributes, numerical claims, list generation tasks.

**Does not help (auto-disabled):**
- Creative Writing preset
- Queries detected as: casual/conversational, pure opinion, creative/brainstorming, mathematical derivations (SC is better for those), pure code generation (execution is the correct verifier, not language model re-answering)

**Detection heuristic (applied server-side, can be overridden by user):**
CoVe runs when the query contains ≥2 of:
- Proper nouns (capitalized words not at sentence start)
- Question words: who, when, where, how many, what year, which
- Temporal references: year, date, century, decade
- Biographical markers: born, died, founded, created, invented

This is a heuristic, not a gate — if the user has CoVe enabled, it runs regardless for all non-creative queries.

### Settings
- Global toggle in global settings panel
- Per-chat override in 3rd column settings panel
- Default: **off**

### Algorithm

**Parameters:**
- Max verification questions: 4 (caps cost at ~2.5x, not 4x)
- Draft call: `store: false` — never shown to user, never persisted
- Verification calls: `store: false`, `integrations: []`, temperature = 0.1 (low for factual recall), **no draft in context**
- Synthesis call: streams to user, persisted as the response
- Reasoning models: use `reasoning: {type: "disabled"}` for Steps 1-3 (verification steps), enable reasoning only for Step 4 synthesis — keeps verification cost low while preserving logical quality in the final answer

**Step 1 — Draft:**
```
model ← original_question
(not shown to user, not stored)
```

**Step 2 — Extract verification questions:**
```
System: You are a fact-checker. Given a question and a draft answer, generate
        targeted verification questions to check the factual claims.

        Rules:
        - Each question must be independently answerable without seeing the draft
        - Each question targets a single specific claim
        - Phrase as standalone questions, not confirmations
        - Maximum 4 questions
        - If there are no specific factual claims to verify, respond with: NONE

User:   Question: {original_question}
        Draft answer: {draft}

        Generate verification questions:
```

**Step 3 — Answer each question independently (parallel, clean context):**
```
[For each verification question VQ_i — separate API call, no draft, no conversation history]

System: Answer the following question directly and accurately. Be concise.

User:   {VQ_i}
```

**Step 4 — Synthesis (streamed to user):**
```
System: You are a careful and accurate assistant.

User:   Original question: {original_question}

        Initial draft answer (may contain errors):
        {draft}

        Verification results:
        Q: {VQ_1}
        A: {verified_answer_1}

        Q: {VQ_2}
        A: {verified_answer_2}

        [...]

        Using the verified answers, write the final response. Where verification
        answers contradict the draft, use the verified information. Acknowledge
        uncertainty where verification answers were inconclusive. Do not mention
        that this is a verification process — just provide the accurate answer.
```

**Flow:**
```
User sends message
  ↓
[CoVe enabled AND query matches heuristic?]
  → No: normal single request
  → Yes:
       Step 1: Draft (async, not shown)
       ↓
       Step 2: Extract VQs
         → "NONE": return draft as final (no factual claims to verify)
         → 1-4 VQs extracted
       ↓
       Step 3: Answer all VQs in parallel (clean context)
       ↓
       Step 4: Synthesis → stream to user → persist
```

**If SC and CoVe are both enabled:** SC runs on the final synthesis output only (Step 4), not on the draft. This avoids N×4 API calls. Specifically: the synthesis prompt from CoVe Step 4 is used as input to SC, producing 3 synthesis candidates which are then USC-selected.

### Progress UI

```
Drafting response...
Identifying claims to verify...
Verifying 3 facts...
Finalizing verified response...
```

When CoVe Step 2 returns "NONE":
```
Drafting response...
No factual claims to verify
Responding...
```

### Server Implementation

```python
def _chain_of_verification(self, payload, user_id):
    """4-step CoVe pipeline.
    Returns bare str when no verifiable claims found — caller emits as synthetic SSE.
    Returns bare dict (streaming payload) on success — caller opens LM Studio connection.
    Returns None on unrecoverable failure — caller falls back to normal single request.
    Same return contract as _self_consistency: bare str | dict | None (no tuple wrapping).

    IMPORTANT: wrap the entire function body in try/except to fulfill the None contract:
        try:
            <body>
        except Exception as e:
            log.warning(f"CoVe pipeline failed: {e}")
            return None
    Without this wrapper, a draft-call failure propagates as an exception instead of
    falling back gracefully to the normal single-request path.
    """
    # Capture original reasoning setting for re-enable in Step 4 synthesis
    original_reasoning = payload.get("reasoning")

    base_silent = {
        **payload,
        "store": False,
        "integrations": [],
        "reasoning": {"type": "disabled"},  # disable thinking for steps 1-3 only
    }

    # Step 1: Draft (never shown to user)
    draft_data = self._lmstudio_chat(base_silent, user_id)
    draft = self._extract_content(draft_data)

    # Step 2: Extract verification questions
    vq_prompt = self._build_vq_extraction_prompt(payload["input"], draft)
    vq_payload = {**base_silent, "input": vq_prompt, "temperature": 0.0, "max_output_tokens": 350}
    vq_data = self._lmstudio_chat(vq_payload, user_id)
    vqs = self._parse_verification_questions(self._extract_content(vq_data))

    if not vqs:
        # No verifiable claims — return draft as final (bare str, same contract as SC early-exit)
        return draft

    # Step 3: Answer VQs independently (parallel, clean context — NO draft)
    # CRITICAL: clean_payload must not include previous_response_id or system_prompt
    # from the original conversation — this enforces the independence constraint.
    import concurrent.futures
    def answer_vq(vq):
        clean_payload = {
            "model": payload["model"],
            "input": vq,
            "system_prompt": "Answer the following question directly and accurately. Be concise.",
            "temperature": 0.1,
            "max_output_tokens": 300,
            "store": False,
            "integrations": [],
            "reasoning": {"type": "disabled"}
            # NOTE: no previous_response_id, no conversation history
        }
        try:
            return vq, self._extract_content(self._lmstudio_chat(clean_payload, user_id, timeout=30))
        except Exception as e:
            log.warning(f"CoVe VQ answer failed: {e}")
            return vq, None  # partial failure: proceed with available answers

    vq_answers = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(len(vqs), 4)) as ex:
        futures = {ex.submit(answer_vq, vq): vq for vq in vqs}
        for f in concurrent.futures.as_completed(futures):
            vq, answer = f.result()
            if answer is not None:
                vq_answers[vq] = answer

    # Step 4: Synthesis — strip previous_response_id (fresh context), enable streaming.
    # Re-enable reasoning if the original payload had it (Steps 1-3 disabled it for cost).
    synthesis_input = self._build_cove_synthesis_prompt(
        payload["input"], draft, vq_answers
    )
    synthesis_payload = {**base_silent, "input": synthesis_input, "stream": True}
    synthesis_payload.pop("previous_response_id", None)
    if original_reasoning is not None:
        synthesis_payload["reasoning"] = original_reasoning  # restore original setting
    else:
        synthesis_payload.pop("reasoning", None)  # omit key → model default
    return synthesis_payload  # bare dict, same contract as SC (no tuple wrapping)


def _parse_verification_questions(self, text):
    """Extract numbered verification questions from LLM output. Max 4."""
    if "NONE" in text.upper():
        return []
    lines = [l.strip() for l in text.split('\n') if l.strip()]
    questions = []
    for line in lines:
        # Match "1. Question?" or "- Question?" patterns
        cleaned = re.sub(r'^[\d\-\.\)]+\s*', '', line).strip()
        if cleaned.endswith('?') and len(cleaned) > 10:
            questions.append(cleaned)
        if len(questions) >= 4:
            break
    return questions
```

---

## 3. Shared Notes

### Both Features: Timeout
The base `_lmstudio_chat` timeout is 60s. SC and CoVe run multiple blocking calls before the stream starts:
- SC: up to 3 parallel generations (~30-60s each on large models) + synthesis
- CoVe: draft + VQ extraction + parallel VQ answers + synthesis
- SC+CoVe combined: up to 3+4+1 = 8 sequential/parallel calls before first token

For SC and CoVe requests, the handler must use an extended timeout (300s, same as `_handle_chat`). The intermediate `_lmstudio_chat` calls should use `timeout=60` each; the final streaming connection uses `timeout=300`.

The client SSE connection stays open throughout (headers are sent immediately). Clients that implement their own request timeouts shorter than 300s may need to be handled — the status events keep the connection alive by sending data.

### Both Features: Token Budget Awareness
Neither SC nor CoVe sends `context_length` in the request (this causes JIT model reloads — known issue fixed in 0.2.1). All intermediate calls use `max_output_tokens` limited to reasonable values to prevent runaway generation on intermediate steps:
- SC candidates: inherit user's `max_output_tokens` setting
- CoVe draft: inherit user's `max_output_tokens`
- CoVe VQ extraction: 350 tokens max (200 is too tight — verbose models add preamble; truncation silently drops questions missing their `?` terminator)
- CoVe VQ answers: 300 tokens max each
- CoVe synthesis: inherit user's `max_output_tokens`
- SC synthesis: inherits user's `max_output_tokens` if present in the original payload, otherwise falls through to model default (returns selected response verbatim — must be full length, so no cap is applied here)

### Both Features: Error Handling
If any intermediate step fails (timeout, model error), fall back gracefully:
- SC: if <2 candidates return successfully, return the first successful candidate as a normal response
- CoVe: if VQ extraction fails, return the draft as the final response; if any VQ answer fails, proceed with synthesis using available answers

### Version
SC + CoVe ship as part of v0.3.0 alongside the core features spec.

---

## Out of Scope
- Confidence-weighted SC (requires logprobs, not exposed on native /api/v1/chat)
- Adaptive early stopping with Bayesian posteriors (CGES) — N=3 with overlap check covers 90% of the value
- Iterative CoVe (running CoVe twice on the same message)
- Per-query automatic CoVe trigger based on model confidence
- Latent SC (requires model architecture access)
