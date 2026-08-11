/* SPDX-License-Identifier: Apache-2.0 */
/**
 * PromptLibrary page — manage user-owned prompt presets.
 *
 * Route: /prompts (lazy-loaded chunk).
 * Links from UserMenu dropdown.
 *
 * Features:
 * - List all prompts sorted by name.
 * - Create form (name + content textarea).
 * - Inline edit dialog (edit name/content of an existing prompt).
 * - Delete button per prompt.
 *
 * Composer integration:
 * The /prompt <name> slash command is handled in Composer.tsx using
 * `usePrompts()` directly — this page is the management view.
 *
 * Inline CSSProperties replaced with reading-rooms.css + settings.css
 * semantic classes. Prompt rows use the "recipe collection" design: no card
 * chrome, title typography + italic preview carries the row.
 */
import { useState } from "react";
import { AppShell } from "@/components/AppShell";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import {
  usePrompts,
  useCreatePrompt,
  useUpdatePrompt,
  useDeletePrompt,
} from "@/hooks/usePrompts";
import type { Prompt } from "@/hooks/usePrompts";
import { useToast } from "@/stores/toastStore";
import { useDropdownKeyboard } from "@/hooks/useDropdownKeyboard";
import "@/styles/reading-rooms.css";
import "@/styles/settings.css";

export default function PromptLibrary() {
  useDocumentTitle("Prompts");
  const { data: prompts, isLoading, isError } = usePrompts();
  const { push } = useToast();

  const [createName, setCreateName] = useState("");
  const [createContent, setCreateContent] = useState("");
  const createPrompt = useCreatePrompt();

  const [editingId, setEditingId] = useState<number | null>(null);
  const [editName, setEditName] = useState("");
  const [editContent, setEditContent] = useState("");

  async function handleCreate(e: {
    preventDefault: () => void;
  }): Promise<void> {
    e.preventDefault();
    if (!createName.trim() || !createContent.trim()) return;
    try {
      await createPrompt.mutateAsync({
        name: createName.trim(),
        content: createContent.trim(),
      });
      setCreateName("");
      setCreateContent("");
    } catch {
      push({
        variant: "error",
        message: "Couldn't create that prompt — the name may already be taken.",
      });
    }
  }

  function startEdit(p: Prompt) {
    setEditingId(p.id);
    setEditName(p.name);
    setEditContent(p.content);
  }

  function cancelEdit() {
    setEditingId(null);
  }

  return (
    <AppShell>
      <div className="rr-page">
        <header className="rr-page-header">
          {/* Context-specific eyebrow */}
          <span className="rr-eyebrow">The Recipes</span>
          <h1 className="rr-page-title">Prompts</h1>
        </header>

        {/* Create form — .lmchat-form pattern from settings.css */}
        <div className="rr-prompt-create">
          <h2 className="rr-prompt-category-heading">New prompt</h2>
          <form
            onSubmit={(e) => {
              void handleCreate(e);
            }}
            className="lmchat-form"
          >
            <div className="lmchat-field">
              <label className="lmchat-field-label" htmlFor="prompt-name">
                Name
              </label>
              <input
                id="prompt-name"
                type="text"
                placeholder="e.g. summarize-code"
                value={createName}
                onChange={(e) => {
                  setCreateName(e.target.value);
                }}
                className="lmchat-input"
                aria-label="Prompt name"
                maxLength={256}
              />
            </div>
            <div className="lmchat-field">
              <label className="lmchat-field-label" htmlFor="prompt-content">
                Content
              </label>
              <textarea
                id="prompt-content"
                placeholder="Prompt content…"
                value={createContent}
                onChange={(e) => {
                  setCreateContent(e.target.value);
                }}
                className="lmchat-input lmchat-textarea"
                aria-label="Prompt content"
                rows={3}
                // Mirror the backend PROMPT_CONTENT_MAX_LENGTH
                // to enforce the cap locally before round-trip.
                maxLength={16384}
              />
            </div>
            <div className="lmchat-form-actions">
              <button
                type="submit"
                disabled={
                  createPrompt.isPending ||
                  !createName.trim() ||
                  !createContent.trim()
                }
                className="lmchat-btn-primary"
              >
                {createPrompt.isPending ? "Creating…" : "Create prompt"}
              </button>
            </div>
          </form>
        </div>

        {/* Prompt list — recipe collection */}
        <div className="rr-prompt-category">
          <h2 className="rr-prompt-category-heading">Your prompts</h2>

          {isLoading && <p className="rr-hint">Loading…</p>}
          {isError && (
            <p className="rr-hint rr-hint--error">
              Couldn't load prompts — try again.
            </p>
          )}
          {!isLoading && !isError && prompts?.length === 0 && (
            <div className="rr-empty-state rr-empty-state--prompts">
              <h3 className="rr-empty-title">The recipes are empty.</h3>
              <p className="rr-empty-marginalia">
                Save a prompt you keep rewriting. Use it from the slash palette
                next time.
              </p>
            </div>
          )}

          {prompts && prompts.length > 0 && (
            <ul className="rr-prompt-list">
              {prompts.map((p) =>
                editingId === p.id ? (
                  <EditPromptRow
                    key={p.id}
                    prompt={p}
                    editName={editName}
                    editContent={editContent}
                    onNameChange={setEditName}
                    onContentChange={setEditContent}
                    onCancel={cancelEdit}
                    onSaved={cancelEdit}
                    onError={() => {
                      push({
                        variant: "error",
                        message: "Couldn't update that prompt — try again.",
                      });
                    }}
                  />
                ) : (
                  <PromptRow
                    key={p.id}
                    prompt={p}
                    onEdit={() => {
                      startEdit(p);
                    }}
                    onDeleteError={() => {
                      push({
                        variant: "error",
                        message: "Couldn't delete that prompt — try again.",
                      });
                    }}
                  />
                ),
              )}
            </ul>
          )}
        </div>
      </div>
    </AppShell>
  );
}

