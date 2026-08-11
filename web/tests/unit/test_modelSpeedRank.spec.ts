/* SPDX-License-Identifier: Apache-2.0 */
import { describe, it, expect } from "vitest";
import {
  modelSpeedRank,
  pickFastestModel,
  pickSlowestModel,
} from "@/lib/modelSpeedRank";

describe("modelSpeedRank", () => {
  it("prefers the MoE ACTIVE param count over the total", () => {
    // 35B total but 3B active -> ranks by 3 (what actually drives speed).
    expect(modelSpeedRank("qwen3.6-35b-a3b-mtp")).toBe(3);
    // 122B total, 10B active -> 10 (slower than the 3B-active 35B).
    expect(modelSpeedRank("qwen3.5-122b-a10b-mtp")).toBe(10);
  });

  it("falls back to total params for dense models", () => {
    expect(modelSpeedRank("qwen3.5-9b")).toBe(9);
    expect(modelSpeedRank("Qwen3.6 27B UD")).toBe(27);
  });

  it("handles decimals and is case-insensitive", () => {
    expect(modelSpeedRank("foo-a1.5b-mtp")).toBe(1.5);
    expect(modelSpeedRank("BAR-A2B")).toBe(2);
  });

  it("returns Infinity when no param token is present", () => {
    expect(modelSpeedRank("some-unlabelled-model")).toBe(Number.POSITIVE_INFINITY);
  });
});

describe("pickFastestModel", () => {
  it("picks the smallest-active general model, NOT the first in list order", () => {
    // Mirrors the dogfood rig: the 122B is first, but the 3B-active 35B is faster.
    const models = [
      { id: "qwen3.5-122b-a10b-mtp", name: "Qwen3.5 122B A10B" },
      { id: "qwen3.6-35b-a3b-mtp", name: "Qwen3.6 35B A3B" },
      { id: "huihui-qwen3.6-35b-a3b-abliterated", name: "Qwen3.6 35B Abliterated" },
    ];
    expect(pickFastestModel(models, (m) => `${m.id} ${m.name}`)?.id).toBe(
      "qwen3.6-35b-a3b-mtp",
    );
  });

  it("keeps the first occurrence on a tie", () => {
    const models = [{ id: "a-9b" }, { id: "b-9b" }];
    expect(pickFastestModel(models, (m) => m.id)?.id).toBe("a-9b");
  });

  it("returns undefined for an empty list", () => {
    expect(pickFastestModel([], (m) => String(m))).toBeUndefined();
  });
});

describe("pickSlowestModel", () => {
  it("picks the largest-active model (the dogfood slow dimension)", () => {
    const models = [
      { id: "qwen3.6-35b-a3b-mtp", name: "Qwen3.6 35B A3B" },
      { id: "qwen3.5-122b-a10b-mtp", name: "Qwen3.5 122B A10B" },
      { id: "qwen3.5-9b", name: "Qwen3.5 9B" },
    ];
    expect(pickSlowestModel(models, (m) => `${m.id} ${m.name}`)?.id).toBe(
      "qwen3.5-122b-a10b-mtp",
    );
  });

  it("keeps the first occurrence on a tie", () => {
    const models = [{ id: "a-122b-a10b" }, { id: "b-122b-a10b" }];
    expect(pickSlowestModel(models, (m) => m.id)?.id).toBe("a-122b-a10b");
  });

  it("returns undefined for an empty list", () => {
    expect(pickSlowestModel([], (m) => String(m))).toBeUndefined();
  });
});
