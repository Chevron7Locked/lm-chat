/* SPDX-License-Identifier: Apache-2.0 */
/**
 * ProvidersSection — Settings → Providers tab.
 *
 * Lists configured cloud providers (OpenRouter, Groq, OpenAI, custom),
 * lets the admin add, edit, test, and delete provider configurations.
 *
 * Modeled after LmStudioSection.tsx patterns:
 * - api_key is NEVER displayed — only api_key_set boolean badge shown.
 * - Test connection → POST /api/admin/providers/{provider}/test → result banner.
 * - Save → PUT /api/admin/providers/{provider}.
 * - Delete → DELETE /api/admin/providers/{provider} with confirm modal.
 * - lmchat-* CSS classes throughout (settings.css).
 * - useToast from @/stores/toastStore for success/error toasts.
 *
 * Model allowlist picker:
 * - After "Test connection" returns model_ids, a searchable checklist appears.
 * - Empty selection = all models allowed (mirrors BE NULL/[] semantics).
 * - allowed_models included in the PUT body on save.
 * - Provider row shows a small "N models" badge when an allowlist is active.
 */
import { useState, useMemo, type JSX } from "react";
import { Eye, EyeOff } from "lucide-react";
import { useToast } from "@/stores/toastStore";
import {
  useProviders,
  useProviderStatus,
  useUpsertProvider,
  useDeleteProvider,
  useTestProvider,
  type ProviderConfigSafeView,
  type ProbeResponse,
} from "@/hooks/useProviders";
import "@/styles/settings.css";

// ─── Curated provider presets ────────────────────────────────────────────────

interface ProviderPreset {
  slug: string;
  label: string;
  defaultBaseUrl: string;
}

const PROVIDER_PRESETS: ProviderPreset[] = [
  { slug: "openrouter", label: "OpenRouter", defaultBaseUrl: "https://openrouter.ai/api" },
  { slug: "groq", label: "Groq", defaultBaseUrl: "https://api.groq.com/openai" },
  { slug: "openai", label: "OpenAI", defaultBaseUrl: "https://api.openai.com" },
  { slug: "custom", label: "Custom", defaultBaseUrl: "" },
];

function presetLabel(slug: string): string {
  return PROVIDER_PRESETS.find((p) => p.slug === slug)?.label ?? slug;
}

// ─── Model allowlist picker ──────────────────────────────────────────────────

interface ModelAllowlistPickerProps {
  /** Full list of available model ids (from test probe). */
  availableIds: string[];
  /** Currently selected ids. Empty = all allowed. */
  selected: string[];
  onChange: (next: string[]) => void;
}

