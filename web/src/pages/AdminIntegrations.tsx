/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Admin: MCP Integrations — /admin/integrations
 *
 * Frontend integrations picker; mitigates the risk of a stale
 * integrations list warning.
 *
 * WHY this page exists
 * --------------------
 * LM Studio does not expose MCP server enumeration over HTTP.  The admin
 * must supply the list of integration IDs (e.g. "mcp/searxng") manually so
 * lm-chat's chat composer can offer a picker.  This page provides the in-app
 * UI for managing the DB-backed list.  The env var (LM_CHAT_AVAILABLE_INTEGRATIONS)
 * is the fallback when the DB list is empty.
 *
 * Stale-list banner
 * -----------------
 * An in-app callout reminds the admin that the list is manually supplied
 * and does not auto-sync with LM Studio's mcp.json.
 */
import { useState, type ChangeEvent, type SubmitEvent } from "react";
import { X } from "lucide-react";
import {
  useIntegrationsList,
  useUpdateIntegrationsList,
} from "@/hooks/useIntegrationsList";
import type { IntegrationSetEntry } from "@/hooks/useIntegrationsList";
import { useToast } from "@/stores/toastStore";
import "@/styles/admin.css";

// ─── Component ───────────────────────────────────────────────────────────────

export default function AdminIntegrations() {
  const { data: entries = [], isLoading, error } = useIntegrationsList();
  const updateMutation = useUpdateIntegrationsList();
  const { push } = useToast();

  // Local edit state: mirror of the DB list, plus add-new input.
  const [localEntries, setLocalEntries] = useState<
    IntegrationSetEntry[] | null
  >(null);
  const [newValue, setNewValue] = useState("");

  // Use local state if the user has started editing; otherwise mirror server data.
  const displayEntries: IntegrationSetEntry[] =
    localEntries ??
    entries.map((e) => ({
      value: e.value,
      sort_order: e.sort_order,
      enabled_by_default: e.enabled_by_default === true,
    }));

  function handleRemove(index: number): void {
    const updated = displayEntries.filter((_, i) => i !== index);
    setLocalEntries(updated.map((e, i) => ({ ...e, sort_order: i })));
  }

  function handleToggleDefault(index: number): void {
    const updated = displayEntries.map((entry, i) =>
      i === index
        ? { ...entry, enabled_by_default: !(entry.enabled_by_default === true) }
        : entry,
    );
    setLocalEntries(updated);
  }

  function handleAdd(e: SubmitEvent<HTMLFormElement>): void {
    e.preventDefault();
    const trimmed = newValue.trim();
    if (!trimmed) return;
    if (displayEntries.some((e) => e.value === trimmed)) {
      push({
        variant: "warning",
        message: `"${trimmed}" is already in the list.`,
      });
      return;
    }
    setLocalEntries([
      ...displayEntries,
      {
        value: trimmed,
        sort_order: displayEntries.length,
        enabled_by_default: false,
      },
    ]);
    setNewValue("");
  }

  function handleSave(): void {
    updateMutation.mutate(displayEntries, {
      onSuccess: () => {
        setLocalEntries(null);
        push({ variant: "success", message: "Integrations list saved." });
      },
      onError: (err) => {
        console.error("[admin-integrations] save failed:", err.message);
        push({
          variant: "error",
          message: "Couldn't save the integrations list — try again.",
        });
      },
    });
  }

  function handleReset(): void {
    setLocalEntries(null);
  }

  const isDirty = localEntries !== null;

  return (
    <div className="admin-page">
      <div className="admin-page-header">
        {/* context-specific eyebrow */}
        <span className="admin-eyebrow">Admin</span>
        <h1 className="admin-page-title">Admin: Integrations</h1>
      </div>

      {/* Stale-list warning banner */}
      <div
        role="note"
        aria-label="Operator note"
        className="admin-banner admin-banner--warning"
        data-testid="r10-banner"
      >
        <strong>List is operator-supplied</strong> — LM Studio does not expose
        MCP server enumeration over HTTP. When you install or remove an MCP
        server in LM Studio's desktop UI, update this list to match.{" "}
        <code>mcp.json</code> changes are not auto-discovered. See{" "}
        <a
          href="/docs/05-mcp-and-tools"
        >
          MCP &amp; Tools guide
        </a>{" "}
        and <code>LM_CHAT_AVAILABLE_INTEGRATIONS</code> in{" "}
        <code>.env.local.example</code> for the env-var alternative.
      </div>

      {/* Current list */}
      <section>
        <h2 className="admin-section-heading">Available integrations</h2>
        <p className="admin-help-text">
          Each entry is an integration ID passed verbatim to LM Studio's{" "}
          <code className="admin-inline-code">/api/v1/chat</code>{" "}
          <code className="admin-inline-code">integrations</code> field when the
          user toggles it on in the chat composer. Format:{" "}
          <code className="admin-inline-code">mcp/&lt;server-name&gt;</code>.
        </p>

        {isLoading && <p className="admin-empty">Loading…</p>}
        {error && <p className="admin-error">Error: {error.message}</p>}

        {!isLoading && displayEntries.length === 0 && (
          <p className="admin-empty">
            No integrations configured. Add entries below, or set{" "}
            <code className="admin-inline-code">
              LM_CHAT_AVAILABLE_INTEGRATIONS
            </code>{" "}
            in the deployment env.
          </p>
        )}

        {displayEntries.length > 0 && (
          <ul className="admin-int-list" data-testid="integrations-list">
            {displayEntries.map((entry, i) => {
              const isDefault = entry.enabled_by_default === true;
              return (
                <li key={entry.value} className="admin-int-item">
                  <span className="admin-int-tag">{entry.value}</span>
                  <label
                    className={`admin-default-toggle${isDefault ? " admin-default-toggle--active" : ""}`}
                    data-testid={`default-on-toggle-${entry.value}`}
                  >
                    <input
                      type="checkbox"
                      checked={isDefault}
                      onChange={() => {
                        handleToggleDefault(i);
                      }}
                      style={{ margin: 0, cursor: "pointer" }}
                      aria-label={`Toggle Default on for ${entry.value}`}
                    />
                    <span>Default on</span>
                  </label>
                  <button
                    type="button"
                    onClick={() => {
                      handleRemove(i);
                    }}
                    aria-label={`Remove ${entry.value}`}
                    className="admin-remove-btn"
                    data-testid={`remove-btn-${entry.value}`}
                  >
                    <X size={12} aria-hidden />
                  </button>
                </li>
              );
            })}
          </ul>
        )}

        {/* Add new entry */}
        <form
          onSubmit={handleAdd}
          className="admin-add-form"
          data-testid="add-form"
        >
          <input
            type="text"
            value={newValue}
            onChange={(e: ChangeEvent<HTMLInputElement>) => {
              setNewValue(e.target.value);
            }}
            placeholder="mcp/server-name"
            aria-label="New integration ID"
            className="admin-add-input"
            data-testid="add-input"
          />
          <button
            type="submit"
            disabled={!newValue.trim()}
            className="admin-btn-primary"
            data-testid="add-btn"
          >
            Add
          </button>
        </form>
      </section>

      {/* Save / Reset */}
      <div
        className="admin-action-group"
        style={{ marginTop: "var(--space-group)" }}
      >
        <button
          type="button"
          onClick={handleSave}
          disabled={updateMutation.isPending || !isDirty}
          className="admin-btn-primary"
          data-testid="save-btn"
        >
          {updateMutation.isPending ? "Saving…" : "Save"}
        </button>
        {isDirty && (
          <button
            type="button"
            onClick={handleReset}
            disabled={updateMutation.isPending}
            className="admin-btn-secondary"
            data-testid="reset-btn"
          >
            Reset
          </button>
        )}
      </div>

      {/* Source indicator */}
      {!isLoading && entries.length > 0 && entries[0]?.id !== -1 && (
        <p className="admin-source-note">
          Source: <strong>DB</strong> (admin-managed). DB entries take
          precedence over the env var.
        </p>
      )}
      {!isLoading && entries.length > 0 && entries[0]?.id === -1 && (
        <p className="admin-source-note">
          Source: <strong>env var</strong> (
          <code className="admin-inline-code">
            LM_CHAT_AVAILABLE_INTEGRATIONS
          </code>
          ). Save a list via this page to switch to DB-backed management.
        </p>
      )}
    </div>
  );
}
