/* SPDX-License-Identifier: Apache-2.0 */
/**
 * docs-render.ts — pure helpers for the in-app Documentation section (/docs).
 *
 * The guide markdown lives in the repo-root `guide/` directory and is bundled
 * at build time via a Vite `import.meta.glob` (see pages/Docs.tsx). These
 * helpers turn that raw markdown into the data the route needs:
 *
 *   - slug / title / order extraction (for the doc list + nav)
 *   - group assignment (for the grouped index + sidebar)
 *   - markdown → HTML rendering (via `marked`)
 *   - internal `NN-name.md` link rewriting → `/docs/NN-name` app routes
 *
 * Everything here is pure and dependency-light so it can be unit-tested without
 * a browser or a server. The route file owns the glob and the React plumbing;
 * this module owns the logic.
 *
 * Ported from the EMS-FE reference implementation (app/src/lib/content/docs-render.ts),
 * adapted to LM Chat's IA and guide file set (00–15).
 */

import { Marked, type Tokens } from "marked";

/** A single documentation page, derived from one markdown file. */
export interface DocMeta {
  /** URL slug — the filename without the `.md` extension, e.g. `02-chatting`. */
  slug: string;
  /** Display title — the text of the first `# H1` line, falling back to the slug. */
  title: string;
  /** Sort order — the leading `NN-` numeric prefix, or a large number if absent. */
  order: number;
  /** Group key this doc belongs to (see GROUP_DEFS). */
  group: GroupKey;
}

/** A heading extracted from a rendered article, for the "On this page" TOC. */
export interface DocHeading {
  /** Heading level (2 = H2, 3 = H3). H1 is the page title and is excluded. */
  depth: number;
  /** Stable anchor id assigned to the heading element (and deduped). */
  id: string;
  /** Plain-text heading label (markup stripped). */
  text: string;
}

/** A group of docs for display in the index + sidebar. */
export interface DocGroup {
  key: GroupKey;
  label: string;
  docs: DocMeta[];
}

export type GroupKey =
  | "get-started"
  | "using-lmchat"
  | "administration"
  | "reference"
  | "technical";

/**
 * Group definitions, in display order. Each entry maps a label to the set of
 * order-prefixes it owns.
 *
 * LM Chat IA (files 00–15):
 *   - Get started:      00, 01
 *   - Using LM Chat:     02, 03, 04, 05, 06, 07, 08, 09
 *   - Administration:   10
 *   - Reference:        11, 12, 13
 *   - Technical:        14, 15
 */
export const GROUP_DEFS: readonly {
  key: GroupKey;
  label: string;
  orders: number[];
}[] = [
  { key: "get-started", label: "Get started", orders: [0, 1] },
  {
    key: "using-lmchat",
    label: "Using LM Chat",
    orders: [2, 3, 4, 5, 6, 7, 8, 9],
  },
  { key: "administration", label: "Administration", orders: [10] },
  { key: "reference", label: "Reference", orders: [11, 12, 13] },
  { key: "technical", label: "Technical", orders: [14, 15] },
];

/**
 * Derive a slug from a file path or filename.
 * `../../../guide/02-chatting.md` → `02-chatting`
 */
export function slugFromPath(path: string): string {
  const base = path.split("/").pop() ?? path;
  return base.replace(/\.md$/i, "");
}

/**
 * Extract the order number from a slug's leading `NN-` prefix.
 * `02-chatting` → 2. Missing/non-numeric prefix → Number.MAX_SAFE_INTEGER
 * (so unprefixed files sort last rather than colliding at 0).
 */
export function orderFromSlug(slug: string): number {
  const m = /^(\d+)/.exec(slug);
  return m ? Number(m[1]) : Number.MAX_SAFE_INTEGER;
}

/**
 * Extract the page title from markdown: the text of the first `# H1` line.
 * Falls back to the provided slug if no H1 is present.
 */
export function extractTitle(markdown: string, fallbackSlug = ""): string {
  for (const line of markdown.split("\n")) {
    const m = /^#\s+(.+?)\s*#*\s*$/.exec(line);
    if (m?.[1]) return m[1].trim();
  }
  return fallbackSlug;
}