// ─── PromptRow ───────────────────────────────────────────────────────────────

interface PromptRowProps {
  prompt: Prompt;
  onEdit: () => void;
  onDeleteError: () => void;
}

function PromptRow({ prompt, onEdit, onDeleteError }: PromptRowProps) {
  const deletePrompt = useDeletePrompt();

  async function handleDelete(): Promise<void> {
    try {
      await deletePrompt.mutateAsync(prompt.id);
    } catch {
      onDeleteError();
    }
  }

  return (
    <li className="rr-prompt-row">
      {/* Title + 2-line italic preview */}
      <div className="rr-prompt-body">
        <p className="rr-prompt-title">{prompt.name}</p>
        <p className="rr-prompt-preview">
          {prompt.content.slice(0, 120)}
          {prompt.content.length > 120 ? "…" : ""}
        </p>
      </div>
      {/* "use →" affordance — fades in on row hover */}
      <span className="rr-prompt-use-hint" aria-hidden="true">
        use →
      </span>
      {/* Quiet action cluster on the right */}
      <div className="rr-prompt-actions">
        <button type="button" onClick={onEdit} className="lmchat-btn-ghost">
          Edit
        </button>
        <button
          type="button"
          onClick={() => {
            void handleDelete();
          }}
          disabled={deletePrompt.isPending}
          className="lmchat-btn-danger"
        >
          Delete
        </button>
      </div>
    </li>
  );
}

// ─── EditPromptRow ───────────────────────────────────────────────────────────

interface EditPromptRowProps {
  prompt: Prompt;
  editName: string;
  editContent: string;
  onNameChange: (v: string) => void;
  onContentChange: (v: string) => void;
  onCancel: () => void;
  onSaved: () => void;
  onError: () => void;
}

function EditPromptRow({
  prompt,
  editName,
  editContent,
  onNameChange,
  onContentChange,
  onCancel,
  onSaved,
  onError,
}: EditPromptRowProps) {
  const updatePrompt = useUpdatePrompt(prompt.id);

  const { containerProps } = useDropdownKeyboard({
    open: true,
    onClose: onCancel,
    itemSelector: "input, textarea, button",
  });

  async function handleSave(e: { preventDefault: () => void }): Promise<void> {
    e.preventDefault();
    try {
      await updatePrompt.mutateAsync({
        name: editName.trim(),
        content: editContent.trim(),
      });
      onSaved();
    } catch {
      onError();
    }
  }

  return (
    <li
      className="rr-prompt-row"
      style={{ flexDirection: "column", alignItems: "stretch" }}
    >
      <form
        onSubmit={(e) => {
          void handleSave(e);
        }}
        className="rr-prompt-edit-form lmchat-form"
        onKeyDown={containerProps.onKeyDown}
      >
        <div className="lmchat-field">
          <label
            className="lmchat-field-label"
            htmlFor={`edit-name-${prompt.id.toString()}`}
          >
            Name
          </label>
          <input
            id={`edit-name-${prompt.id.toString()}`}
            type="text"
            value={editName}
            onChange={(e) => {
              onNameChange(e.target.value);
            }}
            className="lmchat-input"
            aria-label="Edit prompt name"
            maxLength={256}
          />
        </div>
        <div className="lmchat-field">
          <label
            className="lmchat-field-label"
            htmlFor={`edit-content-${prompt.id.toString()}`}
          >
            Content
          </label>
          <textarea
            id={`edit-content-${prompt.id.toString()}`}
            value={editContent}
            onChange={(e) => {
              onContentChange(e.target.value);
            }}
            className="lmchat-input lmchat-textarea"
            aria-label="Edit prompt content"
            rows={4}
            maxLength={16384}
          />
        </div>
        <div className="rr-prompt-edit-actions">
          <button
            type="submit"
            disabled={updatePrompt.isPending}
            className="lmchat-btn-primary"
          >
            {updatePrompt.isPending ? "Saving…" : "Save"}
          </button>
          <button
            type="button"
            onClick={onCancel}
            className="lmchat-btn-secondary"
          >
            Cancel
          </button>
        </div>
      </form>
    </li>
  );
}