function ModelAllowlistPicker({
  availableIds,
  selected,
  onChange,
}: ModelAllowlistPickerProps): JSX.Element {
  const [filter, setFilter] = useState<string>("");

  const filtered = useMemo((): string[] => {
    const q = filter.trim().toLowerCase();
    if (q === "") return availableIds;
    return availableIds.filter((id) => id.toLowerCase().includes(q));
  }, [availableIds, filter]);

  const selectedSet = useMemo(() => new Set(selected), [selected]);

  function toggleOne(id: string): void {
    const next = new Set(selectedSet);
    if (next.has(id)) {
      next.delete(id);
    } else {
      next.add(id);
    }
    onChange(Array.from(next));
  }

  /** Select all items currently visible in the filtered list. */
  function selectFiltered(): void {
    const next = new Set(selectedSet);
    for (const id of filtered) next.add(id);
    onChange(Array.from(next));
  }

  /** Deselect all items currently visible in the filtered list. */
  function deselectFiltered(): void {
    const filterSet = new Set(filtered);
    onChange(selected.filter((id) => !filterSet.has(id)));
  }

  const total = availableIds.length;
  const count = selected.length;
  const isAllAllowed = count === 0;

  const countLabel = isAllAllowed
    ? `All ${String(total)} models allowed (none selected)`
    : `Allowing ${String(count)} of ${String(total)} model${total !== 1 ? "s" : ""}`;

  return (
    <div className="lmchat-allowlist-picker" data-testid="allowlist-picker">
      {/* Count + semantics explanation */}
      <p className="lmchat-allowlist-picker__count" data-testid="allowlist-count">
        {countLabel}
      </p>
      {isAllAllowed && (
        <p className="lmchat-allowlist-picker__hint" data-testid="allowlist-all-hint">
          Leave empty to allow all models from this provider.
        </p>
      )}

      {/* Filter input */}
      <input
        type="search"
        className="lmchat-input lmchat-allowlist-picker__filter"
        placeholder="Filter models…"
        value={filter}
        onChange={(e) => { setFilter(e.target.value); }}
        aria-label="Filter model list"
        data-testid="allowlist-filter"
      />

      {/* Select all / Select none (respects filter) */}
      <div className="lmchat-allowlist-picker__bulk-actions">
        <button
          type="button"
          className="lmchat-link-btn lmchat-text-link"
          onClick={selectFiltered}
          data-testid="allowlist-select-all"
        >
          Select{filter.trim() !== "" ? " filtered" : " all"}
        </button>
        <span className="lmchat-allowlist-picker__sep" aria-hidden>·</span>
        <button
          type="button"
          className="lmchat-link-btn lmchat-text-link"
          onClick={deselectFiltered}
          data-testid="allowlist-select-none"
        >
          {filter.trim() !== "" ? "Deselect filtered" : "Select none"}
        </button>
      </div>

      {/* Scrollable checklist */}
      <ul
        className="lmchat-allowlist-picker__list"
        role="list"
        aria-label="Available models"
        data-testid="allowlist-list"
      >
        {filtered.length === 0 ? (
          <li className="lmchat-allowlist-picker__empty">No models match your filter.</li>
        ) : (
          filtered.map((id) => (
            <li key={id} className="lmchat-allowlist-picker__item">
              <label className="lmchat-allowlist-picker__label">
                <input
                  type="checkbox"
                  className="lmchat-allowlist-picker__checkbox"
                  checked={selectedSet.has(id)}
                  onChange={() => { toggleOne(id); }}
                  data-testid={`allowlist-checkbox-${id}`}
                />
                <span className="lmchat-allowlist-picker__model-id">{id}</span>
              </label>
            </li>
          ))
        )}
      </ul>
    </div>
  );
}

// ─── Provider list row ───────────────────────────────────────────────────────

interface ProviderRowProps {
  config: ProviderConfigSafeView;
  status: { reachable: boolean; error: string | null } | undefined;
  onEdit: () => void;
}

function ProviderRow({ config, status, onEdit }: ProviderRowProps): JSX.Element {
  const truncateUrl = (url: string): string =>
    url.length > 42 ? `${url.slice(0, 39)}…` : url;

  const allowlistCount =
    Array.isArray(config.allowed_models) && config.allowed_models.length > 0
      ? config.allowed_models.length
      : null;

  return (
    <button
      type="button"
      className="lmchat-provider-row"
      data-testid={`provider-row-${config.provider}`}
      onClick={onEdit}
    >
      <span className="lmchat-provider-row__slug">{presetLabel(config.provider)}</span>
      <span className="lmchat-provider-row__url" title={config.base_url}>
        {truncateUrl(config.base_url)}
      </span>
      <span className="lmchat-provider-row__badges">
        {config.api_key_set ? (
          <span className="lmchat-provider-badge lmchat-provider-badge--key">Key set</span>
        ) : (
          <span className="lmchat-provider-badge lmchat-provider-badge--nokey">No key</span>
        )}
        {allowlistCount !== null && (
          <span
            className="lmchat-provider-badge lmchat-provider-badge--allowlist"
            data-testid={`provider-allowlist-badge-${config.provider}`}
            title={`Allowlist: ${String(allowlistCount)} model${allowlistCount !== 1 ? "s" : ""}`}
          >
            {String(allowlistCount)} model{allowlistCount !== 1 ? "s" : ""}
          </span>
        )}
        {status !== undefined ? (
          status.reachable ? (
            <span className="lmchat-provider-badge lmchat-provider-badge--ok">● reachable</span>
          ) : (
            <span
              className="lmchat-provider-badge lmchat-provider-badge--err"
              title={status.error ?? undefined}
            >
              {status.error !== null ? `○ unreachable: ${status.error}` : "○ unreachable"}
            </span>
          )
        ) : null}
      </span>
    </button>
  );
}

