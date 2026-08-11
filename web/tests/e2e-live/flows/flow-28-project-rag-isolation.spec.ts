/**
 * Flow 28 — Project RAG isolation: doc in project A must not leak into project B.
 *
 * What it proves:
 *   Document retrieval is tenant-isolated at the project level. A document
 *   uploaded to Project A must NOT appear in the LM Studio context of a chat
 *   in Project B (different project, same user). The positive control
 *   (flow-28b) proves the isolation predicate is conditional, not a blanket
 *   block: a doc in project P IS retrieved by a chat in project P.
 *
 *   Guards the Phase-4 predicate in retrieval_service.retrieve()
 *   (`AND d.project_id = :project_id`, applied to BOTH the FTS5 keyword stage
 *   and the vector stage). If that predicate is dropped, project A's docs
 *   would leak into project B's retrieved context.
 *
 * Where the assertion lives:
 *   RAG augmentation is a BACKEND operation (stream_chat → rag_service →
 *   retrieval_service), invisible to the FE→backend request. The stub LM
 *   Studio captures the upstream /api/v1/chat body to a per-worker file
 *   (chatCaptureFile fixture); we read it and assert on the real
 *   `system_prompt` the model would see.
 */

import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  patchStreamInjectModel,
  ensureModelSelected,
  resetCapturedUpstreamRequest,
  readCapturedUpstreamRequest,
} from "./_flow-helpers";

test(
  "flow-28: project-A doc with unique token does NOT reach a project-B chat's LM Studio context",
  async ({ page, backendURL, chatCaptureFile, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    const TOKEN_A = `FLOW28PROJECTA${Date.now().toString()}`;
    const DOC_TEXT_A = `Project A secret token: ${TOKEN_A}. This must stay in project A.\n`;

    // Project A + doc.
    const createAResp = await page.request.post(`${backendURL}/api/projects`, {
      form: { name: "Flow28ProjectA", description: "Project A isolation test" },
    });
    expect(createAResp.ok()).toBe(true);
    const projectA = (await createAResp.json()) as { id: number };
    const projectAId = projectA.id;

    const uploadResp = await page.request.post(
      `${backendURL}/api/projects/${String(projectAId)}/documents`,
      {
        multipart: {
          file: {
            name: "flow28-project-a.txt",
            mimeType: "text/plain",
            buffer: Buffer.from(DOC_TEXT_A, "utf-8"),
          },
        },
      }
    );
    expect(uploadResp.ok()).toBe(true);
    const uploadData = (await uploadResp.json()) as { id: number; chunk_count: number };
    expect(uploadData.chunk_count).toBeGreaterThan(0);

    // Project B + chat (different project, same user).
    const createBResp = await page.request.post(`${backendURL}/api/projects`, {
      form: { name: "Flow28ProjectB", description: "Project B isolation test" },
    });
    expect(createBResp.ok()).toBe(true);
    const projectB = (await createBResp.json()) as { id: number };
    const projectBId = projectB.id;

    const chatBResp = await page.request.post(
      `${backendURL}/api/projects/${String(projectBId)}/chats`,
      { form: { title: "Flow28 Project B Chat" } }
    );
    expect(chatBResp.ok()).toBe(true);
    const chatB = (await chatBResp.json()) as { id: number };
    const chatBId = chatB.id;

    // Enable RAG on the project-B chat.
    const patchResp = await page.request.patch(`${backendURL}/api/chats/${String(chatBId)}`, {
      form: { rag_enabled: "true" },
    });
    expect(patchResp.ok()).toBe(true);

    // Drive the project-B chat.
    await page.goto(`${backendURL}/chats/${String(chatBId)}`);
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 15_000 });
    await ensureModelSelected(page);

    resetCapturedUpstreamRequest(chatCaptureFile);
    await patchStreamInjectModel(page);

    const composer = page.getByPlaceholder(/Message/);
    await composer.fill(`What is the value of ${TOKEN_A}?`);
    await composer.press("Meta+Enter");
    await expect(composer).toBeEnabled({ timeout: 20_000 });

    // ISOLATION: Token A must NOT appear in the project-B chat's upstream context.
    const upstream = await readCapturedUpstreamRequest(chatCaptureFile);
    const sysPrompt = upstream.system_prompt ?? "";
    expect(sysPrompt).not.toContain(TOKEN_A);

    assertNoConsoleErrors(collectErrors(), "flow-28");
  }
);

test(
  "flow-28b: doc uploaded to a project IS retrieved into a chat within that same project",
  async ({ page, backendURL, chatCaptureFile, testUsername, testPassword }) => {
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, testUsername, testPassword);

    const TOKEN_SAME = `FLOW28BSAME${Date.now().toString()}`;
    const DOC_TEXT = `Same-project context token: ${TOKEN_SAME}. Should appear in context.\n`;

    const createResp = await page.request.post(`${backendURL}/api/projects`, {
      form: { name: "Flow28bProject", description: "Same-project RAG test" },
    });
    expect(createResp.ok()).toBe(true);
    const project = (await createResp.json()) as { id: number };
    const projectId = project.id;

    const uploadResp = await page.request.post(
      `${backendURL}/api/projects/${String(projectId)}/documents`,
      {
        multipart: {
          file: {
            name: "flow28b-same-project.txt",
            mimeType: "text/plain",
            buffer: Buffer.from(DOC_TEXT, "utf-8"),
          },
        },
      }
    );
    expect(uploadResp.ok()).toBe(true);
    const uploadData = (await uploadResp.json()) as { chunk_count: number };
    expect(uploadData.chunk_count).toBeGreaterThan(0);

    const chatResp = await page.request.post(
      `${backendURL}/api/projects/${String(projectId)}/chats`,
      { form: { title: "Flow28b Same-Project Chat" } }
    );
    expect(chatResp.ok()).toBe(true);
    const chatData = (await chatResp.json()) as { id: number };
    const chatId = chatData.id;

    const patchResp = await page.request.patch(`${backendURL}/api/chats/${String(chatId)}`, {
      form: { rag_enabled: "true" },
    });
    expect(patchResp.ok()).toBe(true);

    await page.goto(`${backendURL}/chats/${String(chatId)}`);
    await page.getByPlaceholder(/Message/).waitFor({ state: "visible", timeout: 15_000 });
    await ensureModelSelected(page);

    resetCapturedUpstreamRequest(chatCaptureFile);
    await patchStreamInjectModel(page);

    const composer = page.getByPlaceholder(/Message/);
    await composer.fill(`Tell me about ${TOKEN_SAME}`);
    await composer.press("Meta+Enter");
    await expect(composer).toBeEnabled({ timeout: 20_000 });

    // Same-project doc MUST appear in the upstream context.
    const upstream = await readCapturedUpstreamRequest(chatCaptureFile);
    const sysPrompt = upstream.system_prompt ?? "";
    expect(sysPrompt).toContain(TOKEN_SAME);

    assertNoConsoleErrors(collectErrors(), "flow-28b");
  }
);
