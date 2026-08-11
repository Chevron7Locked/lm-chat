/**
 * Unit tests for web/src/lib/api.ts.
 *
 * Uses Vitest + the global fetch mock via vi.spyOn / vi.fn.
 * jsdom environment provides fetch as a global (polyfilled by vitest).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { ApiClient } from "@/lib/api";
import type { ApiError } from "@/lib/api";

describe("ApiClient", () => {
  const client = new ApiClient("");

  beforeEach(() => {
    vi.resetAllMocks();
  });

  afterEach(() => {
    vi.restoreAllMocks();
  });

  it("returns parsed JSON on 200", async () => {
    const data = { hello: "world" };
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(data), { status: 200 })
    );

    const result = await client.request<typeof data>("/api/test");
    expect(result).toEqual(data);
  });

  it("throws ApiError with correct status on non-200", async () => {
    const body = { detail: "not found" };
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify(body), { status: 404 })
    );

    await expect(client.request("/api/missing")).rejects.toMatchObject({
      status: 404,
      detail: "not found",
    } satisfies Partial<ApiError>);
  });

  it("throws ApiError with status when body has no detail field", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response("Not Found", { status: 404, statusText: "Not Found" })
    );

    await expect(client.request("/api/missing")).rejects.toMatchObject({
      status: 404,
    } satisfies Partial<ApiError>);
  });

  it("includes credentials: same-origin on every request", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({}), { status: 200 })
    );

    await client.request("/api/anything");

    expect(vi.mocked(global.fetch)).toHaveBeenCalledWith(
      "/api/anything",
      expect.objectContaining({ credentials: "same-origin" })
    );
  });

  it("postForm sets Content-Type application/x-www-form-urlencoded", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), { status: 200 })
    );

    await client.postForm("/api/form", { username: "alice", password: "s3cr3t" });

    expect(vi.mocked(global.fetch)).toHaveBeenCalledWith(
      "/api/form",
      expect.objectContaining({
        method: "POST",
        headers: expect.objectContaining({
          "Content-Type": "application/x-www-form-urlencoded",
        }),
      })
    );
  });

  it("postForm encodes fields as URL-encoded body", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({}), { status: 200 })
    );

    await client.postForm("/api/form", { a: "1", b: "hello world" });

    const call = vi.mocked(global.fetch).mock.calls[0];
    const init = call?.[1] as RequestInit | undefined;
    expect(init?.body).toBe("a=1&b=hello+world");
  });

  it("auto-sets Content-Type application/json for a string body when none is provided", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), { status: 200 })
    );

    await client.request("/api/admin/models/background", {
      method: "PATCH",
      body: JSON.stringify({ model_id: "qwen3" }),
    });

    expect(vi.mocked(global.fetch)).toHaveBeenCalledWith(
      "/api/admin/models/background",
      expect.objectContaining({
        headers: expect.objectContaining({
          "Content-Type": "application/json",
        }),
      })
    );
  });

  it("preserves an explicit Content-Type and does not overwrite it with application/json", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), { status: 200 })
    );

    await client.request("/api/form", {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: "a=1",
    });

    const call = vi.mocked(global.fetch).mock.calls[0];
    const init = call?.[1] as RequestInit | undefined;
    const headers = init?.headers as Record<string, string>;
    expect(headers["Content-Type"]).toBe("application/x-www-form-urlencoded");
    // No duplicate JSON header was injected.
    expect(
      Object.keys(headers).filter((k) => k.toLowerCase() === "content-type")
    ).toHaveLength(1);
  });

  it("does not add a Content-Type for a FormData body (browser sets multipart boundary)", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ ok: true }), { status: 200 })
    );

    const form = new FormData();
    form.append("file", new Blob(["data"]), "doc.txt");
    await client.request("/api/documents", { method: "POST", body: form });

    const call = vi.mocked(global.fetch).mock.calls[0];
    const init = call?.[1] as RequestInit | undefined;
    const headers = init?.headers as Record<string, string>;
    expect(
      Object.keys(headers).some((k) => k.toLowerCase() === "content-type")
    ).toBe(false);
  });

  it("normalizeError extracts detail field from response body", async () => {
    global.fetch = vi.fn().mockResolvedValue(
      new Response(JSON.stringify({ detail: "totp required" }), { status: 401 })
    );

    let caught: ApiError | undefined;
    try {
      await client.request("/api/auth/login");
    } catch (e: unknown) {
      caught = e as ApiError;
    }

    expect(caught).toBeDefined();
    expect(caught?.detail).toBe("totp required");
    expect(caught?.status).toBe(401);
    expect(caught?.message).toBe("totp required");
  });
});
