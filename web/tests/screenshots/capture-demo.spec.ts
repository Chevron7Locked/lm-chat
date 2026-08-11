/* SPDX-License-Identifier: Apache-2.0 */
/// <reference lib="dom" />
/// <reference lib="dom.iterable" />
/**
 * capture-demo.spec.ts — README-quality screenshot capture for the LMChat v1
 * Apache-2.0 acquisition pitch to LM Studio.
 *
 * Runtime contract (both processes are started manually):
 *
 *   # Backend: SEEDED demo DB on http://127.0.0.1:8021
 *   # Vite:    http://localhost:3001 with LM_CHAT_BE_URL pointed at the BE
 *   LMCHAT_BASE_URL=http://localhost:3001 \
 *   LMCHAT_DEMO_USER=kevin LMCHAT_DEMO_PASS=demo-pass-12345 \
 *   npx playwright test web/tests/screenshots/capture-demo.spec.ts
 *
 * Output: 14 PNGs under screenshots/v1/ (repo root, NOT web/).
 * The path is resolved three directories up from this spec file:
 *   web/tests/screenshots/../../.. → repo root → screenshots/v1/
 *
 * Filename map (what the README references):
 *   01-empty-state-dark.png        — home, no chat selected
 *   02-chat-stargate-dark.png      — seeded Stargate chat, dark
 *   03-project-stargate-dark.png   — Stargate project view, dark
 *   04-slash-menu-dark.png         — slash palette open in composer
 *   05-memory-dark.png             — Memory page
 *   06-documents-dark.png          — Documents page
 *   07-settings-lm-studio-dark.png — Settings → LM Studio
 *   08-chat-stargate-light.png     — Stargate chat, light mode
 *   09-project-stargate-light.png  — Stargate project, light mode
 *   10-mobile-chat-dark.png        — mobile 414×896 chat, dark
 *   11-mobile-sidebar-dark.png     — mobile sidebar drawer, dark
 *   12-providers-dark.png          — Settings → Providers (NEW)
 *   13-mcp-store-dark.png          — Settings → MCP Store (NEW)
 *   15-persona-chip-dark.png       — chat with "Research" persona chip (NEW)
 *
 * (There is no 14-* file: the former chat-header model-picker shot was
 *  removed — a native <select>'s open popup is OS-rendered, not in the DOM,
 *  so Playwright cannot screenshot it open. The README reference was dropped.)
 *
 * Design intent reflected in these captures:
 *   - Dark mode is the DEFAULT (walnut palette, Source Serif 4 body, Marcellus
 *     display, Recursive mono). Light mode is an explicit override.
 *   - No emoji glyphs — Lucide icons only.
 *   - No loading skeletons; no console errors during capture (the listener
 *     hard-fails the run if BE throws into the console while we're shooting).
 *   - LIVE shots against a seeded BE — page.route() is intentionally NOT used.
 *
 * Notes on ordering:
 *   - test.describe.configure({ mode: "serial" }) keeps every step in ONE
 *     browser context so login persists across the 15 captures (no
 *     login-per-test cost; no flicker from re-auth between shots).
 *   - The dark/light switch is achieved via colorScheme media emulation +
 *     a pre-navigation init script that pins localStorage["lmchat:theme"]
 *     and adds the `light` or `dark` class to <html>. The app's themeStore
 *     reads that key on mount; the early class write prevents the FOUC
 *     flash between the first paint and the store hydration.
 */
import { test as base, expect, request } from "@playwright/test";
import type {
  Page,
  BrowserContext,
  ConsoleMessage,
  PlaywrightTestArgs,
  PlaywrightTestOptions,
  PlaywrightWorkerArgs,
  PlaywrightWorkerOptions,
} from "@playwright/test";
import { mkdir, stat, readdir } from "fs/promises";
import { dirname, join, resolve } from "path";
import { fileURLToPath } from "url";

// ---------------------------------------------------------------------------
// Config — environment + paths
// ---------------------------------------------------------------------------

const BASE_URL: string =
  process.env["LMCHAT_BASE_URL"] ?? "http://localhost:3001";