/** Pick the group a given order number belongs to (defaults to reference if unmatched). */
function groupForOrder(order: number): GroupKey {
  for (const g of GROUP_DEFS) {
    if (g.orders.includes(order)) return g.key;
  }
  return "reference";
}

/**
 * Build the sorted, grouped doc list from a glob map of `{ path: rawMarkdown }`.
 * Returns both the flat sorted list (for prev/next) and the grouped list (for nav).
 */
export function buildDocList(files: Record<string, string>): {
  docs: DocMeta[];
  groups: DocGroup[];
} {
  const docs: DocMeta[] = Object.entries(files).map(([path, raw]) => {
    const slug = slugFromPath(path);
    const order = orderFromSlug(slug);
    return {
      slug,
      order,
      title: extractTitle(raw, slug),
      group: groupForOrder(order),
    };
  });

  docs.sort((a, b) => a.order - b.order || a.slug.localeCompare(b.slug));

  const groups: DocGroup[] = GROUP_DEFS.map((def) => ({
    key: def.key,
    label: def.label,
    docs: docs.filter((d) => d.group === def.key),
  })).filter((g) => g.docs.length > 0);

  return { docs, groups };
}

/** A slug → display-title map, used to humanize internal cross-link text. */
export type TitleMap = Readonly<Record<string, string>>;

