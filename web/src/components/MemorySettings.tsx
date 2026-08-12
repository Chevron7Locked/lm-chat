/* SPDX-License-Identifier: Apache-2.0 */
/**
 * MemorySettings — Settings → Memory tab.
 *
 * Provides controlled toggles for auto-memory + sub-session memory,
 * a web-search provider selector with conditional SearXNG URL input,
 * and an admin-only gating layer.  Non-admin users see read-only
 * status indicators (the `is_override` flag from the backend).
 *
 * Reuses the read-only MemoryIndexingCard from LmStudioSection for
 * the embedding/indexing status readout.
 *
 * API contract (already shipped):
 *   GET  /api/settings/app  →  {
 *     memory_distillation_enabled: { value: bool|null, is_override: bool },
 *     subsession_memory_distillation_enabled: { value: bool|null, is_override: bool },
 *     web_search_provider: { value: str|null, is_override: bool },
 *     searxng_url: { value: str|null, is_override: bool },
 *     repeat_warning_cut_k: { value: int|null, is_override: bool },
 *   }
 *   PATCH /api/settings/app (admin)  →  body any subset of:
 *     { memory_distillation_enabled?: bool|null,
 *       subsession_memory_distillation_enabled?: bool|null,
 *       web_search_provider?: 'searxng'|'ddg'|'brave'|'brave_llm'|null,
 *       searxng_url?: string|null,
 *       repeat_warning_cut_k?: number|null }
 *   A field set to null clears the override (reverts to config default).
 *   Omitted fields are unchanged.  Non-admin PATCH → 403.
 */
import { useState, type ChangeEvent, type JSX } from "react";
import { useQuery, useMutation } from "@tanstack/react-query";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { useAuthStore } from "@/stores/authStore";
import { MemoryIndexingCard } from "@/components/LmStudioSection";
import "@/styles/settings.css";

// ─── Types ────────────────────────────────────────────────────────────────────

interface OverrideField<T> {
  value: T | null;
  is_override: boolean;
}

interface AppSettingsResponse {
  memory_distillation_enabled: OverrideField<boolean>;
  subsession_memory_distillation_enabled: OverrideField<boolean>;
  web_search_provider: OverrideField<"searxng" | "ddg" | "brave" | "brave_llm" | null>;
  searxng_url: OverrideField<string>;
  repeat_warning_cut_k: OverrideField<number>;
}

type AppSettingsPatch = Partial<{
  memory_distillation_enabled: boolean | null;
  subsession_memory_distillation_enabled: boolean | null;
  web_search_provider: "searxng" | "ddg" | "brave" | "brave_llm" | null;
  searxng_url: string | null;
  repeat_warning_cut_k: number | null;
}>;

// ─── Helpers ──────────────────────────────────────────────────────────────────

function OverrideBadge({ isOverride }: { isOverride: boolean }): JSX.Element {
  return (
    <span
      className="lmchat-field-hint"
      style={{ marginLeft: "var(--space-glue)", fontStyle: "italic" }}
      data-testid="settings-memory-override-badge"
    >
      {isOverride ? "(override)" : "(default)"}
    </span>
  );
}

function AdminChip(): JSX.Element {
  return (
    <span
      style={{
        display: "inline-block",
        padding: "1px var(--space-glue-relaxed)",
        marginLeft: "var(--space-glue-relaxed)",
        background: "var(--color-surface-elevated)",
        border: "1px solid var(--color-border-default)",
        borderRadius: "var(--radius-pill)",
        fontFamily: "var(--font-display)",
        fontSize: "var(--fs-label)",
        textTransform: "uppercase" as const,
        letterSpacing: "var(--ls-caps)",
        color: "var(--color-text-subtle)",
        cursor: "help",
        verticalAlign: "middle",
      }}
      title="Configured by your administrator; contact them to change this limit."
      data-testid="settings-memory-admin-chip"
    >
      admin-set
    </span>
  );
}

// ─── Main component ───────────────────────────────────────────────────────────

