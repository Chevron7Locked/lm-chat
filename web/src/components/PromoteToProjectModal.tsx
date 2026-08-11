/* SPDX-License-Identifier: Apache-2.0 */
/**
 * PromoteToProjectModal — "Turn this chat into a Project."
 *
 * Promotes an existing chat into a brand-new project. The chat's message
 * history, compactions, and embeddings are all chat_id-scoped and travel
 * for free the moment the backend flips ``chats.project_id`` — this modal
 * only collects the new project's identity (name + custom instructions)
 * and which un-projected documents should move along with it.
 *
 * Opened from the chat's ⋯ overflow menu ("New project from this chat").
 * The menu item itself is gated off (not rendered) when the chat already
 * belongs to a project, is incognito, or has an open sub-session — see
 * ``pages/Chat.tsx``'s TopBar wiring for the exact gate; this modal
 * assumes the caller already applied it.
 */
import { useEffect, useId, useRef, useState, type SubmitEvent } from "react";
import { useNavigate } from "react-router-dom";
import { usePromoteChatToProject } from "@/hooks/useProjects";
import { useDocuments } from "@/hooks/useDocuments";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { useToast } from "@/stores/toastStore";
import "@/styles/promote-to-project-modal.css";

export interface PromoteToProjectModalProps {
  open: boolean;
  onClose: () => void;
  chatId: number;
  /** Prefills the project name field. */
  chatTitle: string;
  /** Default-checked in the document picker when present + un-projected. */
  focusedDocumentId?: number | null;
}

export function PromoteToProjectModal({
  open,
  onClose,
  chatId,
  chatTitle,
  focusedDocumentId,
}: PromoteToProjectModalProps) {
  const dialogRef = useRef<HTMLDivElement>(null);
  const nameInputRef = useRef<HTMLInputElement>(null);
  const titleId = useId();
  const navigate = useNavigate();
  const { push } = useToast();
  const { data: allDocuments } = useDocuments();
  const promote = usePromoteChatToProject(chatId);

  const [name, setName] = useState(chatTitle);
  const [systemPrompt, setSystemPrompt] = useState("");
  const [selectedIds, setSelectedIds] = useState<Set<number>>(new Set());
  // Only auto-check the focused document ONCE per open — otherwise every
  // unrelated doc-list refetch would silently re-check a box the user
  // just unchecked.
  const didAutoFocusRef = useRef(false);

  const unprojectedDocuments = (allDocuments ?? []).filter(
    (d) => d.project_id == null,
  );

  // Reset the form's local state every time the modal opens.
  useEffect(() => {
    if (!open) return;
    setName(chatTitle);
    setSystemPrompt("");
    setSelectedIds(new Set());
    didAutoFocusRef.current = false;
  }, [open, chatTitle]);

  useEffect(() => {
    if (!open || didAutoFocusRef.current) return;
    if (focusedDocumentId == null) return;
    if (unprojectedDocuments.some((d) => d.id === focusedDocumentId)) {
      setSelectedIds(new Set([focusedDocumentId]));
      didAutoFocusRef.current = true;
    }
  }, [open, focusedDocumentId, unprojectedDocuments]);

  useFocusTrap(dialogRef, open, { initialFocusRef: nameInputRef });

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent): void {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    }
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
    };
  }, [open, onClose]);

  if (!open) return null;

  function toggleDoc(id: number): void {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) {
        next.delete(id);
      } else {
        next.add(id);
      }
      return next;
    });
  }

  function handleSubmit(e: SubmitEvent<HTMLFormElement>): void {
    e.preventDefault();
    const trimmedName = name.trim();
    promote.mutate(
      {
        ...(trimmedName !== "" ? { name: trimmedName } : {}),
        system_prompt: systemPrompt,
        document_ids: Array.from(selectedIds),
      },
      {
        onSuccess: (created) => {
          const movedSuffix =
            created.moved_document_count > 0
              ? ` — ${String(created.moved_document_count)} document${
                  created.moved_document_count === 1 ? "" : "s"
                } moved`
              : "";
          push({
            variant: "success",
            message: `"${created.name}" created${movedSuffix}.`,
          });
          onClose();
          void navigate(`/project/${String(created.id)}`);
        },
        onError: (err) => {
          const detail =
            (err as { detail?: unknown }).detail ??
            (err instanceof Error ? err.message : String(err));
          const suffix =
            typeof detail === "string" && detail.length > 0
              ? ` — ${detail}`
              : "";
          push({
            variant: "error",
            message: `Couldn't create the project${suffix}`,
          });
        },
      },
    );
  }

  return (
    <div
      className="ptp-backdrop"
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="ptp-card"
        data-testid="promote-to-project-modal"
      >
        <form onSubmit={handleSubmit} className="ptp-form">
          <header className="ptp-header">
            <h2 id={titleId} className="ptp-title">
              New project from this chat
            </h2>
          </header>

          <div className="ptp-body">
            <label htmlFor="ptp-name" className="ptp-label">
              Project name
            </label>
            <input
              ref={nameInputRef}
              id="ptp-name"
              type="text"
              value={name}
              onChange={(e) => {
                setName(e.target.value);
              }}
              className="ptp-input"
              data-testid="promote-to-project-name"
              maxLength={256}
            />

            <label htmlFor="ptp-instructions" className="ptp-label">
              Custom instructions{" "}
              <span className="ptp-label-optional">(optional)</span>
            </label>
            <textarea
              id="ptp-instructions"
              value={systemPrompt}
              onChange={(e) => {
                setSystemPrompt(e.target.value);
              }}
              className="ptp-input ptp-textarea"
              placeholder="How should this project's assistant behave?"
              data-testid="promote-to-project-instructions"
            />

            <span className="ptp-label">
              Bring documents{" "}
              <span className="ptp-label-optional">(optional)</span>
            </span>
            {unprojectedDocuments.length === 0 ? (
              <p className="ptp-empty">
                No un-projected documents to bring along.
              </p>
            ) : (
              <ul
                className="ptp-doc-list"
                data-testid="promote-to-project-doc-list"
              >
                {unprojectedDocuments.map((doc) => (
                  <li key={doc.id} className="ptp-doc-item">
                    <label className="ptp-doc-label">
                      <input
                        type="checkbox"
                        className="ptp-doc-checkbox"
                        checked={selectedIds.has(doc.id)}
                        onChange={() => {
                          toggleDoc(doc.id);
                        }}
                        data-testid={`promote-to-project-doc-${String(doc.id)}`}
                      />
                      <span className="ptp-doc-title">{doc.title}</span>
                    </label>
                  </li>
                ))}
              </ul>
            )}
          </div>

          <footer className="ptp-footer">
            <button
              type="button"
              onClick={onClose}
              className="ptp-cancel-btn"
              data-testid="promote-to-project-cancel"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={promote.isPending}
              className="ptp-submit-btn"
              data-testid="promote-to-project-submit"
            >
              {promote.isPending ? "Creating…" : "Create project"}
            </button>
          </footer>
        </form>
      </div>
    </div>
  );
}