/** True when `text` is itself a bare `NN-name.md` filename (optional `./` / `#anchor`). */
function filenameLinkText(text: string): { slug: string } | null {
  const m = /^\.?\/?([0-9]{2}-[a-z0-9-]+)\.md(?:#[^\s]*)?$/i.exec(text.trim());
  return m?.[1] ? { slug: m[1] } : null;
}

/**
 * Rewrite internal documentation links in rendered HTML so they resolve as app
 * routes, AND humanize their visible text.
 *
 * Markdown authors link to sibling guides as `NN-name.md` (optionally
 * `./NN-name.md`, optionally with a `#anchor`). Two transforms happen here:
 *
 *  1. **href:** `NN-name.md` → `/docs/NN-name` (preserving any `#anchor`).
 *     External `http(s)://`, `mailto:`, in-page `#anchors`, and already-absolute
 *     `/docs/...` links are left untouched.
 *  2. **text:** when an internal link's VISIBLE TEXT is *itself* a doc filename
 *     (e.g. `[03-personas.md](03-personas.md)` or an autolinked filename),
 *     replace it with the target guide's TITLE from `titles`. Unknown slug →
 *     the `.md` is stripped as a graceful fallback. Custom link text is preserved.
 *
 * `titles` is an optional slug → title map threaded in from the loader.
 */
export function rewriteDocLinks(html: string, titles: TitleMap = {}): string {
  return html.replace(
    /<a\s+href="([^"]*)"([^>]*)>([\s\S]*?)<\/a>/g,
    (full, href: string, attrs: string, inner: string) => {
      // Leave absolute/external/protocol/in-page links alone.
      if (/^([a-z]+:|\/\/|\/|#)/i.test(href)) return full;

      // Match an optional `./`, an NN-name(.md) body, and an optional #anchor.
      const m = /^\.?\/?([0-9]{2}-[a-z0-9-]+)\.md(#[^"]*)?$/i.exec(href);
      if (!m) return full;

      const slug = m[1];
      const anchor = m[2] ?? "";
      const newHref = `/docs/${String(slug)}${anchor}`;

      // Humanize the text ONLY when it is itself a doc filename.
      let newInner = inner;
      const asFilename = filenameLinkText(inner);
      if (asFilename) {
        newInner =
          titles[asFilename.slug] ?? asFilename.slug.replace(/\.md$/i, "");
      }

      return `<a href="${newHref}"${attrs}>${newInner}</a>`;
    },
  );
}

/**
 * Decode the handful of HTML entities `marked` emits in inline text so the
 * plain heading label + id are human-readable rather than carrying `&amp;`.
 */
function decodeEntities(text: string): string {
  return text
    .replace(/&amp;/g, "&")
    .replace(/&lt;/g, "<")
    .replace(/&gt;/g, ">")
    .replace(/&quot;/g, '"')
    .replace(/&#39;/g, "'")
    .replace(/&#x27;/gi, "'")
    .replace(/&#(\d+);/g, (_, n: string) => String.fromCodePoint(Number(n)))
    .replace(/&#x([0-9a-f]+);/gi, (_, n: string) =>
      String.fromCodePoint(parseInt(n, 16)),
    );
}

/**
 * Turn heading text into a stable, url-safe anchor id.
 * "Set a default Track ID" → "set-a-default-track-id".
 */
function slugifyHeading(text: string): string {
  return (
    text
      .toLowerCase()
      .replace(/[^a-z0-9\s-]/g, "")
      .trim()
      .replace(/\s+/g, "-")
      .replace(/-+/g, "-") || "section"
  );
}

/**
 * Render markdown to HTML, assign stable anchor ids to headings, rewrite +
 * humanize internal doc links, and return the collected H2/H3 headings (for the
 * "On this page" TOC). Synchronous.
 *
 * Two-pass approach (compatible with marked v18):
 *  1. `walkTokens` collects heading tokens to compute ids before rendering.
 *  2. A custom `heading` renderer uses those pre-computed ids.
 *
 * A fresh `Marked` instance per call keeps state isolated (no cross-render
 * leakage), so the function stays pure.
 *
 * Mermaid note: `guide/14-architecture.md` contains a ```mermaid fence.
 * It is rendered as a plain fenced code block (the mermaid source is
 * visible as preformatted text). Adding a full mermaid renderer was
 * out of scope for the initial implementation; the code block is
 * readable and conveys the diagram structure.
 */
export function renderDoc(
  markdown: string,
  titles: TitleMap = {},
): { html: string; headings: DocHeading[] } {
  const headings: DocHeading[] = [];
  // id-map tracks the rendered id for each heading token object, keyed by reference.
  const idMap = new WeakMap<Tokens.Heading, string>();
  const seen = new Map<string, number>();

  // ── Two-pass approach (marked v18 compatible) ─────────────────────────────
  // Pass 1: walkTokens runs before rendering; we compute stable anchor ids
  // from the token stream and store them keyed by token object reference.
  // Pass 2: the custom heading renderer reads the pre-computed id from idMap.
  //
  // We use md.use() for the renderer extension to avoid TypeScript's `this`
  // constraint on RendererObject methods (which requires the unexported
  // _Renderer type). md.use() accepts MarkedExtension and applies it without
  // needing an explicit `this` annotation on the inline object.
  const md = new Marked();

  md.use({
    walkTokens(token: Tokens.Generic) {
      if (token.type !== "heading") return;
      const h = token as Tokens.Heading;
      // Extract plain text from the raw heading token text (strip inline markup).
      const plain = decodeEntities(h.text.replace(/<[^>]+>/g, "")).trim();
      const base = slugifyHeading(plain);
      let id = base;
      const prior = seen.get(base);
      if (prior !== undefined) {
        const n = prior + 1;
        seen.set(base, n);
        id = `${base}-${String(n)}`;
      } else {
        seen.set(base, 0);
      }
      idMap.set(h, id);
      if (h.depth >= 2 && h.depth <= 3) {
        headings.push({ depth: h.depth, id, text: plain });
      }
    },
  });

  md.use({
    renderer: {
      heading(token: Tokens.Heading): string {
        const { text, depth } = token;
        const id =
          idMap.get(token) ??
          slugifyHeading(
            decodeEntities(text.replace(/<[^>]+>/g, "")).trim(),
          );
        return `<h${String(depth)} id="${id}">${text}</h${String(depth)}>\n`;
      },
    },
  });

  // marked parse is synchronous when no async extensions are configured.
  const rawHtml = md.parse(markdown, { async: false });
  // Strip <hr> (from the guides' `---` section separators). The headings
  // already structure each page; rendered <hr>s produce awkward empty bands.
  const noHr = rawHtml.replace(/<hr\s*\/?>\s*/gi, "");
  const html = rewriteDocLinks(noHr, titles);
  return { html, headings };
}
