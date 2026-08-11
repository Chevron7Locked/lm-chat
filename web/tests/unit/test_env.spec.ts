import { it, expect } from "vitest";
it("stream globals available", () => {
  expect(typeof ReadableStream).toBe("function");
  expect(typeof TransformStream).toBe("function");
  expect(typeof WritableStream).toBe("function");
});
