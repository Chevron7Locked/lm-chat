/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useDocumentTitle — set the browser tab title for the current route.
 *
 * Without care, a "one-frame title flash" appears on every route
 * change: `<title>` flashes `Chat 13` for a frame before the real
 * chat title resolves. Two fixes:
 *
 * 1. Switch to ``useLayoutEffect`` so the title write happens BEFORE
 *    the browser paints. Without this, React commits the route's DOM
 *    before the effect fires, the browser paints with the previous
 *    title, and only the next frame shows the new one.
 *
 * 2. Treat ``null``/``""``/``undefined`` as "no specific title yet"
 *    and render only the brand. Callers can pass ``null`` while
 *    waiting on async data (e.g. chat title from an API fetch); we
 *    DO NOT render a placeholder like "Chat 13" that has to be
 *    replaced milliseconds later.
 *
 * Pages call this hook with a short suffix; we render
 * "<BRAND_NAME> — <suffix>". When suffix is empty/null, we render
 * just "<BRAND_NAME>".
 */
import { useLayoutEffect } from "react";
import { BRAND_NAME } from "@/components/BrandMark";

export function useDocumentTitle(suffix: string | null | undefined): void {
  useLayoutEffect(() => {
    const previous = document.title;
    const trimmed = (suffix ?? "").trim();
    document.title = trimmed === "" ? BRAND_NAME : `${BRAND_NAME} — ${trimmed}`;
    return () => {
      document.title = previous;
    };
  }, [suffix]);
}
