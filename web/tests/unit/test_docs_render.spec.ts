/**
 * Unit tests for docs-render.ts
 *
 * Covers: slugFromPath, orderFromSlug, extractTitle, buildDocList,
 * rewriteDocLinks, and renderDoc.
 */
import { describe, it, expect } from "vitest";
import {
  slugFromPath,
  orderFromSlug,
  extractTitle,
  buildDocList,
  rewriteDocLinks,
  renderDoc,
  GROUP_DEFS,
} from "@/lib/docs-render";

// ─── slugFromPath ─────────────────────────────────────────────────────────────

describe("slugFromPath", () => {
  it("strips the .md extension and returns just the filename", () => {
    expect(slugFromPath("../../../guide/02-chatting.md")).toBe("02-chatting");
  });

  it("handles a bare filename (no directory prefix)", () => {
    expect(slugFromPath("00-quickstart.md")).toBe("00-quickstart");
  });

  it("handles a path with multiple slashes", () => {
    expect(slugFromPath("a/b/c/15-api-reference.md")).toBe("15-api-reference");
  });

  it("falls back gracefully when there is no extension", () => {
    expect(slugFromPath("14-architecture")).toBe("14-architecture");
  });
});

// ─── orderFromSlug ────────────────────────────────────────────────────────────

describe("orderFromSlug", () => {
  it("extracts the leading numeric prefix", () => {
    expect(orderFromSlug("02-chatting")).toBe(2);
    expect(orderFromSlug("00-quickstart")).toBe(0);
    expect(orderFromSlug("15-api-reference")).toBe(15);
  });

  it("returns MAX_SAFE_INTEGER when there is no numeric prefix", () => {
    expect(orderFromSlug("no-prefix-here")).toBe(Number.MAX_SAFE_INTEGER);
    expect(orderFromSlug("")).toBe(Number.MAX_SAFE_INTEGER);
  });
});

// ─── extractTitle ─────────────────────────────────────────────────────────────

describe("extractTitle", () => {
  it("returns the text of the first H1 line", () => {
    const md = "# Hello World\n\nSome content.";
    expect(extractTitle(md)).toBe("Hello World");
  });

  it("strips trailing `#` characters (ATX-style closed heading)", () => {
    const md = "# My Title ##\n";
    expect(extractTitle(md)).toBe("My Title");
  });

  it("falls back to the provided slug when no H1 is found", () => {
    const md = "## No H1 here\n\nJust H2.";
    expect(extractTitle(md, "my-slug")).toBe("my-slug");
  });

  it("returns empty string fallback when slug is not provided and no H1", () => {
    expect(extractTitle("no heading")).toBe("");
  });
});

// ─── buildDocList ─────────────────────────────────────────────────────────────

describe("buildDocList", () => {
  const FIXTURE: Record<string, string> = {
    "../../../guide/00-quickstart.md": "# Quickstart\nContent.",
    "../../../guide/02-chatting.md": "# Chatting\nContent.",
    "../../../guide/14-architecture.md": "# Architecture\nContent.",
    "../../../guide/10-settings-and-admin.md":
      "# Settings and Admin\nContent.",
  };

  it("returns one DocMeta per file, sorted by order", () => {
    const { docs } = buildDocList(FIXTURE);
    expect(docs).toHaveLength(4);
    expect(docs[0]?.slug).toBe("00-quickstart");
    expect(docs[1]?.slug).toBe("02-chatting");
    expect(docs[2]?.slug).toBe("10-settings-and-admin");
    expect(docs[3]?.slug).toBe("14-architecture");
  });

  it("assigns correct group keys", () => {
    const { docs } = buildDocList(FIXTURE);
    const bySlug = Object.fromEntries(docs.map((d) => [d.slug, d]));
    expect(bySlug["00-quickstart"]?.group).toBe("get-started");
    expect(bySlug["02-chatting"]?.group).toBe("using-lmchat");
    expect(bySlug["10-settings-and-admin"]?.group).toBe("administration");
    expect(bySlug["14-architecture"]?.group).toBe("technical");
  });

  it("extracts titles from H1", () => {
    const { docs } = buildDocList(FIXTURE);
    const bySlug = Object.fromEntries(docs.map((d) => [d.slug, d]));
    expect(bySlug["00-quickstart"]?.title).toBe("Quickstart");
    expect(bySlug["02-chatting"]?.title).toBe("Chatting");
  });

  it("returns groups in GROUP_DEFS display order (no empty groups)", () => {
    const { groups } = buildDocList(FIXTURE);
    // Fixture has: get-started(00), using-lmchat(02), administration(10), technical(14)
    // reference group is absent (no 11,12,13 in fixture)
    expect(groups.map((g) => g.key)).toEqual([
      "get-started",
      "using-lmchat",
      "administration",
      "technical",
    ]);
  });

  it("handles an empty file map without throwing", () => {
    const { docs, groups } = buildDocList({});
    expect(docs).toHaveLength(0);
    expect(groups).toHaveLength(0);
  });
});

// ─── GROUP_DEFS coverage ──────────────────────────────────────────────────────

