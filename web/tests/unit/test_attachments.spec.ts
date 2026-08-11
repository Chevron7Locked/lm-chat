/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for web/src/lib/attachments.ts — the composer attachment
 * partition/decode used to keep text files off the {type:"image"} path.
 */
import { describe, it, expect } from "vitest";
import {
  isImageDataUrl,
  dataUrlToText,
  partitionAttachments,
} from "@/lib/attachments";

function textDataUrl(text: string, mime = "text/plain"): string {
  const bytes = new TextEncoder().encode(text);
  let binary = "";
  for (const b of bytes) binary += String.fromCharCode(b);
  return `data:${mime};base64,${btoa(binary)}`;
}

describe("isImageDataUrl", () => {
  it("is true for image data URLs, false otherwise", () => {
    expect(isImageDataUrl("data:image/png;base64,AAAA")).toBe(true);
    expect(isImageDataUrl("data:image/webp;base64,AAAA")).toBe(true);
    expect(isImageDataUrl("data:text/plain;base64,AAAA")).toBe(false);
    expect(isImageDataUrl("data:application/pdf;base64,AAAA")).toBe(false);
  });
});

describe("dataUrlToText", () => {
  it("decodes ASCII text", () => {
    expect(dataUrlToText(textDataUrl("hello world"))).toBe("hello world");
  });

  it("decodes multi-byte UTF-8", () => {
    expect(dataUrlToText(textDataUrl("café ☕ — 日本語"))).toBe("café ☕ — 日本語");
  });

  it("returns empty string for empty or malformed payloads", () => {
    expect(dataUrlToText("data:text/plain;base64,")).toBe("");
    expect(dataUrlToText("not-a-data-url")).toBe("");
    expect(dataUrlToText("")).toBe("");
  });
});

describe("partitionAttachments", () => {
  it("keeps images, folds text with a filename label, drops empty text", () => {
    const { imageDataUrls, textSegments } = partitionAttachments([
      { id: "1", name: "pic.png", dataUrl: "data:image/png;base64,iVBORw0KGgo=" },
      { id: "2", name: "notes.txt", dataUrl: textDataUrl("line one\nline two") },
      { id: "3", name: "blank.txt", dataUrl: "data:text/plain;base64," },
    ]);
    expect(imageDataUrls).toEqual(["data:image/png;base64,iVBORw0KGgo="]);
    expect(textSegments).toEqual([
      "[Attached file: notes.txt]\nline one\nline two",
    ]);
  });

  it("handles an all-image set (no text segments)", () => {
    const { imageDataUrls, textSegments } = partitionAttachments([
      { id: "1", name: "a.png", dataUrl: "data:image/png;base64,AAAA" },
    ]);
    expect(imageDataUrls).toEqual(["data:image/png;base64,AAAA"]);
    expect(textSegments).toEqual([]);
  });
});
