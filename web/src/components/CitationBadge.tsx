/* SPDX-License-Identifier: Apache-2.0 */
/**
 * CitationBadge — chip-style UI for RAG document references.
 *
 * Backend RAG injection (src/lmchat/services/rag_service.py) writes
 * citations into the prompt context block in two relevant forms:
 *
 *   1. Source pointer line:  `source: doc:<id> | chunk:<ordinal> | title: <title>`
 *      (injected verbatim into the augmented user prompt).
 *   2. Inline reference     `[doc:<id>]` — the canonical short form used
 *      when an assistant turn cites a retrieved document.  This is the
 *      marker the assistant emits when it wants the badge to render.
 *
 * Markers are replaced inline in the rendered markdown via
 * {@link replaceCitationsInText}; the badge presents the document id (or a
 * caller-supplied title) plus a click target that opens the Documents
 * route in a new tab.  No new dependency is added — the badge is pure CSS.
 */
import type { CSSProperties } from "react";

// ─── Types ──────────────────────────────────────────────────────────────────

export interface CitationBadgeProps {
  /** Document id (string — slug or numeric, per the marker). */
  docId: string;
  /** Optional human-readable title (defaults to the docId). */
  title?: string | undefined;
  /** Optional click handler; default opens /documents in a new tab. */
  onClick?: (() => void) | undefined;
}

// ─── Marker pattern ─────────────────────────────────────────────────────────

/**
 * Match `[doc:<id>]` where <id> may contain letters, digits, dashes,
 * underscores, or dots (typical slugified filenames).  Spaces inside the
 * marker are not permitted — they signal a malformed marker that should be
 * left as plain text.
 */
const CITATION_PATTERN = /\[doc:([a-zA-Z0-9._-]+)\]/g;

/**
 * Tokenise a rendered text fragment into a list of plain strings and
 * `{ kind: "citation", docId }` records.  Used by ChatMessage's markdown
 * `text` renderer to inject {@link CitationBadge} into the React tree.
 *
 * Returning a `kind` discriminant (instead of mixing strings and JSX) keeps
 * the tokeniser pure — JSX is constructed by the caller, which is what the
 * rendering tests assert against.
 */
export type CitationToken =
  | { kind: "text"; value: string }
  | { kind: "citation"; docId: string };

export function tokenizeCitations(text: string): CitationToken[] {
  const tokens: CitationToken[] = [];
  let lastIndex = 0;
  // Reset the regex's lastIndex because /g is stateful across calls.
  const pattern = new RegExp(CITATION_PATTERN.source, "g");
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > lastIndex) {
      tokens.push({ kind: "text", value: text.slice(lastIndex, match.index) });
    }
    const docId = match[1] ?? "";
    if (docId !== "") {
      tokens.push({ kind: "citation", docId });
    }
    lastIndex = match.index + match[0].length;
  }
  if (lastIndex < text.length) {
    tokens.push({ kind: "text", value: text.slice(lastIndex) });
  }
  return tokens;
}

// ─── Component ──────────────────────────────────────────────────────────────

export function CitationBadge({ docId, title, onClick }: CitationBadgeProps) {
  const label = title !== undefined && title !== "" ? title : docId;

  function handleClick(): void {
    if (onClick !== undefined) {
      onClick();
      return;
    }
    // Default: open the Documents page in a new tab so the user can find
    // the source.  Anchor focus so screen readers announce the navigation.
    try {
      window.open("/documents", "_blank", "noopener,noreferrer");
    } catch {
      // window.open can throw in unusual sandboxes (jsdom in some configs).
      // The badge still rendered; click is best-effort.
    }
  }

  return (
    <button
      type="button"
      onClick={handleClick}
      aria-label={`Citation: ${label}`}
      data-testid={`citation-badge-${docId}`}
      style={badgeStyle}
      title={`Open documents — ${label}`}
    >
      <CitationIcon />
      <span style={labelStyle}>{label}</span>
    </button>
  );
}

// ─── Icon ──────────────────────────────────────────────────────────────────

function CitationIcon() {
  return (
    <svg
      width="11"
      height="11"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
      <polyline points="14 2 14 8 20 8" />
    </svg>
  );
}

// ─── Styles ────────────────────────────────────────────────────────────────

const badgeStyle: CSSProperties = {
  display: "inline-flex",
  alignItems: "center",
  gap: "4px",
  margin: "0 2px",
  padding: "1px 6px",
  background: "var(--color-accent-quiet)",
  border: "1px solid var(--color-accent-border)",
  borderRadius: "var(--radius-pill, 9999px)",
  fontSize: "0.75rem",
  fontFamily: "var(--font-mono)",
  color: "var(--color-accent)",
  cursor: "pointer",
  verticalAlign: "baseline",
  lineHeight: 1.4,
  textDecoration: "none",
  transition: "background var(--duration-fast) var(--ease-out)",
};

const labelStyle: CSSProperties = {
  fontWeight: 500,
  letterSpacing: "0.01em",
  maxWidth: "260px",
  overflow: "hidden",
  textOverflow: "ellipsis",
  whiteSpace: "nowrap",
};