// ─── Add/edit form ───────────────────────────────────────────────────────────

interface ProviderFormProps {
  /** null = add mode, defined = edit mode */
  editing: ProviderConfigSafeView | null;
  onClose: () => void;
  onSaved: () => void;
}

function ProviderForm({ editing, onClose, onSaved }: ProviderFormProps): JSX.Element {
  const { push } = useToast();
  const isEditMode = editing !== null;

  // Determine initial preset slug
  const initialPresetSlug = (): string => {
    if (!isEditMode) return "openrouter";
    const known = PROVIDER_PRESETS.find((p) => p.slug === editing.provider);
    return known ? editing.provider : "custom";
  };

  const [presetSlug, setPresetSlug] = useState<string>(initialPresetSlug());
  const [providerSlug, setProviderSlug] = useState<string>(
    isEditMode ? editing.provider : "openrouter",
  );
  const [baseUrl, setBaseUrl] = useState<string>(
    isEditMode ? editing.base_url : "https://openrouter.ai/api",
  );
  const [apiKey, setApiKey] = useState<string>("");
  const [showApiKey, setShowApiKey] = useState<boolean>(false);
  const [defaultModel, setDefaultModel] = useState<string>(
    isEditMode ? (editing.default_model ?? "") : "",
  );
  const [enabled, setEnabled] = useState<boolean>(
    isEditMode ? editing.enabled : true,
  );

  // ── Allowlist state ──────────────────────────────────────────────────────
  // availableIds: populated after a successful test probe (model_ids).
  // allowedModels: the set the admin has selected ([] = all allowed).
  const [availableIds, setAvailableIds] = useState<string[] | null>(null);
  const [allowedModels, setAllowedModels] = useState<string[]>(
    // Pre-populate from existing config when editing
    isEditMode && Array.isArray(editing.allowed_models) && editing.allowed_models.length > 0
      ? editing.allowed_models
      : [],
  );

  const [saveError, setSaveError] = useState<string | null>(null);
  const [testResult, setTestResult] = useState<ProbeResponse | null>(null);

  const upsertMutation = useUpsertProvider();
  const deleteMutation = useDeleteProvider();
  const testMutation = useTestProvider();

  function handlePresetChange(slug: string): void {
    setPresetSlug(slug);
    if (slug === "custom") {
      setProviderSlug("");
      setBaseUrl("");
    } else {
      const preset = PROVIDER_PRESETS.find((p) => p.slug === slug);
      if (preset) {
        setProviderSlug(preset.slug);
        setBaseUrl(preset.defaultBaseUrl);
      }
    }
    setTestResult(null);
    setAvailableIds(null);
    setSaveError(null);
  }

  function handleTest(): void {
    setTestResult(null);
    const slug = isEditMode ? editing.provider : providerSlug;
    if (!slug || !baseUrl) return;

    const vars: { provider: string; base_url?: string; api_key?: string } = {
      provider: slug,
      base_url: baseUrl,
    };
    if (apiKey !== "") vars.api_key = apiKey;

    testMutation.mutate(vars, {
      onSuccess: (result) => {
        setTestResult(result);
        // Populate the allowlist picker when model_ids are returned
        if (result.ok && Array.isArray(result.model_ids) && result.model_ids.length > 0) {
          setAvailableIds(result.model_ids);
        }
      },
      onError: (err) => {
        setTestResult({ ok: false, error: err.detail ?? "Test failed" });
      },
    });
  }

  function handleSave(): void {
    setSaveError(null);
    setTestResult(null);

    const slug = isEditMode ? editing.provider : providerSlug;
    if (!slug) {
      setSaveError("Provider slug is required.");
      return;
    }
    if (!baseUrl) {
      setSaveError("Base URL is required.");
      return;
    }

    const body: {
      base_url: string;
      api_key?: string;
      default_model?: string;
      enabled: boolean;
      allowed_models?: string[] | null;
    } = { base_url: baseUrl, enabled };

    if (apiKey !== "") body.api_key = apiKey;
    if (defaultModel !== "") body.default_model = defaultModel;
    // Always include allowed_models so the BE can distinguish "not changed" vs "cleared"
    // [] means "all allowed" on the BE; non-empty means restricted to those ids.
    body.allowed_models = allowedModels.length > 0 ? allowedModels : [];

    upsertMutation.mutate(
      { provider: slug, body },
      {
        onSuccess: () => {
          push({ variant: "success", message: `Provider "${presetLabel(slug)}" saved.` });
          onSaved();
        },
        onError: (err) => {
          setSaveError(err.detail ?? "Save failed.");
        },
      },
    );
  }

  function handleDelete(): void {
    if (!isEditMode) return;
    const slug = editing.provider;
    if (!window.confirm(`Delete provider "${presetLabel(slug)}"? This cannot be undone.`)) return;

    deleteMutation.mutate(slug, {
      onSuccess: () => {
        push({ variant: "success", message: `Provider "${presetLabel(slug)}" deleted.` });
        onSaved();
      },
      onError: (err) => {
        setSaveError(err.detail ?? "Delete failed.");
      },
    });
  }

  const isSaving = upsertMutation.isPending;
  const isTesting = testMutation.isPending;
  const isDeleting = deleteMutation.isPending;

  // Show "edit mode but no model_ids yet" affordance when editing an existing
  // provider that already has an allowlist but hasn't been re-tested this session.
  const hasExistingAllowlist =
    isEditMode &&
    Array.isArray(editing.allowed_models) &&
    editing.allowed_models.length > 0;
  const showRetestAffordance = hasExistingAllowlist && availableIds === null;

  return (
    <div className="lmchat-form" style={{ maxWidth: 480 }} data-testid="providers-form">
      {/* Provider type selector */}
      {!isEditMode && (
        <div className="lmchat-field">
          <label htmlFor="provider-select" className="lmchat-field-label">
            Provider
          </label>
          <select
            id="provider-select"
            data-testid="provider-select"
            className="lmchat-input"
            value={presetSlug}
            onChange={(e) => {
              handlePresetChange(e.target.value);
            }}
          >
            {PROVIDER_PRESETS.map((p) => (
              <option key={p.slug} value={p.slug}>
                {p.label}
              </option>
            ))}
          </select>
        </div>
      )}

      {/* Custom slug input for custom provider (add mode only) */}
      {!isEditMode && presetSlug === "custom" && (
        <div className="lmchat-field">
          <label htmlFor="provider-slug" className="lmchat-field-label">
            Provider slug
          </label>
          <input
            id="provider-slug"
            type="text"
            value={providerSlug}
            onChange={(e) => {
              setProviderSlug(e.target.value);
            }}
            placeholder="e.g. my-provider"
            autoComplete="off"
            spellCheck={false}
            className="lmchat-input"
          />
        </div>
      )}

      {/* Base URL */}
      <div className="lmchat-field">
        <label htmlFor="provider-base-url" className="lmchat-field-label">
          Base URL
        </label>
        <input
          id="provider-base-url"
          type="url"
          value={baseUrl}
          onChange={(e) => {
            setBaseUrl(e.target.value);
          }}
          placeholder="https://api.example.com"
          autoComplete="off"
          spellCheck={false}
          className="lmchat-input"
          data-testid="provider-base-url"
        />
      </div>

      {/* API key */}
      <div className="lmchat-field">
        <label htmlFor="provider-api-key" className="lmchat-field-label">
          API key
        </label>
        <div className="lmchat-api-key-row">
          <input
            id="provider-api-key"
            type={showApiKey ? "text" : "password"}
            value={apiKey}
            onChange={(e) => {
              setApiKey(e.target.value);
            }}
            placeholder={
              isEditMode && editing.api_key_set
                ? "•••••• (saved — type to replace)"
                : "(none set)"
            }
            autoComplete="off"
            spellCheck={false}
            className="lmchat-input"
            style={{ flex: 1 }}
            data-testid="provider-api-key"
          />
          <button
            type="button"
            onClick={() => {
              setShowApiKey((s) => !s);
            }}
            className="lmchat-btn-eye"
            data-testid="provider-api-key-show"
            aria-pressed={showApiKey}
            aria-label={showApiKey ? "Hide API key" : "Show API key"}
            title={showApiKey ? "Hide API key" : "Show API key"}
          >
            {showApiKey ? (
              <EyeOff size={16} aria-hidden />
            ) : (
              <Eye size={16} aria-hidden />
            )}
          </button>
        </div>
        {isEditMode && editing.api_key_set && (
          <p
            className="lmchat-field-hint"
            data-testid="provider-api-key-hint"
          >
            Leave blank to keep the current key.
          </p>
        )}
      </div>

      {/* Default model */}
      <div className="lmchat-field">
        <label htmlFor="provider-default-model" className="lmchat-field-label">
          Default model (optional)
        </label>
        <input
          id="provider-default-model"
          type="text"
          value={defaultModel}
          onChange={(e) => {
            setDefaultModel(e.target.value);
          }}
          placeholder="e.g. meta-llama/llama-3.3-70b"
          autoComplete="off"
          spellCheck={false}
          className="lmchat-input"
          data-testid="provider-default-model"
        />
      </div>

      {/* Enabled toggle */}
      <div className="lmchat-field" style={{ flexDirection: "row", alignItems: "center", gap: 8 }}>
        <input
          id="provider-enabled"
          type="checkbox"
          checked={enabled}
          onChange={(e) => {
            setEnabled(e.target.checked);
          }}
          data-testid="provider-enabled"
        />
        <label htmlFor="provider-enabled" className="lmchat-field-label" style={{ margin: 0 }}>
          Enabled
        </label>
      </div>

      {/* Save error */}
      {saveError !== null && (
        <p
          role="alert"
          className="lmchat-form-error"
          data-testid="providers-save-error"
        >
          {saveError}
        </p>
      )}

      {/* Actions */}
      <div className="lmchat-form-actions" style={{ flexWrap: "wrap" }}>
        <button
          type="button"
          onClick={handleSave}
          disabled={isSaving || isDeleting}
          className="lmchat-btn-primary"
          data-testid="providers-save"
        >
          {isSaving ? "Saving…" : "Save"}
        </button>
        <button
          type="button"
          onClick={handleTest}
          disabled={isTesting || !baseUrl}
          className="lmchat-btn-secondary"
          data-testid="providers-test"
        >
          {isTesting ? "Testing…" : "Test connection"}
        </button>
        {isEditMode && (
          <button
            type="button"
            onClick={handleDelete}
            disabled={isDeleting || isSaving}
            className="lmchat-btn-secondary"
            style={{ color: "var(--color-danger, #e53e3e)" }}
            data-testid="providers-delete"
          >
            {isDeleting ? "Deleting…" : "Delete"}
          </button>
        )}
        <button
          type="button"
          onClick={onClose}
          className="lmchat-btn-secondary"
        >
          Cancel
        </button>

        {/* Test result banner */}
        {testResult !== null && (
          <div
            role={testResult.ok ? "status" : "alert"}
            data-testid="providers-test-result"
            className={`lmchat-probe-banner lmchat-probe-banner--${testResult.ok ? "ok" : "err"}`}
          >
            <span className="lmchat-probe-banner-dot" aria-hidden />
            <span className="lmchat-probe-banner-text">
              {testResult.ok
                ? `Connected — ${String(testResult.model_count ?? 0)} models reachable`
                : (testResult.error ?? "Test failed")}
            </span>
          </div>
        )}
      </div>

      {/* ── Model allowlist picker ───────────────────────────────────────────── */}
      {/* Shown after a successful test probe returns model_ids */}
      {availableIds !== null && (
        <div className="lmchat-field" style={{ marginTop: "var(--space-group)" }}>
          <span className="lmchat-field-label">Models to allow</span>
          <ModelAllowlistPicker
            availableIds={availableIds}
            selected={allowedModels}
            onChange={setAllowedModels}
          />
        </div>
      )}

      {/* Editing an existing allowlist but haven't re-tested yet */}
      {showRetestAffordance && Array.isArray(editing.allowed_models) && (
        <p
          className="lmchat-field-hint"
          data-testid="allowlist-retest-hint"
          style={{ marginTop: "var(--space-group)" }}
        >
          This provider has an allowlist ({String(editing.allowed_models.length)} model
          {editing.allowed_models.length !== 1 ? "s" : ""}).
          Test connection to edit the list, or save to keep the current selection.
        </p>
      )}
    </div>
  );
}