describe("GROUP_DEFS", () => {
  it("covers LMChat orders 00–15 with no gaps or overlaps", () => {
    const allOrders = GROUP_DEFS.flatMap((g) => g.orders);
    // No duplicates
    expect(new Set(allOrders).size).toBe(allOrders.length);
    // Covers 0 through 15
    for (let i = 0; i <= 15; i++) {
      expect(allOrders).toContain(i);
    }
  });
});

// ─── rewriteDocLinks ─────────────────────────────────────────────────────────

describe("rewriteDocLinks", () => {
  it("rewrites NN-name.md hrefs to /docs/NN-name", () => {
    const html = `<a href="02-chatting.md">link</a>`;
    expect(rewriteDocLinks(html)).toBe(`<a href="/docs/02-chatting">link</a>`);
  });

  it("rewrites ./NN-name.md hrefs correctly", () => {
    const html = `<a href="./03-personas-and-modes.md">link</a>`;
    expect(rewriteDocLinks(html)).toBe(
      `<a href="/docs/03-personas-and-modes">link</a>`,
    );
  });

  it("preserves #anchor fragments", () => {
    const html = `<a href="05-mcp-and-tools.md#section">link</a>`;
    expect(rewriteDocLinks(html)).toBe(
      `<a href="/docs/05-mcp-and-tools#section">link</a>`,
    );
  });

  it("humanizes filename-as-link-text using the titles map", () => {
    const html = `<a href="00-quickstart.md">00-quickstart.md</a>`;
    const titles = { "00-quickstart": "Quickstart" };
    expect(rewriteDocLinks(html, titles)).toBe(
      `<a href="/docs/00-quickstart">Quickstart</a>`,
    );
  });

  it("strips .md from filename text when title is not in the map", () => {
    const html = `<a href="00-quickstart.md">00-quickstart.md</a>`;
    expect(rewriteDocLinks(html, {})).toBe(
      `<a href="/docs/00-quickstart">00-quickstart</a>`,
    );
  });

  it("preserves custom (non-filename) link text verbatim", () => {
    const html = `<a href="02-chatting.md">Read the chatting guide</a>`;
    expect(rewriteDocLinks(html)).toBe(
      `<a href="/docs/02-chatting">Read the chatting guide</a>`,
    );
  });

  it("leaves external https:// links untouched", () => {
    const html = `<a href="https://example.com">external</a>`;
    expect(rewriteDocLinks(html)).toBe(html);
  });

  it("leaves in-page #anchor links untouched", () => {
    const html = `<a href="#section">jump</a>`;
    expect(rewriteDocLinks(html)).toBe(html);
  });

  it("leaves already-absolute /docs/... links untouched", () => {
    const html = `<a href="/docs/02-chatting">already absolute</a>`;
    expect(rewriteDocLinks(html)).toBe(html);
  });
});

// ─── renderDoc ────────────────────────────────────────────────────────────────

describe("renderDoc", () => {
  it("converts markdown to HTML with an H1", () => {
    const { html } = renderDoc("# Hello\n\nParagraph.");
    expect(html).toContain("<h1");
    expect(html).toContain("Hello");
    expect(html).toContain("<p>");
  });

  it("collects H2 and H3 headings in order", () => {
    const md = "# Title\n\n## Alpha\n\n### Beta\n\n## Gamma\n";
    const { headings } = renderDoc(md);
    expect(headings).toHaveLength(3);
    expect(headings[0]).toMatchObject({ depth: 2, text: "Alpha" });
    expect(headings[1]).toMatchObject({ depth: 3, text: "Beta" });
    expect(headings[2]).toMatchObject({ depth: 2, text: "Gamma" });
  });

  it("does NOT include H1 in the headings array", () => {
    const { headings } = renderDoc("# Page Title\n\n## Section\n");
    expect(headings.every((h) => h.depth !== 1)).toBe(true);
  });

  it("assigns stable slug-based ids to headings", () => {
    const { html } = renderDoc("## Hello World\n");
    expect(html).toContain('id="hello-world"');
  });

  it("deduplicates identical heading ids with a numeric suffix", () => {
    const md = "## Foo\n\n## Foo\n\n## Foo\n";
    const { headings } = renderDoc(md);
    expect(headings[0]?.id).toBe("foo");
    expect(headings[1]?.id).toBe("foo-1");
    expect(headings[2]?.id).toBe("foo-2");
  });

  it("strips <hr> elements from the output", () => {
    const md = "## Section\n\n---\n\nParagraph.\n";
    const { html } = renderDoc(md);
    expect(html).not.toMatch(/<hr/i);
  });

  it("rewrites internal doc links", () => {
    const md = "[Chatting](02-chatting.md)\n";
    const { html } = renderDoc(md, { "02-chatting": "Chatting" });
    expect(html).toContain('href="/docs/02-chatting"');
  });

  it("is pure: calling it twice on the same input gives identical results", () => {
    const md = "# Title\n\n## Section\n\nParagraph.\n";
    const r1 = renderDoc(md);
    const r2 = renderDoc(md);
    expect(r1.html).toBe(r2.html);
    expect(r1.headings).toEqual(r2.headings);
  });

  it("renders code fences (including mermaid) as <pre><code> blocks", () => {
    const md = "```mermaid\ngraph TD;\n  A-->B;\n```\n";
    const { html } = renderDoc(md);
    expect(html).toContain("<pre>");
    expect(html).toContain("<code");
    expect(html).toContain("graph TD");
  });
});
