/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Docs — in-app documentation reader.
 *
 * Routes:
 *   /docs          → redirects to /docs/00-quickstart
 *   /docs/:slug    → renders the matching guide page
 *
 * Content is bundled at build time from the repo-root `guide/` directory
 * via a Vite `import.meta.glob`. The glob reaches up two levels from
 * `src/pages/` → `../../../guide/*.md`.
 *
 * Layout: grouped sidebar nav | rendered markdown article | on-this-page TOC.
 * All styling via docs.css (Midnight Atelier token vocabulary).
 *
 * Internal `NN-name.md` links in the article are intercepted at click time
 * and converted to React Router navigations so the SPA doesn't hard-reload.
 */
import { useEffect, useMemo, useRef } from "react";
import { Link, Navigate, useNavigate, useParams } from "react-router-dom";
import { AppShell } from "@/components/AppShell";
import {
  buildDocList,
  renderDoc,
} from "@/lib/docs-render";
import type { DocMeta, TitleMap } from "@/lib/docs-render";
import "@/styles/docs.css";

// ─── Content bundle ──────────────────────────────────────────────────────────
// Reaches from web/src/pages/ up to the repo-root guide/ directory.
// Vite resolves this statically at build time; server.fs.allow in vite.config.ts
// permits dev-server access to the guide directory outside web/.
const RAW_FILES = import.meta.glob("../../../guide/*.md", {
  query: "?raw",
  eager: true,
  import: "default",
});

// ─── Pre-computed doc list ────────────────────────────────────────────────────
// buildDocList is pure + cheap; run it once at module load so every render
// of the Docs component gets the same stable references.
const { docs, groups } = buildDocList(RAW_FILES);

// Slug → title map for humanizing internal cross-links.
const TITLE_MAP: TitleMap = Object.fromEntries(
  docs.map((d) => [d.slug, d.title]),
);

// ─── Component ───────────────────────────────────────────────────────────────

function Docs() {
  const { slug } = useParams<{ slug?: string }>();
  const navigate = useNavigate();
  const articleRef = useRef<HTMLDivElement>(null);

  // Resolve the current doc. `slug` may be undefined on the bare /docs route;
  // these stay null then, and the hooks below still run unconditionally.
  const currentDoc: DocMeta | undefined = slug
    ? docs.find((d) => d.slug === slug)
    : undefined;
  const rawMd: string | undefined = slug
    ? RAW_FILES[`../../../guide/${slug}.md`]
    : undefined;

  // Render the article. useMemo so re-renders don't re-parse needlessly.
  // NOTE: every hook must run on every render — they all precede the early
  // returns below (Rules of Hooks).
  const { html, headings } = useMemo(() => {
    if (!rawMd) return { html: "", headings: [] };
    return renderDoc(rawMd, TITLE_MAP);
  }, [rawMd]);

  // Intercept clicks on internal `/docs/...` links in the rendered HTML so they
  // navigate via React Router instead of triggering a full page reload.
  useEffect(() => {
    const el = articleRef.current;
    if (!el) return;

    function handleClick(e: MouseEvent) {
      const target = (e.target as Element).closest("a");
      if (!target) return;
      const href = target.getAttribute("href");
      if (!href) return;
      // Only intercept internal /docs/... links.
      if (!href.startsWith("/docs/")) return;
      e.preventDefault();
      void navigate(href);
    }

    el.addEventListener("click", handleClick);
    return () => { el.removeEventListener("click", handleClick); };
  }, [navigate]);

  // ── All hooks above; early returns below ──

  // Bare /docs route → redirect to the first doc (quickstart).
  if (!slug) {
    const first = docs[0];
    if (first) return <Navigate to={`/docs/${first.slug}`} replace />;
    // No docs bundled at all — degenerate state.
    return (
      <AppShell>
        <div className="lmchat-docs-page">
          <main className="lmchat-docs-main">
            <p style={{ padding: "var(--space-group)", color: "var(--color-text-muted)" }}>
              No documentation pages found.
            </p>
          </main>
        </div>
      </AppShell>
    );
  }

  // Unknown slug → 404 state.
  if (!currentDoc || !rawMd) {
    return (
      <AppShell>
        <div className="lmchat-docs-page">
          <main className="lmchat-docs-main">
            <div className="lmchat-docs-layout">
              <Sidebar currentSlug={slug} />
              <div className="lmchat-docs-article-wrap">
                <div className="lmchat-docs-not-found">
                  <h2>Page not found</h2>
                  <p>
                    The documentation page <code>{slug}</code> does not exist.
                  </p>
                  <Link
                    to={`/docs/${docs[0]?.slug ?? ""}`}
                    style={{ color: "var(--color-accent-text)" }}
                  >
                    Back to quickstart
                  </Link>
                </div>
              </div>
            </div>
          </main>
        </div>
      </AppShell>
    );
  }

  return (
    <AppShell>
      <div className="lmchat-docs-page">
        <main className="lmchat-docs-main" id="docs-main-content">
          <div className="lmchat-docs-layout">
            {/* ── Sidebar ────────────────────────────────────────────────── */}
            <Sidebar currentSlug={slug} />

            {/* ── Article ────────────────────────────────────────────────── */}
            <div className="lmchat-docs-article-wrap">
              <article
                ref={articleRef}
                className="lmchat-docs-article"
                /* The content is first-party, build-time-bundled markdown with no
                   user-supplied input. dangerouslySetInnerHTML is acceptable here. */
                dangerouslySetInnerHTML={{ __html: html }}
              />
            </div>

            {/* ── On this page TOC ───────────────────────────────────────── */}
            {headings.length > 0 && (
              <nav
                className="lmchat-docs-toc"
                aria-label="On this page"
              >
                <span className="lmchat-docs-toc-title">On this page</span>
                <ul className="lmchat-docs-toc-list" role="list">
                  {headings.map((h) => (
                    <li key={h.id} className="lmchat-docs-toc-item">
                      <a
                        href={`#${h.id}`}
                        className={`lmchat-docs-toc-link${h.depth === 3 ? " lmchat-docs-toc-link--h3" : ""}`}
                      >
                        {h.text}
                      </a>
                    </li>
                  ))}
                </ul>
              </nav>
            )}
          </div>
        </main>
      </div>
    </AppShell>
  );
}

// ─── Sidebar ─────────────────────────────────────────────────────────────────

function Sidebar({ currentSlug }: { currentSlug: string }) {
  return (
    <nav
      className="lmchat-docs-sidebar"
      aria-label="Documentation navigation"
    >
      <div className="lmchat-docs-sidebar-header">
        <p className="lmchat-docs-sidebar-title">Documentation</p>
      </div>

      {groups.map((group) => (
        <div key={group.key} className="lmchat-docs-nav-group">
          <span className="lmchat-docs-nav-group-label">{group.label}</span>
          <div className="lmchat-docs-nav-items">
            {group.docs.map((doc) => (
              <Link
                key={doc.slug}
                to={`/docs/${doc.slug}`}
                className={`lmchat-docs-nav-item${doc.slug === currentSlug ? " lmchat-docs-nav-item--active" : ""}`}
                aria-current={doc.slug === currentSlug ? "page" : undefined}
              >
                {doc.title}
              </Link>
            ))}
          </div>
        </div>
      ))}
    </nav>
  );
}

export default Docs;
