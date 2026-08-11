/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useComposerAttachments — staged file attachments for the composer:
 * image/text file validation + base64 encoding, the vision capability
 * gate, and the `accept` filter hint.
 *
 * Extracted from Composer.tsx — behavior-preserving; every callback body is
 * verbatim from Composer.tsx, only wrapped in this hook.
 */
import { useRef, useState } from "react";
import type { Dispatch, RefObject, SetStateAction } from "react";
import type { PushOptions } from "@/stores/toastStore";

export interface ComposerAttachment {
  id: string;
  dataUrl: string;
  name: string;
}

export interface UseComposerAttachmentsResult {
  attachedImages: ComposerAttachment[];
  setAttachedImages: Dispatch<SetStateAction<ComposerAttachment[]>>;
  attachInputRef: RefObject<HTMLInputElement | null>;
  attachAccept: string;
  handleAttachFiles: (files: FileList | null) => void;
}

export function useComposerAttachments(
  isVision: boolean,
  push: (opts: PushOptions) => string,
): UseComposerAttachmentsResult {
  const attachInputRef = useRef<HTMLInputElement>(null);

  // Attachment state.
  // Staged attachments are base64-encoded data URLs sent via the existing
  // {type: "image", data_url} payload shape. Gated on capabilities.vision.
  const [attachedImages, setAttachedImages] = useState<ComposerAttachment[]>(
    [],
  );

  // File-attachment handler. Accepts image/* and a small text/* whitelist.
  const ACCEPTED_FILE_TYPES = ["image/jpeg", "image/png", "image/gif", "image/webp",
    "image/svg+xml", "text/plain"] as const;
  // Non-vision (or unknown-capability) models only
  // get the text subset in the file picker's `accept` filter. This is a UX
  // hint, not the enforcement boundary — see the isVision guard below.
  const attachAccept = (isVision
    ? ACCEPTED_FILE_TYPES
    : ACCEPTED_FILE_TYPES.filter((t) => !t.startsWith("image/"))
  ).join(",");
  const MAX_ATTACH_SIZE_MB = 10;

  function handleAttachFiles(files: FileList | null): void {
    if (files === null || files.length === 0) return;
    Array.from(files).forEach((file) => {
      // Enforce the vision gate in the handler, not
      // just the `accept` attribute — drag/paste and model-switch bypass the
      // picker filter. Text files always stage regardless of capability.
      if (!isVision && file.type.startsWith("image/")) {
        push({
          variant: "warning",
          message: "This model can't view images — switch to a vision model, or attach a text file.",
        });
        return;
      }
      if (!ACCEPTED_FILE_TYPES.includes(file.type as typeof ACCEPTED_FILE_TYPES[number])) {
        push({ variant: "warning", message: `Unsupported file type: ${file.type}` });
        return;
      }
      if (file.size > MAX_ATTACH_SIZE_MB * 1024 * 1024) {
        push({ variant: "warning", message: `File too large (max ${String(MAX_ATTACH_SIZE_MB)} MB): ${file.name}` });
        return;
      }
      const reader = new FileReader();
      reader.onload = (ev) => {
        const dataUrl = ev.target?.result;
        if (typeof dataUrl === "string") {
          setAttachedImages((prev) => [
            ...prev,
            { id: `${file.name}-${String(Date.now())}`, dataUrl, name: file.name },
          ]);
        }
      };
      reader.readAsDataURL(file);
    });
    // Reset input so the same file can be re-attached after removal.
    if (attachInputRef.current !== null) attachInputRef.current.value = "";
  }

  return {
    attachedImages,
    setAttachedImages,
    attachInputRef,
    attachAccept,
    handleAttachFiles,
  };
}