export function MemorySettings(): JSX.Element {
  const isAdmin = useAuthStore((s) => s.user?.is_admin ?? false);
  const qc = useQueryClient();

  const [editingSearxngUrl, setEditingSearxngUrl] = useState(false);
  const [searxngUrlDraft, setSearxngUrlDraft] = useState("");
  const [editingRepeatWarningCutK, setEditingRepeatWarningCutK] = useState(false);
  const [repeatWarningCutKDraft, setRepeatWarningCutKDraft] = useState("");

  // ── Query ────────────────────────────────────────────────────────────────
  const { data, isLoading, isError } = useQuery<AppSettingsResponse, ApiError>({
    queryKey: ["app-settings"],
    queryFn: () =>
      api.request<AppSettingsResponse>("/api/settings/app"),
    staleTime: 30_000,
  });

  // ── Mutation ─────────────────────────────────────────────────────────────
  const patchMutation = useMutation<AppSettingsResponse, ApiError, AppSettingsPatch, { previous: AppSettingsResponse | undefined }>({
    mutationFn: (body: AppSettingsPatch) =>
      api.request<AppSettingsResponse>("/api/settings/app", {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    onMutate: (variables) => {
      // Snapshot current data for rollback.
      const previous = qc.getQueryData<AppSettingsResponse>(["app-settings"]);
      // Optimistically update.
      if (previous) {
        qc.setQueryData<AppSettingsResponse>(["app-settings"], {
          ...previous,
          ...Object.fromEntries(
            Object.entries(variables).map(([key, val]) => [
              key,
              { value: val, is_override: true },
            ]),
          ) as unknown as AppSettingsResponse,
        });
      }
      return { previous };
    },
    onError: (_err, _vars, context) => {
      // Rollback on error.
      if (context?.previous) {
        qc.setQueryData(["app-settings"], context.previous);
      }
    },
    onSettled: () => {
      // Always refetch to confirm server state.
      void qc.invalidateQueries({ queryKey: ["app-settings"] });
    },
  });

  // ── Handlers ─────────────────────────────────────────────────────────────

  function handleMasterToggle(e: ChangeEvent<HTMLInputElement>): void {
    if (!isAdmin) return;
    patchMutation.mutate({
      memory_distillation_enabled: e.target.checked,
    });
  }

  function handleSubsessionToggle(e: ChangeEvent<HTMLInputElement>): void {
    if (!isAdmin) return;
    patchMutation.mutate({
      subsession_memory_distillation_enabled: e.target.checked,
    });
  }

  function handleSearchProviderChange(e: ChangeEvent<HTMLSelectElement>): void {
    if (!isAdmin) return;
    const value =
      e.target.value === ""
        ? null
        : (e.target.value as "searxng" | "ddg" | "brave" | "brave_llm");
    patchMutation.mutate({ web_search_provider: value });
    if (value !== "searxng") {
      // Clear the URL override when switching away from SearXNG.
      patchMutation.mutate({ searxng_url: null });
      setEditingSearxngUrl(false);
    }
  }

  function handleSearxngUrlSave(): void {
    if (!isAdmin) return;
    patchMutation.mutate({ searxng_url: searxngUrlDraft || null });
    setEditingSearxngUrl(false);
  }

  function handleSearxngUrlCancel(): void {
    setEditingSearxngUrl(false);
  }

  function handleRepeatWarningCutKSave(): void {
    if (!isAdmin) return;
    const trimmed = repeatWarningCutKDraft.trim();
    if (trimmed === "") {
      // Empty draft clears the override (reverts to config default) —
      // mirrors the searxng_url save-empty-to-clear convention above.
      patchMutation.mutate({ repeat_warning_cut_k: null });
      setEditingRepeatWarningCutK(false);
      return;
    }
    const num = Number(trimmed);
    if (!Number.isFinite(num)) return;
    const clamped = Math.max(0, Math.min(100, Math.trunc(num)));
    patchMutation.mutate({ repeat_warning_cut_k: clamped });
    setEditingRepeatWarningCutK(false);
  }

  function handleRepeatWarningCutKCancel(): void {
    setEditingRepeatWarningCutK(false);
  }

  // ── Read values ──────────────────────────────────────────────────────────
  const masterValue = data?.memory_distillation_enabled;
  const subValue = data?.subsession_memory_distillation_enabled;
  const searchProvider = data?.web_search_provider;
  const searxngUrlField = data?.searxng_url;
  const repeatWarningCutKField = data?.repeat_warning_cut_k;

  if (isLoading) {
    return (
      <div
        className="lmchat-section-container"
        data-testid="settings-memory-section"
      >
        <p className="lmchat-section-description">Loading memory settings…</p>
      </div>
    );
  }

  if (isError) {
    return (
      <div
        className="lmchat-section-container"
        data-testid="settings-memory-section"
      >
        <p
          className="lmchat-form-error"
          role="alert"
          data-testid="settings-memory-error"
        >
          Couldn't load memory settings.
        </p>
      </div>
    );
  }

  return (
    <div
      className="lmchat-section-container"
      data-testid="settings-memory-section"
    >
        {/* ── Auto-memory toggle ───────────────────────────────────────────── */}
        <section className="lmchat-section" aria-label="Auto memory">
          <h3 className="lmchat-section-heading">Auto memory</h3>
          <p className="lmchat-section-description">
            When enabled, LM Chat automatically remembers facts from your
            conversations.
          </p>
          <div className="lmchat-meta-block">
            <div className="lmchat-field-row">
              {isAdmin ? (
                <label
                  className="lmchat-mcp-toggle"
                  data-testid="settings-memory-master-toggle-label"
                >
                  <input
                    type="checkbox"
                    className="lmchat-mcp-toggle__input"
                    checked={masterValue?.value ?? false}
                    onChange={handleMasterToggle}
                    aria-label="Remember facts automatically"
                    data-testid="settings-memory-master-toggle"
                  />
                  <span className="lmchat-mcp-toggle__track" aria-hidden="true" />
                  <span className="lmchat-toggle-hint">
                    {masterValue?.value ? "On" : "Off"}
                  </span>
                  <OverrideBadge isOverride={masterValue?.is_override ?? false} />
                </label>
                ) : (
                  <div className="lmchat-field-row" data-testid="settings-memory-master-toggle-label">
                    <span className="lmchat-field-row-label">
                      {masterValue?.value ? "On" : "Off"}
                    </span>
                    <OverrideBadge isOverride={masterValue?.is_override ?? false} />
                  </div>
                )}
            </div>
          </div>
        </section>

      {/* ── Divider ──────────────────────────────────────────────────────── */}
      <hr className="lmchat-section-divider" aria-hidden="true" />

        {/* ── Sub-session memory toggle ────────────────────────────────────── */}
        <section className="lmchat-section" aria-label="Sub-session memory">
          <div className="lmchat-meta-block">
            <div className="lmchat-field-row">
              {isAdmin ? (
                !masterValue?.value ? (
                  <>
                    <label
                      className="lmchat-mcp-toggle"
                      data-testid="settings-memory-subsession-toggle-label"
                      aria-disabled="true"
                    >
                      <input
                        type="checkbox"
                        className="lmchat-mcp-toggle__input"
                        checked={subValue?.value ?? false}
                        disabled
                        aria-label="Also remember from research / sub-agent turns"
                        data-testid="settings-memory-subsession-toggle"
                      />
                      <span className="lmchat-mcp-toggle__track" aria-hidden="true" />
                      <span className="lmchat-toggle-hint">
                        {subValue?.value ? "On" : "Off"}
                      </span>
                      <OverrideBadge isOverride={subValue?.is_override ?? false} />
                    </label>
                    <p
                      className="lmchat-field-hint"
                      style={{ marginTop: "var(--space-glue-relaxed)" }}
                      data-testid="settings-memory-subsession-hint"
                    >
                      Turn on automatic memory first.
                    </p>
                  </>
                ) : (
                  <label
                    className="lmchat-mcp-toggle"
                    data-testid="settings-memory-subsession-toggle-label"
                  >
                    <input
                      type="checkbox"
                      className="lmchat-mcp-toggle__input"
                      checked={subValue?.value ?? false}
                      onChange={handleSubsessionToggle}
                      aria-label="Also remember from research / sub-agent turns"
                      data-testid="settings-memory-subsession-toggle"
                    />
                    <span className="lmchat-mcp-toggle__track" aria-hidden="true" />
                    <span className="lmchat-toggle-hint">
                      {subValue?.value ? "On" : "Off"}
                    </span>
                    <OverrideBadge isOverride={subValue?.is_override ?? false} />
                  </label>
                )
                ) : (
                  <div className="lmchat-field-row" data-testid="settings-memory-subsession-toggle-label">
                    <span className="lmchat-field-row-label">
                      {subValue?.value ? "On" : "Off"}
                    </span>
                    <OverrideBadge isOverride={subValue?.is_override ?? false} />
                  </div>
                )}
            </div>
          </div>
        </section>

      {/* ── Divider ──────────────────────────────────────────────────────── */}
      <hr className="lmchat-section-divider" aria-hidden="true" />

      {/* ── Web search provider ──────────────────────────────────────────── */}
      <section className="lmchat-section" aria-label="Web search">
        <h3 className="lmchat-section-heading">Web search source</h3>
        <p className="lmchat-section-description">
          Choose the default source for web search results.
        </p>
        <div className="lmchat-meta-block">
          <div className="lmchat-field-row">
            <span className="lmchat-field-row-label">Provider</span>
            <select
              className="lmchat-select"
              value={searchProvider?.value ?? ""}
              onChange={handleSearchProviderChange}
              disabled={!isAdmin}
              aria-label="Web search provider"
              data-testid="settings-memory-search-provider"
            >
              <option value="">Default</option>
              <option value="searxng">SearXNG</option>
              <option value="ddg">DuckDuckGo</option>
              <option value="brave">Brave Search</option>
              <option value="brave_llm">Brave (LLM Context)</option>
            </select>
            <OverrideBadge isOverride={searchProvider?.is_override ?? false} />
          </div>
        </div>

        {/* Conditional SearXNG URL input */}
        {searchProvider?.value === "searxng" && (
          <div style={{ marginTop: "var(--space-group)" }}>
            {!editingSearxngUrl ? (
              <div className="lmchat-field-row">
                <span className="lmchat-field-row-label">SearXNG URL</span>
                <button
                  type="button"
                  className="lmchat-btn-secondary"
                  onClick={() => {
                    setSearxngUrlDraft(searxngUrlField?.value ?? "");
                    setEditingSearxngUrl(true);
                  }}
                  disabled={!isAdmin}
                  data-testid="settings-memory-searxng-url-edit"
                >
                  {searxngUrlField?.value ?? "Set URL"}
                </button>
                <OverrideBadge isOverride={searxngUrlField?.is_override ?? false} />
              </div>
            ) : (
              <div style={{ marginTop: "var(--space-glue-relaxed)" }}>
                <div className="lmchat-field">
                  <label
                    htmlFor="memory-searxng-url-input"
                    className="lmchat-field-label"
                  >
                    SearXNG instance URL
                  </label>
                  <input
                    id="memory-searxng-url-input"
                    type="url"
                    className="lmchat-input"
                    value={searxngUrlDraft}
                    onChange={(e) => {
                      setSearxngUrlDraft(e.target.value);
                    }}
                    placeholder="https://searxng.example.com"
                    autoComplete="off"
                    spellCheck={false}
                    data-testid="settings-memory-searxng-url-input"
                  />
                </div>
                <div className="lmchat-form-actions">
                  <button
                    type="button"
                    className="lmchat-btn-primary"
                    onClick={handleSearxngUrlSave}
                    disabled={patchMutation.isPending}
                    data-testid="settings-memory-searxng-url-save"
                  >
                    {patchMutation.isPending ? "Saving…" : "Save"}
                  </button>
                  <button
                    type="button"
                    className="lmchat-btn-secondary"
                    onClick={handleSearxngUrlCancel}
                    disabled={patchMutation.isPending}
                    data-testid="settings-memory-searxng-url-cancel"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </div>
        )}
      </section>

      {/* ── Divider ──────────────────────────────────────────────────────── */}
      <hr className="lmchat-section-divider" aria-hidden="true" />

      {/* ── Repeat-loop cut threshold (K) ────────────────────────────────── */}
      <section className="lmchat-section" aria-label="Repeat-loop cut">
        <h3 className="lmchat-section-heading">Repeat-loop cut (K)</h3>
        <p className="lmchat-section-description">
          Cut a tool-calling loop after K identical calls. Higher = more
          permissive for heavy agentic runs. 0 disables. Default 16.
        </p>
        <div className="lmchat-meta-block">
          {!editingRepeatWarningCutK ? (
            <div className="lmchat-field-row">
              <span className="lmchat-field-row-label">Threshold</span>
              <button
                type="button"
                className="lmchat-btn-secondary"
                onClick={() => {
                  setRepeatWarningCutKDraft(
                    repeatWarningCutKField?.value !== null &&
                      repeatWarningCutKField?.value !== undefined
                      ? String(repeatWarningCutKField.value)
                      : "",
                  );
                  setEditingRepeatWarningCutK(true);
                }}
                disabled={!isAdmin}
                data-testid="settings-memory-repeat-cut-k-edit"
              >
                {repeatWarningCutKField?.value ?? "16"}
              </button>
              <OverrideBadge isOverride={repeatWarningCutKField?.is_override ?? false} />
            </div>
          ) : (
            <div style={{ marginTop: "var(--space-glue-relaxed)" }}>
              <div className="lmchat-field">
                <label
                  htmlFor="memory-repeat-cut-k-input"
                  className="lmchat-field-label"
                >
                  Repeat-loop cut (K)
                </label>
                <input
                  id="memory-repeat-cut-k-input"
                  type="number"
                  min={0}
                  max={100}
                  step={1}
                  className="lmchat-input"
                  value={repeatWarningCutKDraft}
                  onChange={(e) => {
                    setRepeatWarningCutKDraft(e.target.value);
                  }}
                  placeholder="16"
                  data-testid="settings-memory-repeat-cut-k-input"
                />
              </div>
              <div className="lmchat-form-actions">
                <button
                  type="button"
                  className="lmchat-btn-primary"
                  onClick={handleRepeatWarningCutKSave}
                  disabled={patchMutation.isPending}
                  data-testid="settings-memory-repeat-cut-k-save"
                >
                  {patchMutation.isPending ? "Saving…" : "Save"}
                </button>
                <button
                  type="button"
                  className="lmchat-btn-secondary"
                  onClick={handleRepeatWarningCutKCancel}
                  disabled={patchMutation.isPending}
                  data-testid="settings-memory-repeat-cut-k-cancel"
                >
                  Cancel
                </button>
              </div>
            </div>
          )}
        </div>
      </section>

        {/* ── Divider ──────────────────────────────────────────────────────── */}
        <hr className="lmchat-section-divider" aria-hidden="true" />

        {/* ── Pinned insights (read-only) ──────────────────────────────────── */}
        <section className="lmchat-section" aria-label="Pinned insights">
          <h3 className="lmchat-section-heading">Pinned insights</h3>
          <p className="lmchat-section-description">
            Maximum number of pinned insights stored per user.
          </p>
          <div className="lmchat-meta-block">
            <div className="lmchat-meta-row">
              <span className="lmchat-meta-label">Limit</span>
              <span className="lmchat-meta-value">
                Up to 100 per user
                <AdminChip />
              </span>
            </div>
          </div>
        </section>

        {/* ── Divider ──────────────────────────────────────────────────────── */}
        <hr className="lmchat-section-divider" aria-hidden="true" />

        {/* ── Memory indexing status ───────────────────────────────────────── */}
        <section className="lmchat-section" aria-label="Memory indexing status">
          <h3 className="lmchat-section-heading">Indexing status</h3>
          <MemoryIndexingCard />
        </section>
    </div>
  );
}
