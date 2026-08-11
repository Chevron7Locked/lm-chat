/* SPDX-License-Identifier: Apache-2.0 */
/**
 * dedupeByKey — pure-function unit tests.
 *
 * Bug B (2026-07-18 dogfood): "Encountered two children with the same
 * key" fired on model/embedder dropdowns because upstream lists (a raw
 * LM Studio probe, an independently-polled status snapshot) aren't
 * always unique on the field used as the React `key`. This helper is
 * the shared fix applied at each list-construction site.
 */
import { describe, it, expect } from "vitest";
import { dedupeByKey } from "@/lib/dedupeByKey";

describe("dedupeByKey", () => {
  it("returns the input unchanged when there are no duplicate keys", () => {
    const items = [{ id: "a" }, { id: "b" }, { id: "c" }];
    expect(dedupeByKey(items, (i) => i.id)).toEqual(items);
  });

  it("keeps only the FIRST occurrence of a repeated key", () => {
    const items = [
      { id: "a", label: "first" },
      { id: "b", label: "only" },
      { id: "a", label: "second — dropped" },
    ];
    const result = dedupeByKey(items, (i) => i.id);
    expect(result).toHaveLength(2);
    expect(result.find((i) => i.id === "a")?.label).toBe("first");
  });

  it("collapses a key repeated more than twice down to one entry", () => {
    const items = ["x", "x", "x"];
    expect(dedupeByKey(items, (s) => s)).toEqual(["x"]);
  });

  it("returns an empty array for empty input", () => {
    expect(dedupeByKey([], (s: string) => s)).toEqual([]);
  });

  it("supports a plain string list via the identity key selector", () => {
    const ids = ["mcp/a", "mcp/b", "mcp/a"];
    expect(dedupeByKey(ids, (id) => id)).toEqual(["mcp/a", "mcp/b"]);
  });
});
