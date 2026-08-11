/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Documents page — upload, list, and manage RAG documents.
 *
 * Features:
 * - Drag-drop upload zone (with <input type="file"> fallback).
 * - Multi-file upload supported.
 * - Document list: filename, size, chunk count, uploaded-at, delete button.
 * - Chunk preview: click a document to expand and show chunk previews.
 *
 * Consumes:
 *   GET    /api/documents              — useDocuments
 *   POST   /api/documents              — useUploadDocument
 *   DELETE /api/documents/{id}         — useDeleteDocument
 *   GET    /api/documents/{id}/chunks  — useDocumentChunks
 *
 * Per-chat RAG enable toggle is on Chat.tsx (separate from this page).
 *
 * Inline CSSProperties replaced with reading-rooms.css semantic classes.
 */
import { useState, useRef } from "react";
import type { ChangeEvent, DragEvent } from "react";
import { ChevronDown, ChevronRight, X } from "lucide-react";
import { AppShell } from "@/components/AppShell";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import {
  useDocuments,
  useUploadDocument,
  useDeleteDocument,
  useDocumentChunks,
} from "@/hooks/useDocuments";
import type { Document } from "@/hooks/useDocuments";
import { useMoveDocumentToProject } from "@/hooks/useProjects";
import { MoveToProjectMenu } from "@/components/MoveToProjectMenu";
import { useAuthStore } from "@/stores/authStore";
import { useToast } from "@/stores/toastStore";
import "@/styles/reading-rooms.css";
import "@/styles/settings.css";

// ─── Helpers ─────────────────────────────────────────────────────────────────

function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes.toString()} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString();
}

function getFileExt(title: string): string {
  const dot = title.lastIndexOf(".");
  if (dot === -1) return "file";
  return title.slice(dot + 1).toUpperCase();
}

// ─── Chunk preview sub-component ─────────────────────────────────────────────

function ChunkExpansion({ documentId }: { documentId: number }) {
  const { data, isLoading, isError } = useDocumentChunks(documentId);

  if (isLoading) return <p className="rr-hint">Loading chunks…</p>;
  if (isError)
    return (
      <p className="rr-hint rr-hint--error">
        Couldn't load documents — try again.
      </p>
    );
  if (!data || data.length === 0)
    return <p className="rr-hint">No chunks yet.</p>;

  return (
    <div className="rr-chunk-panel">
      {data.map((chunk) => (
        <div key={chunk.ordinal} className="rr-chunk-row">
          <span className="rr-chunk-ordinal">#{chunk.ordinal}</span>
          <span className="rr-chunk-text">{chunk.text_preview}</span>
        </div>
      ))}
    </div>
  );
}

// ─── Document row component ───────────────────────────────────────────────────

function DocumentRow({
  doc,
  onDelete,
  isExpanded,
  onToggleExpand,
}: {
  doc: Document;
  onDelete: (id: number) => void;
  isExpanded: boolean;
  onToggleExpand: (id: number) => void;
}) {
  // Move-to-project menu on the document row.
  const moveDoc = useMoveDocumentToProject();
  const currentProjectId = doc.project_id ?? null;

  function handleMove(newProjectId: number | null): void {
    moveDoc.mutate({
      documentId: doc.id,
      oldProjectId: currentProjectId,
      newProjectId,
    });
  }

  return (
    <li className="rr-doc-row">
      <div className="rr-doc-row-header">
        <button
          type="button"
          onClick={() => {
            onToggleExpand(doc.id);
          }}
          className="rr-doc-title-btn"
          aria-expanded={isExpanded}
        >
          <span className="rr-doc-expand-icon">
            {isExpanded ? (
              <ChevronDown size={14} aria-hidden />
            ) : (
              <ChevronRight size={14} aria-hidden />
            )}
          </span>
          <span className="rr-doc-title">{doc.title}</span>
        </button>
        {/* File-type chip — foxing background */}
        <span className="rr-doc-type-chip">{getFileExt(doc.title)}</span>
        {/* Marginalia metadata */}
        <span className="rr-doc-meta">
          {formatBytes(doc.byte_size)} · {doc.chunk_count} chunks ·{" "}
          {formatDate(doc.uploaded_at)}
        </span>
        {/* Hover-revealed actions — layout owned by .rr-doc-actions
            (flex + glue-relaxed gap); former inline style duplicated it. */}
        <span className="rr-doc-actions">
          <MoveToProjectMenu
            currentProjectId={currentProjectId}
            onMove={handleMove}
            testIdPrefix={`doc-${String(doc.id)}-move`}
            ariaLabel={`Move "${doc.title}" to project`}
          />
          <button
            type="button"
            onClick={() => {
              onDelete(doc.id);
            }}
            aria-label={`Delete ${doc.title}`}
            className="rr-icon-btn"
          >
            <X size={12} aria-hidden />
          </button>
        </span>
      </div>
      {isExpanded && <ChunkExpansion documentId={doc.id} />}
    </li>
  );
}

// ─── Main component ───────────────────────────────────────────────────────────

export interface DocumentsProps {
  /**
   * Right-rail mount (Chat.tsx RightPanel Drawer, ~380px). Skips the
   * AppShell wrapper — the page already lives inside Chat's <main>, and a
   * nested AppShell rendered a duplicate `<main id="main-content">`
   * landmark inside the dialog. Also drops the page header (the Drawer
   * banner already says "Documents") and applies compact rail padding.
   */
  inRail?: boolean;
}

