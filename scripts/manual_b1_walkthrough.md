# Manual walkthrough: upload → pin → re-embed against real LM Studio

**Audience**: anyone verifying this flow by hand.
**Time**: ~5–10 min.
**Prereq**: LM Studio running with at least one embedding model loaded
(`text-embedding-nomic-embed-text-v1.5` ships with every LM Studio install and works fine here).

This script is the human-runnable companion to
`tests/integration/test_lm_studio_b1_pin_and_reembed.py`. The
integration test wires the real `EmbeddingClient` + `ModelsService`
against an `httpx.MockTransport`; this walkthrough does the same loop
against the real LM Studio daemon so you can confirm the wire contract
end-to-end by hand.

## Steps

1. **Boot.**
   ```bash
   set -a && source .env.local && set +a
   uv run python -m lmchat.main &
   cd web && pnpm dev &
   ```
   Open <http://localhost:3001> in a browser. Sign in.

2. **Create a project.**
   - Sidebar → Projects → New project.
   - Name it `B1 walkthrough`.
   - In the project page, confirm the knowledge-base section reads
     "no documents yet".

3. **Pin an embedding model (B1 write-once).**
   - Project settings → Embedding model → pick the loaded model.
   - **Observation:** the pin lands on first attach; subsequent edits
     are blocked unless the project is empty. This is the
     compare-and-swap UPDATE that B1 protects.

4. **Upload a small document.**
   - Knowledge base → Add document → drop in a 1–2 KB `.txt` or `.md`.
   - Watch the docker logs (or `uv run python -m lmchat.main` stdout)
     for the embedding request — it must hit `/api/v1/embeddings`
     with the model id from step 3, not the global default.

5. **Confirm in DB.**
   ```bash
   sqlite3 ./lmchat.db "SELECT id, project_id, embedding_model_id FROM documents ORDER BY id DESC LIMIT 1;"
   sqlite3 ./lmchat.db "SELECT id, document_id, length(embedding) FROM document_chunks ORDER BY id DESC LIMIT 3;"
   ```
   The document row's `embedding_model_id` must equal the project pin
   from step 3. Each chunk's `embedding` BLOB length is `n_floats * 4`
   bytes (4 bytes per float32 component).

6. **Swap the loaded model + re-embed.**
   - In LM Studio, unload the embedding model from step 3, load a
     different embedding model (any other one in the local cache).
   - Back in LM Chat: Project settings → re-embed.
   - **Observation:** the re-embed pulls the loaded model from
     `/api/v1/models`, sees the mismatch, and re-embeds every chunk
     against the new model. The project's `embedding_model_id` pin
     updates after the loop finishes.

7. **Re-confirm in DB.**
   ```bash
   sqlite3 ./lmchat.db "SELECT id, project_id, embedding_model_id FROM documents WHERE project_id = <P>;"
   sqlite3 ./lmchat.db "SELECT id, document_id, length(embedding) FROM document_chunks WHERE document_id IN (SELECT id FROM documents WHERE project_id = <P>) LIMIT 3;"
   ```
   `embedding_model_id` now matches the NEW model. Chunk byte-lengths
   may differ (different model = different vector dimensionality).

8. **No-embedding-model failure mode.**
   - In LM Studio, unload ALL embedding models.
   - Re-embed again from the UI.
   - **Observation:** the route returns 503 with `code:
     no_embedding_model_loaded`; the frontend shows the embedding-status
     sentinel (`useEmbeddingStatus`), not a silent failure.

9. **Tear down.** Kill the BE + Vite processes. Re-load the original
   embedding model in LM Studio for the next session.

## What "green" looks like

- Step 5: document row's `embedding_model_id` = project pin.
- Step 7: same column reflects the swap.
- Step 8: 503 sentinel surfaces in the UI; no console error spew.

If any step diverges from the above, file it as a bug before tagging a
release — confirm the walkthrough completes cleanly first.