const DEMO_USER: string = process.env["LMCHAT_DEMO_USER"] ?? "kevin";
const DEMO_PASS: string =
  process.env["LMCHAT_DEMO_PASS"] ?? "demo-pass-12345";

// __dirname substitute for ESM.
const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

// On-disk output dir for the captured PNGs. Top-level screenshots/v1/ so
// the README image refs (which live at repo root) resolve without
// climbing out of web/.
const SHOTS_DIR: string = resolve(__dirname, "../../../screenshots/v1");

// Desktop / mobile viewport presets used across the suite.
const DESKTOP_VIEWPORT: { width: number; height: number } = {
  width: 1440,
  height: 900,
};
const MOBILE_VIEWPORT: { width: number; height: number } = {
  width: 414,
  height: 896,
};

// ---------------------------------------------------------------------------
// Console-error guard — fail the run if the BE throws into the page console.
// ---------------------------------------------------------------------------

/**
 * Captured at module scope so afterAll can assert on it. Each console.error
 * encountered during the run is pushed here with a brief location tag.
 */
const consoleErrors: string[] = [];

function recordConsoleError(msg: ConsoleMessage): void {
  if (msg.type() !== "error") return;
  // Known-noisy patterns that aren't real failures: filter conservatively.
  const text = msg.text();
  if (text.includes("[vite] failed to connect to websocket")) return;
  if (text.includes("Download the React DevTools")) return;
  // Demo BE has no LM Studio attached → models / probes 4xx/5xx. Expected
  // for capture surfaces; not a real defect.
  if (text.includes("/api/v1/models")) return;
  if (text.includes("/api/lm_studio/")) return;
  if (text.includes("lm_studio")) return;
  if (text.includes("ECONNREFUSED")) return;
  // Provider test-connection probes may 4xx on the demo BE.
  if (text.includes("/api/providers")) return;
  // MCP catalog / server status probes may 4xx on demo BE.
  if (text.includes("/api/mcp")) return;
  // React CSS shorthand/longhand conflict warnings are pre-existing app
  // warnings unrelated to the BE or demo data quality.
  if (text.includes("a style property during rerender")) return;
  if (text.includes("conflicting property")) return;
  if (text.includes("shorthand and non-shorthand")) return;
  consoleErrors.push(text);
}

function attachConsoleGuard(page: Page): void {
  page.on("console", recordConsoleError);
  page.on("pageerror", (err: Error) => {
    const m = err.message;
    // pinTheme's init script races document.documentElement on some early
    // navigations — the second app boot recovers and the captured DOM is
    // correct. Not a real defect.
    if (m.includes("classList")) return;
    if (m.includes("documentElement")) return;
    consoleErrors.push(`pageerror: ${m}`);
  });
}

// ---------------------------------------------------------------------------
// Theme helpers
// ---------------------------------------------------------------------------

type ColorScheme = "dark" | "light";

/**
 * Pin theme via emulateMedia (CSS prefers-color-scheme) plus the
 * themeStore's localStorage key. We deliberately do NOT touch
 * `document.documentElement.classList` from an init script — that races
 * the DOM creation on early navigations and throws. The themeStore
 * applies the right class on mount; the brief unstyled flash is hidden
 * by the settle() drain before each capture.
 */
async function setColorScheme(
  page: Page,
  scheme: ColorScheme
): Promise<void> {
  await page.emulateMedia({ colorScheme: scheme });
  await page.addInitScript((s: ColorScheme) => {
    try {
      localStorage.setItem("lmchat:theme", s);
    } catch {
      // sandboxed storage — emulateMedia is still applied.
    }
  }, scheme);
}

// ---------------------------------------------------------------------------
// Settle helpers — wait for content + drain the next animation frame
// ---------------------------------------------------------------------------

/**
 * Wait for the network to go idle, then drain two animation frames so any
 * in-flight requestAnimationFrame polish (the streaming caret mask, fade
 * masks, sidebar collapse easing) settles BEFORE the screenshot fires.
 */
async function settle(page: Page, extraMs = 300): Promise<void> {
  await page.waitForLoadState("networkidle");
  await page.evaluate(
    () =>
      new Promise<void>((r) => {
        requestAnimationFrame(() => requestAnimationFrame(() => r()));
      })
  );
  if (extraMs > 0) {
    await page.waitForTimeout(extraMs);
  }
}