export default function Documents({ inRail = false }: DocumentsProps) {
  useDocumentTitle("Documents");
  const { user, isInitializing } = useAuthStore();
  const { data, isLoading, isError } = useDocuments();
  const upload = useUploadDocument();
  const deleteDoc = useDeleteDocument();
  const { push } = useToast();

  const [isDragging, setIsDragging] = useState(false);
  const [expandedId, setExpandedId] = useState<number | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const docs: Document[] = data ?? [];

  if (!isInitializing && user === null) {
    return (
      <div className="rr-page">
        <p className="rr-hint">Sign in to manage your documents.</p>
      </div>
    );
  }

  async function handleFiles(files: FileList | File[]): Promise<void> {
    const arr = Array.from(files);
    for (const file of arr) {
      try {
        const result = await upload.mutateAsync(file);
        push({
          variant: "success",
          message: `Uploaded "${result.filename}" (${result.chunk_count.toString()} chunks).`,
        });
      } catch {
        push({
          variant: "error",
          message: `Couldn't upload "${file.name}" — try again.`,
        });
      }
    }
  }

  function handleDragOver(e: DragEvent<HTMLDivElement>): void {
    e.preventDefault();
    setIsDragging(true);
  }

  function handleDragLeave(e: DragEvent<HTMLDivElement>): void {
    e.preventDefault();
    setIsDragging(false);
  }

  function handleDrop(e: DragEvent<HTMLDivElement>): void {
    e.preventDefault();
    setIsDragging(false);
    if (e.dataTransfer.files.length > 0) {
      void handleFiles(e.dataTransfer.files);
    }
  }

  function handleInputChange(e: ChangeEvent<HTMLInputElement>): void {
    if (e.target.files && e.target.files.length > 0) {
      void handleFiles(e.target.files);
      e.target.value = "";
    }
  }

  async function handleDelete(id: number): Promise<void> {
    // Collapse BEFORE the request: unmounts the row's ChunkExpansion (and
    // its chunks query subscription) so nothing can refetch chunks for a
    // document that is about to disappear from the list.
    if (expandedId === id) setExpandedId(null);
    try {
      await deleteDoc.mutateAsync(id);
      push({ variant: "info", message: "Document deleted." });
    } catch {
      push({
        variant: "error",
        message: "Couldn't delete that document — try again.",
      });
    }
  }

  function handleToggleExpand(id: number): void {
    setExpandedId((prev) => (prev === id ? null : id));
  }

  const body = (
    <div className={inRail ? "rr-page rr-page--rail" : "rr-page"}>
      {/* Rail mount: the Drawer banner already says "Documents". */}
      {!inRail && (
        <header className="rr-page-header">
          {/* Context-specific eyebrow */}
          <span className="rr-eyebrow">The Desk</span>
          <h1 className="rr-page-title">Documents</h1>
        </header>
      )}

      {/* Upload zone — vellum-tinted panel, NOT a solid box */}
      <div
        className={`rr-upload-zone${isDragging ? " rr-upload-zone--drag" : ""}`}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        role="button"
        tabIndex={0}
        aria-label="Drop files here or click to upload"
        onClick={() => {
          fileInputRef.current?.click();
        }}
        onKeyDown={(e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            fileInputRef.current?.click();
          }
        }}
      >
        <input
          ref={fileInputRef}
          type="file"
          multiple
          accept=".txt,.md,.html,.pdf,.epub,.docx"
          style={{ display: "none" }}
          onChange={handleInputChange}
          aria-label="File input"
        />
        {upload.isPending ? (
          <span className="rr-upload-zone-label">Uploading…</span>
        ) : (
          <span className="rr-upload-zone-label">
            Drop files here or click to upload
            <span className="rr-upload-zone-hint">
              .txt · .md · .html · .pdf · .epub · .docx
            </span>
          </span>
        )}
      </div>

      {/* Document list */}
      {isLoading && <p className="rr-hint">Loading documents…</p>}
      {isError && (
        <p className="rr-hint rr-hint--error">
          Couldn't load documents — try again.
        </p>
      )}
      {!isLoading && !isError && docs.length === 0 && (
        /* Heading copy avoids the word "Documents" so flow-11's
           getByRole('heading', { name: 'Documents' }) keeps resolving to
           exactly one element (the page <h1>). */
        <div
          className="rr-empty-state rr-empty-state--documents"
          data-testid="documents-empty-state"
        >
          <h3 className="rr-empty-title">No documents yet.</h3>
          <p className="rr-empty-marginalia">
            Drop a PDF or paste a URL. The model will pull from them when you
            ask.
          </p>
        </div>
      )}

      {/* The desk — document rows on canvas, GROUP gap */}
      <ul className="rr-doc-list">
        {docs.map((doc) => (
          <DocumentRow
            key={doc.id}
            doc={doc}
            onDelete={(id) => {
              void handleDelete(id);
            }}
            isExpanded={expandedId === doc.id}
            onToggleExpand={handleToggleExpand}
          />
        ))}
      </ul>
    </div>
  );
  return inRail ? body : <AppShell>{body}</AppShell>;
}
