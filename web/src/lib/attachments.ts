/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Composer attachment helpers.
 *
 * The chat composer stages every attachment as a base64 `data:` URL. Images
 * are sent to the model as ``{type:"image", data_url}`` blocks; TEXT files
 * (e.g. `text/plain`) must NOT go down that path — the backend validates the
 * image data_url and rejects a `data:text/plain;base64,…` payload as "invalid
 * base64-encoded image data". Instead a text file's contents are decoded and
 * folded into the message text. These helpers do the partition + decode.
 */

/** A file staged in the composer, as a base64 `data:` URL plus its name. */
export interface StagedAttachment {
  id: string;
  dataUrl: string;
  name: string;
}

/** True when the data: URL carries image bytes (→ send as an image block). */
export function isImageDataUrl(dataUrl: string): boolean {
  return dataUrl.startsWith("data:image/");
}

/**
 * Decode a base64 `data:` URL to UTF-8 text.
 *
 * Returns `""` for a malformed / non-base64 / empty payload so the caller can
 * skip it rather than surfacing a decode crash. Handles multi-byte UTF-8 (a
 * naive `atob` alone would mangle non-ASCII).
 */
export function dataUrlToText(dataUrl: string): string {
  const comma = dataUrl.indexOf(",");
  const b64 = comma >= 0 ? dataUrl.slice(comma + 1) : "";
  if (b64 === "") return "";
  try {
    const binary = atob(b64);
    const bytes = Uint8Array.from(binary, (ch) => ch.charCodeAt(0));
    return new TextDecoder("utf-8").decode(bytes);
  } catch {
    return "";
  }
}

/**
 * Split staged attachments into image blocks (sent as `type:"image"`) and the
 * decoded text of any non-image attachments, each labelled with its filename
 * so the model sees a clear boundary. Empty/undecodable text files are dropped.
 */
export function partitionAttachments(attachments: StagedAttachment[]): {
  imageDataUrls: string[];
  textSegments: string[];
} {
  const imageDataUrls: string[] = [];
  const textSegments: string[] = [];
  for (const a of attachments) {
    if (isImageDataUrl(a.dataUrl)) {
      imageDataUrls.push(a.dataUrl);
      continue;
    }
    const text = dataUrlToText(a.dataUrl);
    if (text.trim() !== "") {
      textSegments.push(`[Attached file: ${a.name}]\n${text}`);
    }
  }
  return { imageDataUrls, textSegments };
}