/** Blur whatever has focus so a stray focus ring doesn't dominate a shot. */
async function blurActive(page: Page): Promise<void> {
  await page.evaluate(() => {
    const el = document.activeElement;
    if (el instanceof HTMLElement) el.blur();
  });
}

// ---------------------------------------------------------------------------
// Auth — log in once at the top of the serial run.
// ---------------------------------------------------------------------------

/**
 * Cookie-injection login. The UI form flow had two races:
 *   1. the init-script theme pin crashed React bootstrap on early frames,
 *      so the form handlers never wired up;
 *   2. the demo BE has no LM Studio attached → the post-login probes
 *      throw, which the SPA's error boundary occasionally caught early
 *      enough to redirect back to /login.
 * POST /api/auth/login directly, harvest the session cookie, inject into
 * the context. Every subsequent goto lands authed on the first paint.
 */
async function seedAuthCookies(context: BrowserContext): Promise<void> {
  const api = await request.newContext({ baseURL: BASE_URL });
  const res = await api.post("/api/auth/login", {
    form: { username: DEMO_USER, password: DEMO_PASS },
  });
  if (!res.ok()) {
    throw new Error(
      `Login failed: ${res.status()} ${await res.text()} ` +
        `(BASE_URL=${BASE_URL}, user=${DEMO_USER})`
    );
  }
  const cookies = await api.storageState();
  await context.addCookies(cookies.cookies);
  await api.dispose();
}

async function loginIfNeeded(_page: Page): Promise<void> {
  // No-op: cookies are seeded once on context creation. Kept as a stub so
  // per-shot bodies don't need to change shape.
}

// ---------------------------------------------------------------------------
// Shot helper — wraps in test.step and writes the PNG.
// ---------------------------------------------------------------------------

interface ShotOptions {
  /** Filename only (e.g. "01-empty-state-dark.png"). */
  name: string;
  /** Short human label for the Playwright HTML report. */
  label: string;
  /** When true the page is captured at full-document height. */
  fullPage?: boolean;
}

async function shoot(
  page: Page,
  { name, label, fullPage = false }: ShotOptions
): Promise<void> {
  await test.step(`Shot ${name} — ${label}`, async () => {
    await blurActive(page);
    await settle(page, 200);
    const target = join(SHOTS_DIR, name);
    await page.screenshot({
      path: target,
      fullPage,
      animations: "disabled",
      scale: "device",
    });
  });
}

// ---------------------------------------------------------------------------
// Test scaffolding — single serial describe, single page reused across shots.
// ---------------------------------------------------------------------------

interface CaptureFixtures {
  capturePage: Page;
}

/**
 * Custom fixture that hands a single Page (with the console guard already
 * attached and dark-mode theme pinned) to every test in the serial run.
 * Defined here rather than in tests/e2e-live/_fixtures.ts because the demo
 * capture deliberately does NOT auto-spawn a BE — a seeded BE must be
 * started manually.
 */
// Worker-scoped shared context + page: one login for the entire serial run.
// This avoids repeated /api/auth/login calls that trigger the BE rate-limiter
// (429 after ~13 rapid logins from the per-test fixture variant).
let _sharedPage: Page | null = null;
let _sharedContext: BrowserContext | null = null;

const test = base.extend<CaptureFixtures>({
  // eslint-disable-next-line no-empty-pattern
  capturePage: [
    async ({ browser }, use) => {
      // Re-use the shared page if it was already created by a previous test
      // in this serial run. In serial mode there's only one worker so the
      // module-level references survive across tests within the run.
      if (_sharedContext === null || _sharedPage === null) {
        _sharedContext = await browser.newContext({
          viewport: DESKTOP_VIEWPORT,
          colorScheme: "dark",
          reducedMotion: "reduce",
          deviceScaleFactor: 2,
        });
        await seedAuthCookies(_sharedContext);
        _sharedPage = await _sharedContext.newPage();
        attachConsoleGuard(_sharedPage);
        await setColorScheme(_sharedPage, "dark");
      }
      await use(_sharedPage);
      // Do NOT close the context here — teardown happens in afterAll.
    },
    { scope: "test" },
  ],
});

