/* SPDX-License-Identifier: Apache-2.0 */
/**
 * SSE event-name contract: FE union == sse-event-names.json.
 *
 * Item 5 (chat-flow remediation 2026-06-12) — the structural fix for the
 * T0-1 class of bugs (a BE event the FE silently dropped). The shared SSOT
 * is web/src/types/sse-event-names.json; this file asserts the FE side:
 *
 *   CANONICAL_EVENT_TYPES (the value-level artifact that derives the
 *   CanonicalEventType literal union in useSSE.ts) == the SSOT, exactly.
 *
 * Because handleEvent's switch ends in assertNever(raw.type), TypeScript
 * already guarantees every member of CanonicalEventType has a case (or an
 * explicit ignore-list entry). This test closes the remaining gap: the
 * union itself can't silently miss (or hoard stale) wire event names.
 *
 * The BE side is asserted by pytest
 * (tests/contracts/test_sse_event_names_contract.py) against the
 * CanonicalEvent Literal + the _format_error_frame/_format_warning_frame
 * synthetic frames. Either side drifting fails its test loudly.
 */
import { describe, it, expect } from "vitest";
import { CANONICAL_EVENT_TYPES } from "@/hooks/useSSE";
import type { CanonicalEventType } from "@/hooks/useSSE";
// Vite/vitest resolves JSON imports natively (same pattern as
// test_sub_error_contract.spec.ts).
import ssot from "@/types/sse-event-names.json";

describe("SSE event-name contract — FE union matches sse-event-names.json", () => {
  const ssotNames: string[] = ssot.event_names;

  it("SSOT file is well-formed (non-empty list of unique strings)", () => {
    expect(Array.isArray(ssotNames)).toBe(true);
    expect(ssotNames.length).toBeGreaterThan(0);
    expect(new Set(ssotNames).size).toBe(ssotNames.length);
    for (const name of ssotNames) expect(typeof name).toBe("string");
  });

  it("CanonicalEventType covers every BE-emittable event name", () => {
    const union = new Set<string>(CANONICAL_EVENT_TYPES);
    const missing = ssotNames.filter((n) => !union.has(n));
    expect(
      missing,
      `BE-emittable SSE events missing from the FE CanonicalEventType union ` +
        `(these would be silently dropped — the T0-1 bug class): ` +
        `${JSON.stringify(missing)}. Add them to CANONICAL_EVENT_TYPES and ` +
        `give handleEvent a case (or an explicit ignore-list entry).`,
    ).toEqual([]);
  });

  it("CanonicalEventType lists no stale names the BE cannot emit", () => {
    const ssotSet = new Set(ssotNames);
    const stale = CANONICAL_EVENT_TYPES.filter((n) => !ssotSet.has(n));
    expect(
      stale,
      `FE CanonicalEventType members absent from sse-event-names.json: ` +
        `${JSON.stringify(stale)}. Remove them, or — if the BE really emits ` +
        `them — add them to the SSOT so the BE pytest pins them too.`,
    ).toEqual([]);
  });

  it("union has no duplicate members", () => {
    expect(new Set(CANONICAL_EVENT_TYPES).size).toBe(
      CANONICAL_EVENT_TYPES.length,
    );
  });

  it("type-level: CANONICAL_EVENT_TYPES drives the CanonicalEventType union", () => {
    // Compile-time round-trip: every array member is a CanonicalEventType
    // (trivially true by derivation) and the two synthetic frames the FE
    // special-cases are members of the union. If either is ever dropped
    // from CANONICAL_EVENT_TYPES, these annotations fail the typecheck.
    const errorName: CanonicalEventType = "error";
    const warningName: CanonicalEventType = "warning";
    expect(CANONICAL_EVENT_TYPES).toContain(errorName);
    expect(CANONICAL_EVENT_TYPES).toContain(warningName);
  });
});
