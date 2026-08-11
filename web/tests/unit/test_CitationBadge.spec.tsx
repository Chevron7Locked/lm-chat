/* SPDX-License-Identifier: Apache-2.0 */
/**
 * CitationBadge unit tests — render + click + tokeniser invariants.
 *
 * Locked behaviours:
 *   - The badge renders with the doc-id label by default, and prefers a
 *     caller-supplied title when provided.
 *   - Click invokes the supplied onClick prop (default-tab-open is exercised
 *     in the e2e suite — jsdom's window.open is a no-op stub).
 *   - tokenizeCitations splits a mixed text/marker string into the expected
 *     ordered token list and tolerates malformed markers without throwing.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { CitationBadge, tokenizeCitations } from "@/components/CitationBadge";

describe("CitationBadge", () => {
  it("renders the docId as label by default", () => {
    render(<CitationBadge docId="sample-5page-pdf" />);
    const btn = screen.getByRole("button", { name: /Citation: sample-5page-pdf/ });
    expect(btn).not.toBeNull();
    expect(btn.textContent).toContain("sample-5page-pdf");
  });

  it("prefers the supplied title over docId", () => {
    render(<CitationBadge docId="42" title="Annual report 2026" />);
    const btn = screen.getByRole("button", { name: /Citation: Annual report 2026/ });
    expect(btn.textContent).toContain("Annual report 2026");
  });

  it("invokes onClick when the badge is clicked", () => {
    const handle = vi.fn();
    render(<CitationBadge docId="abc" onClick={handle} />);
    fireEvent.click(screen.getByRole("button", { name: /Citation: abc/ }));
    expect(handle).toHaveBeenCalledTimes(1);
  });

  it("exposes a data-testid keyed by the docId", () => {
    render(<CitationBadge docId="sample-5page-pdf" />);
    expect(document.querySelector("[data-testid='citation-badge-sample-5page-pdf']")).not.toBeNull();
  });
});

describe("tokenizeCitations", () => {
  it("returns a single text token when no marker is present", () => {
    const tokens = tokenizeCitations("hello world");
    expect(tokens).toEqual([{ kind: "text", value: "hello world" }]);
  });

  it("splits a single inline marker", () => {
    const tokens = tokenizeCitations("see [doc:sample-pdf] for details");
    expect(tokens).toEqual([
      { kind: "text", value: "see " },
      { kind: "citation", docId: "sample-pdf" },
      { kind: "text", value: " for details" },
    ]);
  });

  it("splits multiple markers in order", () => {
    const tokens = tokenizeCitations("[doc:a] and [doc:b]");
    expect(tokens).toEqual([
      { kind: "citation", docId: "a" },
      { kind: "text", value: " and " },
      { kind: "citation", docId: "b" },
    ]);
  });

  it("leaves malformed markers as plain text (no docId match)", () => {
    const tokens = tokenizeCitations("not a citation: [doc:has spaces]");
    expect(tokens).toEqual([
      { kind: "text", value: "not a citation: [doc:has spaces]" },
    ]);
  });

  it("supports dotted and underscored doc ids", () => {
    const tokens = tokenizeCitations("ref [doc:plan.v2_final]");
    expect(tokens[1]).toEqual({ kind: "citation", docId: "plan.v2_final" });
  });
});