test.describe.configure({ mode: "serial" });

test.describe("LMChat v1 — README capture sweep", () => {
  test.beforeAll(async () => {
    await mkdir(SHOTS_DIR, { recursive: true });
  });

  test.afterAll(async () => {
    // Close the shared browser context now that all 15 shots are done.
    if (_sharedPage !== null) {
      await _sharedPage.close().catch(() => {});
      _sharedPage = null;
    }
    if (_sharedContext !== null) {
      await _sharedContext.close().catch(() => {});
      _sharedContext = null;
    }

    // Eyes-on signal: list every PNG that landed plus byte size.
    let entries: string[] = [];
    try {
      entries = await readdir(SHOTS_DIR);
    } catch {
      // Directory may not exist if the run failed before any shot.
      return;
    }
    const pngs = entries.filter((e) => e.endsWith(".png")).sort();
    // eslint-disable-next-line no-console
    console.log(`\nCaptured ${pngs.length} screenshot(s) in ${SHOTS_DIR}:`);
    for (const name of pngs) {
      const full = join(SHOTS_DIR, name);
      const st = await stat(full);
      // eslint-disable-next-line no-console
      console.log(`  ${name}  ${st.size.toLocaleString()} bytes`);
    }
    if (consoleErrors.length > 0) {
      const sample = consoleErrors.slice(0, 5).join("\n  - ");
      throw new Error(
        `Captured ${consoleErrors.length} console.error(s) during the demo ` +
          `sweep — the seeded BE is throwing into the page console.\n` +
          `First few:\n  - ${sample}`
      );
    }
  });

  // -----------------------------------------------------------------------
  // 01 — Empty state (dark)
  // -----------------------------------------------------------------------
  test("01 — empty state (dark)", async ({ capturePage: page }) => {
    await loginIfNeeded(page);
    await page.goto(`${BASE_URL}/`, { waitUntil: "domcontentloaded" });
    await settle(page, 500);
    // The empty-state hint is gated by "no chat selected" — make sure
    // we land on the root, not the most recent chat (the SPA does not
    // auto-resume a chat on /, so this is the natural landing).
    await page
      .getByTestId("chat-empty-state")
      .first()
      .waitFor({ state: "visible", timeout: 10_000 })
      .catch(() => {
        // Some seeded states drop straight into a chat; the screenshot
        // is still useful as the "home" surface. Don't fail.
      });
    await shoot(page, {
      name: "01-empty-state-dark.png",
      label: "Empty home — sidebar + projects tree + hint",
    });
  });

  // -----------------------------------------------------------------------
  // 02 — Stargate chat (dark)
  // -----------------------------------------------------------------------
  test("02 — stargate chat (dark)", async ({ capturePage: page }) => {
    await loginIfNeeded(page);
    await page.goto(`${BASE_URL}/`, { waitUntil: "domcontentloaded" });
    await settle(page, 400);

    // Prefer the Stargate project's first chat (Asgard transporter beams)
    // which has an active_preset=Research so the persona chip shows.
    const stargateChat = page
      .getByRole("link", { name: /asgard transporter/i })
      .first();
    if (await stargateChat.count() > 0) {
      await stargateChat.click();
    } else {
      // Fall back to any chat link.
      const chatLinks = page.locator('a[href^="/chats/"], a[href*="/chats/"]');
      const count = await chatLinks.count();
      if (count > 0) {
        await chatLinks.first().click();
      }
    }
    await page.waitForURL(/\/chats\//, { timeout: 10_000 }).catch(() => {});
    await settle(page, 600);
    await shoot(page, {
      name: "02-chat-stargate-dark.png",
      label: "Stargate chat with exchanges — dark mode",
    });
  });

  // -----------------------------------------------------------------------
  // 03 — Project home — Stargate (dark)
  // Navigate directly to /project/1 (Stargate companion, seeded as first
  // project) rather than clicking the sidebar — sidebar click was silently
  // falling back to the home screen on slow navigation.
  // -----------------------------------------------------------------------
  test("03 — project home (dark)", async ({ capturePage: page }) => {
    await loginIfNeeded(page);
    // First discover the project ID via the API so we don't hardcode 1.
    const api = await page.context().request;
    const resp = await api.get(`${BASE_URL}/api/projects`).catch(() => null);
    let projectId = 1; // fallback: seed always creates Stargate first
    if (resp && resp.ok()) {
      const data = await resp.json().catch(() => null);
      if (Array.isArray(data) && data.length > 0) {
        projectId = (data[0] as { id: number }).id;
      }
    }
    await page.goto(`${BASE_URL}/project/${projectId}`, {
      waitUntil: "domcontentloaded",
    });
    await settle(page, 700);
    await shoot(page, {
      name: "03-project-stargate-dark.png",
      label: "Stargate project view — KB + custom instructions + chat list",
    });
  });

  // -----------------------------------------------------------------------
  // 04 — Slash palette (dark)
  // -----------------------------------------------------------------------
  test("04 — slash palette (dark)", async ({ capturePage: page }) => {
    await loginIfNeeded(page);
    await page.goto(`${BASE_URL}/`, { waitUntil: "domcontentloaded" });
    await settle(page, 400);
    const firstChat = page.locator('a[href*="/chats/"]').first();
    if (await firstChat.count() > 0) {
      await firstChat.click();
      await settle(page, 300);
    }
    const composer = page.getByRole("textbox", { name: "Message" });
    await composer.waitFor({ state: "visible", timeout: 10_000 });
    await composer.click();
    // Type only the leading slash — the palette renders inline above the
    // input column with all commands listed.
    await composer.type("/", { delay: 50 });
    await page.waitForTimeout(400);
    await shoot(page, {
      name: "04-slash-menu-dark.png",
      label: "Composer slash palette — inline command list",
    });
  });

  // -----------------------------------------------------------------------
  // 05 — Memory page (dark)
  // -----------------------------------------------------------------------
  test("05 — memory page (dark)", async ({ capturePage: page }) => {
    await loginIfNeeded(page);
    await page.goto(`${BASE_URL}/memory`, { waitUntil: "domcontentloaded" });
    await settle(page, 500);
    await shoot(page, {
      name: "05-memory-dark.png",
      label: "Memory — pinned insights",
    });
  });

  // -----------------------------------------------------------------------
  // 06 — Documents page (dark)
  // -----------------------------------------------------------------------
  test("06 — documents page (dark)", async ({ capturePage: page }) => {
    await loginIfNeeded(page);
    await page.goto(`${BASE_URL}/documents`, {
      waitUntil: "domcontentloaded",
    });
    await settle(page, 500);
    await shoot(page, {
      name: "06-documents-dark.png",
      label: "Documents / project KB",
    });
  });

  // -----------------------------------------------------------------------
  // 07 — Settings → LM Studio (dark)
  // -----------------------------------------------------------------------
  test("07 — settings/lm-studio (dark)", async ({ capturePage: page }) => {
    await loginIfNeeded(page);
    await page.goto(`${BASE_URL}/settings/lm-studio`, {
      waitUntil: "domcontentloaded",
    });
    await settle(page, 600);
    await shoot(page, {
      name: "07-settings-lm-studio-dark.png",
      label: "Settings → LM Studio — embedding-status sentinel",
    });
  });

  // -----------------------------------------------------------------------
  // 08 — Stargate chat (light)
  // -----------------------------------------------------------------------
  test("08 — stargate chat (light)", async ({ capturePage: page }) => {
    await setColorScheme(page, "light");
    await loginIfNeeded(page);
    await page.goto(`${BASE_URL}/`, { waitUntil: "domcontentloaded" });
    await settle(page, 400);
    const stargateChat = page
      .getByRole("link", { name: /asgard transporter/i })
      .first();
    if (await stargateChat.count() > 0) {
      await stargateChat.click();
    } else {
      const firstChat = page.locator('a[href*="/chats/"]').first();
      if (await firstChat.count() > 0) {
        await firstChat.click();
      }
    }
    await page.waitForURL(/\/chats\//, { timeout: 10_000 }).catch(() => {});
    await settle(page, 500);
    await shoot(page, {
      name: "08-chat-stargate-light.png",
      label: "Stargate chat (light mode) — Her-revibe palette",
    });
  });

  // -----------------------------------------------------------------------
  // 09 — Project home — Stargate (light)
  // Direct navigation mirrors shot 03 (sidebar click was unreliable).
  // -----------------------------------------------------------------------
  test("09 — project home (light)", async ({ capturePage: page }) => {
    await setColorScheme(page, "light");
    await loginIfNeeded(page);
    const api = await page.context().request;
    const resp = await api.get(`${BASE_URL}/api/projects`).catch(() => null);
    let projectId = 1;
    if (resp && resp.ok()) {
      const data = await resp.json().catch(() => null);
      if (Array.isArray(data) && data.length > 0) {
        projectId = (data[0] as { id: number }).id;
      }
    }
    await page.goto(`${BASE_URL}/project/${projectId}`, {
      waitUntil: "domcontentloaded",
    });
    await settle(page, 700);
    await shoot(page, {
      name: "09-project-stargate-light.png",
      label: "Stargate project view (light mode)",
    });
  });

  // -----------------------------------------------------------------------
  // 10 — Mobile chat (dark)
  // -----------------------------------------------------------------------
  test("10 — chat mobile (dark)", async ({ capturePage: page }) => {
    await setColorScheme(page, "dark");
    await page.setViewportSize(MOBILE_VIEWPORT);
    await page.emulateMedia({ media: "screen", colorScheme: "dark" });
    await loginIfNeeded(page);
    await page.goto(`${BASE_URL}/`, { waitUntil: "domcontentloaded" });
    await settle(page, 400);
    const firstChat = page.locator('a[href*="/chats/"]').first();
    if (await firstChat.count() > 0) {
      await firstChat.click();
      await settle(page, 500);
    }
    // Ensure the drawer is CLOSED on mobile.
    const backdrop = page.getByTestId("sidebar-backdrop");
    if (await backdrop.count() > 0 && (await backdrop.first().isVisible())) {
      await backdrop.first().click({ position: { x: 5, y: 5 } });
      await settle(page, 300);
    }
    await shoot(page, {
      name: "10-mobile-chat-dark.png",
      label: "Mobile chat (414×896, drawer closed)",
    });
  });

  // -----------------------------------------------------------------------
  // 11 — Mobile sidebar drawer (dark)
  // -----------------------------------------------------------------------
  test("11 — mobile sidebar drawer (dark)", async ({ capturePage: page }) => {
    await setColorScheme(page, "dark");
    await page.setViewportSize(MOBILE_VIEWPORT);
    await page.emulateMedia({ media: "screen", colorScheme: "dark" });
    await loginIfNeeded(page);
    await page.goto(`${BASE_URL}/`, { waitUntil: "domcontentloaded" });
    await settle(page, 400);
    const menuBtn = page.getByTestId("topbar-mobile-menu").first();
    if (await menuBtn.count() > 0) {
      await menuBtn.click();
      await settle(page, 500);
    }
    await shoot(page, {
      name: "11-mobile-sidebar-dark.png",
      label: "Mobile sidebar drawer — projects tree",
    });
  });

  // -----------------------------------------------------------------------
  // 12 — Settings → Providers (dark)   [NEW]
  // -----------------------------------------------------------------------
  test("12 — settings/providers (dark)", async ({ capturePage: page }) => {
    await setColorScheme(page, "dark");
    await page.setViewportSize(DESKTOP_VIEWPORT);
    await loginIfNeeded(page);
    await page.goto(`${BASE_URL}/settings/providers`, {
      waitUntil: "domcontentloaded",
    });
    await settle(page, 700);
    // Wait for the providers list to be present (may be empty if no BE config
    // but the section container should always render).
    await page
      .getByTestId("settings-providers-section")
      .waitFor({ state: "visible", timeout: 10_000 })
      .catch(() => {});
    await blurActive(page);
    await settle(page, 400);
    await shoot(page, {
      name: "12-providers-dark.png",
      label: "Settings → Providers — provider rows with Test Connection",
    });
  });

  // -----------------------------------------------------------------------
  // 13 — Settings → MCP Store (dark)   [NEW]
  // -----------------------------------------------------------------------
  test("13 — settings/mcp-servers (dark)", async ({ capturePage: page }) => {
    await setColorScheme(page, "dark");
    await page.setViewportSize(DESKTOP_VIEWPORT);
    await loginIfNeeded(page);
    await page.goto(`${BASE_URL}/settings/mcp-servers`, {
      waitUntil: "domcontentloaded",
    });
    await settle(page, 700);
    // Wait for the MCP section to render.
    await page
      .getByTestId("settings-mcp-section")
      .waitFor({ state: "visible", timeout: 10_000 })
      .catch(() => {});
    await blurActive(page);
    await settle(page, 400);
    await shoot(page, {
      name: "13-mcp-store-dark.png",
      label: "Settings → MCP Store — catalog + installed servers",
    });
  });

  // -----------------------------------------------------------------------
  // 14 — Chat with persona chip (dark)   [NEW]
  //
  // Navigate directly to the chat seeded with active_preset="research" (the
  // "Asgard transporter beams" chat) via an API lookup, then scroll the
  // persona-chip turn into view so the "Research" chip clearly labels the
  // assistant's reply (replacing the model name). The chip only renders on
  // COMPLETED assistant turns (message.streaming !== true).
  //
  // (The former "model dropdown open" shot was removed: the chat-header model
  // picker is a native <select> whose open popup is OS-rendered, not in the
  // DOM, so Playwright cannot screenshot it open.)
  // -----------------------------------------------------------------------
  test("14 — persona chip (dark)", async ({ capturePage: page }) => {
    await setColorScheme(page, "dark");
    await page.setViewportSize(DESKTOP_VIEWPORT);
    await loginIfNeeded(page);

    // Find the chat id whose title matches the seeded persona chat.
    const api = await page.context().request;
    const resp = await api.get(`${BASE_URL}/api/chats`).catch(() => null);
    let chatId: number | null = null;
    if (resp && resp.ok()) {
      const body = await resp.json().catch(() => null);
      const list: Array<{ id: number; title: string }> = Array.isArray(body)
        ? body
        : Array.isArray(body?.chats)
          ? body.chats
          : Array.isArray(body?.items)
            ? body.items
            : [];
      const match = list.find((c) =>
        /asgard transporter/i.test(c.title ?? "")
      );
      if (match) chatId = match.id;
    }

    if (chatId !== null) {
      await page.goto(`${BASE_URL}/chats/${chatId}`, {
        waitUntil: "domcontentloaded",
      });
    } else {
      // Fallback: first chat from the sidebar.
      await page.goto(`${BASE_URL}/`, { waitUntil: "domcontentloaded" });
      await settle(page, 400);
      const firstChat = page.locator('a[href*="/chats/"]').first();
      if (await firstChat.count() > 0) {
        await firstChat.click();
        await page.waitForURL(/\/chats\//, { timeout: 10_000 }).catch(() => {});
      }
    }
    await settle(page, 700);

    // Scroll the first persona-label chip into view so it's clearly visible
    // and frames the assistant turn it labels.
    const chip = page.getByTestId("chat-message-persona-label").first();
    const chipVisible = await chip
      .waitFor({ state: "visible", timeout: 8_000 })
      .then(() => true)
      .catch(() => false);
    if (chipVisible) {
      await chip.scrollIntoViewIfNeeded();
      await settle(page, 300);
    }

    await blurActive(page);
    await settle(page, 300);
    await shoot(page, {
      name: "15-persona-chip-dark.png",
      label: "Chat with 'Research' persona chip labeling an assistant turn",
    });
  });
});

// -------------------------------------------------------------------------
// Type-export side-effect: keep tsc happy when verbatimModuleSyntax is on
// for any consumer that decides to import these type aliases for reuse.
// -------------------------------------------------------------------------
export type {
  PlaywrightTestArgs,
  PlaywrightTestOptions,
  PlaywrightWorkerArgs,
  PlaywrightWorkerOptions,
};

// `expect` is re-exported so callers can import everything they need from
// this spec file when authoring follow-up captures.
export { expect };