// ─── Main component ──────────────────────────────────────────────────────────

export function ProvidersSection(): JSX.Element {
  const { data: providers, isLoading, isError } = useProviders();
  const { data: statusList } = useProviderStatus();

  // null = closed, "add" = add form, ProviderConfigSafeView = edit form
  const [formState, setFormState] = useState<"add" | ProviderConfigSafeView | null>(null);

  function statusFor(slug: string): { reachable: boolean; error: string | null } | undefined {
    return statusList?.find((s) => s.provider === slug);
  }

  function handleSaved(): void {
    setFormState(null);
  }

  return (
    <div className="lmchat-section-container" data-testid="settings-providers-section">
      {formState === null ? (
        <>
          {/* Provider list */}
          {isLoading && (
            <p className="lmchat-section-description">Loading providers…</p>
          )}
          {isError && (
            <p className="lmchat-form-error" role="alert">
              Couldn't load providers — try again.
            </p>
          )}
          {!isLoading && !isError && (
            <div data-testid="providers-list">
              {providers !== undefined && providers.length > 0 ? (
                providers.map((cfg) => (
                  <ProviderRow
                    key={cfg.provider}
                    config={cfg}
                    status={statusFor(cfg.provider)}
                    onEdit={() => {
                      setFormState(cfg);
                    }}
                  />
                ))
              ) : (
                <p className="lmchat-section-description">
                  No cloud providers configured. Add one to route chats to OpenRouter, Groq, or OpenAI.
                </p>
              )}
            </div>
          )}

          <div className="lmchat-form-actions" style={{ marginTop: 16 }}>
            <button
              type="button"
              onClick={() => {
                setFormState("add");
              }}
              className="lmchat-btn-primary"
              data-testid="providers-add-btn"
            >
              Add provider
            </button>
          </div>
        </>
      ) : formState === "add" ? (
        <ProviderForm
          editing={null}
          onClose={() => {
            setFormState(null);
          }}
          onSaved={handleSaved}
        />
      ) : (
        <ProviderForm
          editing={formState}
          onClose={() => {
            setFormState(null);
          }}
          onSaved={handleSaved}
        />
      )}
    </div>
  );
}
