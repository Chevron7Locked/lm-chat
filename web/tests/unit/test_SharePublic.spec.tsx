/* SPDX-License-Identifier: Apache-2.0 */
/**
 * SharePublic page unit tests — render + transport states.
 *
 * Locked behaviours:
 *   - With no `:token` route param the page surfaces the "share not found"
 *     state immediately, without firing an api.request.
 *   - On a 404 from the backend the same "not found" state surfaces.
 *   - On a successful GET, the eyebrow ("Shared conversation · <date>"),
 *     the chat title heading, and one rendered message per backend row
 *     show up, plus the "Powered by LM Chat" footer attribution link wired
 *     back to /.
 *   - On a non-404 error the error-state copy surfaces with the error
 *     message text.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  render,
  screen,
  waitFor,
  cleanup,
} from "@testing-library/react";
import { createElement } from "react";
import { MemoryRouter, Routes, Route } from "react-router-dom";

// ─── Mock api.request — the only network surface SharePublic uses ────────────

const mockRequest = vi.fn<(...args: unknown[]) => Promise<unknown>>();
vi.mock("@/lib/api", () => ({
  api: { request: (...args: unknown[]) => mockRequest(...args) },
  ApiClient: vi.fn(),
}));

// ─── Mock ChatMessage — keep the test focused on SharePublic shell ──────────

vi.mock("@/components/ChatMessage", () => ({
  ChatMessage: ({ message }: { message: { id: number; content: string } }) =>
    createElement(
      "div",
      { "data-testid": `mock-message-${String(message.id)}` },
      message.content,
    ),
}));

async function freshSharePublic() {
  vi.resetModules();
  const mod = await import("@/pages/SharePublic");
  return mod.default;
}

function renderShare(Page: React.ComponentType, token: string | null) {
  const path = token === null ? "/share/" : `/share/${token}`;
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/share/:token" element={<Page />} />
        <Route path="/share/" element={<Page />} />
      </Routes>
    </MemoryRouter>,
  );
}

describe("SharePublic", () => {
  beforeEach(() => {
    mockRequest.mockReset();
    cleanup();
  });

  it("renders the not-found state when no token is provided", async () => {
    const Page = await freshSharePublic();
    renderShare(Page, null);
    expect(screen.getByTestId("share-not-found")).toBeTruthy();
    expect(mockRequest).not.toHaveBeenCalled();
  });

  it("renders the not-found state when the backend returns 404", async () => {
    const err: Error & { status?: number } = Object.assign(new Error("not found"), {
      status: 404,
    });
    mockRequest.mockRejectedValue(err);
    const Page = await freshSharePublic();
    renderShare(Page, "abc123");

    await waitFor(() => {
      expect(screen.getByTestId("share-not-found")).toBeTruthy();
    });
    expect(mockRequest).toHaveBeenCalledWith("/api/share/abc123");
  });

  it("renders the title, eyebrow, and message rows on success", async () => {
    mockRequest.mockResolvedValue({
      title: "Demo Chat",
      created_at: "2026-05-30T10:00:00Z",
      messages: [
        {
          id: 1,
          role: "user",
          content: "Hello there",
          reasoning_content: null,
          created_at: "2026-05-30T10:00:00Z",
        },
        {
          id: 2,
          role: "assistant",
          content: "General Kenobi",
          reasoning_content: null,
          created_at: "2026-05-30T10:00:01Z",
        },
      ],
    });
    const Page = await freshSharePublic();
    renderShare(Page, "xyz789");

    await waitFor(() => {
      expect(screen.getByTestId("share-title")).toBeTruthy();
    });
    expect(screen.getByText("Demo Chat")).toBeTruthy();
    expect(screen.getByTestId("share-public-banner").textContent).toMatch(
      /Shared conversation/,
    );
    expect(screen.getByTestId("mock-message-1")).toBeTruthy();
    expect(screen.getByTestId("mock-message-2")).toBeTruthy();
    // Footer attribution link routes home.
    const homeLink = screen.getByRole("link", { name: /LM Chat home/i });
    expect(homeLink.getAttribute("href")).toBe("/");
  });

  it("renders the error state on a non-404 transport failure", async () => {
    mockRequest.mockRejectedValue(new Error("boom"));
    const Page = await freshSharePublic();
    renderShare(Page, "broken");

    await waitFor(() => {
      expect(screen.getByTestId("share-error")).toBeTruthy();
    });
    expect(screen.getByText(/Couldn't load shared chat: boom/)).toBeTruthy();
  });
});
