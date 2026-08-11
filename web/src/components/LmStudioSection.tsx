/* SPDX-License-Identifier: Apache-2.0 */
/**
 * LmStudioSection — Settings → LM Studio tab.
 *
 * Surfaces the active LM Studio connection parameters (base URL, API
 * key, default model) and lets the user record per-user overrides.
 * The fallback chain is server-side (user → server admin → env);
 * this component reads ``GET /api/settings/lmstudio`` for the
 * resolved view and writes through ``PUT /api/settings/lmstudio`` to
 * patch the per-user row.  "Test connection" issues a one-shot
 * ``POST /api/settings/lmstudio/test`` probe.
 *
 * Wire contract: see ``src/lmchat/routes/lm_studio_settings.py``.
 * The API key cleartext is NEVER returned by GET;
 * we surface ``api_key_set: bool`` and render the input field with a
 * "use saved value" placeholder when true.  Submitting the form
 * without re-typing the key preserves the stored one (we send
 * ``api_key`` only when the user explicitly types it).
 *
 * Source banner: a small chip near the top reports which tier each
 * field came from (``user`` / ``server_admin`` / ``env``).
 *
 * Inline CSSProperties replaced with settings.css semantic classes.
 * Spacing grammar applied:
 *   - Banner → form: GROUP (24px) via .lmchat-form margin-top
 *   - Field → field: GROUP (24px) via .lmchat-form gap
 *   - Label → input: GLUE (4px) via .lmchat-field gap
 *   - Hint below input: GLUE (4px) via .lmchat-model-hint-row margin-top
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type SubmitEvent,
} from "react";
import { useBlocker } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { lmStudioConfigKeys, useLmStudioConfig } from "@/hooks/useLmStudioConfig";
import { Eye, EyeOff } from "lucide-react";
import { useToast } from "@/stores/toastStore";
import { formatRelativeTime } from "@/lib/relativeTime";
import { useModelList } from "@/hooks/useModelList";
import { useRefreshModels } from "@/hooks/useModels";
import { useChatModelOptions } from "@/hooks/useChatModelOptions";
import { useLmStudioStore } from "@/stores/lmStudioStore";
import { useAuthStore } from "@/stores/authStore";
import { useEmbeddingStatus } from "@/hooks/useEmbeddingStatus";
import { ModelSelectControl } from "@/components/ModelSelectControl";
import { dedupeByKey } from "@/lib/dedupeByKey";
import "@/styles/settings.css";

// ─── Wire shapes (mirror src/lmchat/routes/lm_studio_settings.py) ───────────

type Source = "user" | "server_admin" | "env" | "unset";

interface ResolvedConfig {
  base_url: string;
  default_model: string;
  api_key_set: boolean;
  source_base_url: Source;
  source_api_key: Source;
  source_default_model: Source;
  /** "native" (default) or "openai_compat" — optional until the BE lands it. */
  lm_studio_endpoint_mode?: "native" | "openai_compat";
}

interface ProbeResponse {
  ok: boolean;
  model_count?: number | null;
  error?: string | null;
}

// ─── Pure helpers ───────────────────────────────────────────────────────────

/**
 * Build the connection-fields patch body for a Save submission.
 *
 * The backend treats an OMITTED field as "leave unchanged", and the
 * admin PATCH endpoint runs a live probe of LM Studio before writing
 * base_url or api_key.  Sending these fields when the user did NOT
 * change them triggers a probe that fails with 401 when LM Studio
 * requires an API key — blocking even a model-only change.
 *
 * Rules:
 *   - Include base_url only when it differs from the loaded value.
 *   - Include api_key only when the user typed a new value (the input
 *     starts empty and is never prefilled from the server).
 *   - An empty string for either field is treated as "user cleared it"
 *     and forwarded as-is (the backend will validate/reject).
 */
export function buildConnBody(
  baseUrl: string,
  apiKey: string,
  loadedBaseUrl: string,
): { base_url?: string; api_key?: string } {
  const body: { base_url?: string; api_key?: string } = {};
  // Only include base_url when the user actually changed it.
  if (baseUrl !== loadedBaseUrl) {
    body.base_url = baseUrl;
  }
  // api_key starts empty; a non-empty value always means "user typed a new key".
  if (apiKey !== "") {
    body.api_key = apiKey;
  }
  return body;
}

// ─── Component ──────────────────────────────────────────────────────────────

export function LmStudioSection() {
  const { push } = useToast();
  const qc = useQueryClient();

  const [loading, setLoading] = useState<boolean>(true);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [resolved, setResolved] = useState<ResolvedConfig | null>(null);

  // Form draft state — separate from resolved so the user can edit
  // without rewriting the source-of-truth until they save.
  const [baseUrl, setBaseUrl] = useState<string>("");
  const [apiKey, setApiKey] = useState<string>("");
  const [showApiKey, setShowApiKey] = useState<boolean>(false);
  const [defaultModel, setDefaultModel] = useState<string>("");

  const [saving, setSaving] = useState<boolean>(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const [testing, setTesting] = useState<boolean>(false);
  const [testResult, setTestResult] = useState<ProbeResponse | null>(null);

  // Dirty tracking — true when any form field diverges from resolved
  // (or when the user typed a new API key). Drives the navigation
  // blocker + the browser beforeunload warning.
  const isDirty =
    resolved !== null &&
    (baseUrl !== resolved.base_url ||
      defaultModel !== resolved.default_model ||
      apiKey !== "");

  // In-app router navigation guard — the app now uses a data router
  // (createBrowserRouter) so useBlocker is available. Confirms before
  // navigating away with unsaved changes.
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      isDirty && currentLocation.pathname !== nextLocation.pathname,
  );
  useEffect(() => {
    if (blocker.state === "blocked") {
      const proceed = window.confirm(
        "You have unsaved LM Studio settings. Leave without saving?",
      );
      if (proceed) blocker.proceed();
      else blocker.reset();
    }
  }, [blocker]);
  useEffect(() => {
    if (!isDirty) return;
    const handler = (e: BeforeUnloadEvent): void => {
      // preventDefault() alone triggers the browser's native "leave site?"
      // confirmation in modern browsers; `returnValue` is deprecated.
      e.preventDefault();
    };
    window.addEventListener("beforeunload", handler);
    return () => {
      window.removeEventListener("beforeunload", handler);
    };
  }, [isDirty]);

  // Model dropdown sourced from /api/models.
  // The user can either pick from the dropdown or fall back to typing
  // a custom value (e.g. an admin pre-seeding a model that isn't loaded yet).
  const { revalidate: refreshModelList } = useModelList();
  // GET /api/models reads a BE-side cache that doesn't re-probe LM Studio,
  // so invalidating the FE query alone returned the same stale list. The
  // user-visible "Refresh list" button calls THIS mutation, which POSTs
  // /api/admin/models/refresh to force a re-probe before the FE refetch.
  const refreshModelsMutation = useRefreshModels();
  const resolveProbe = useLmStudioStore((s) => s.resolveProbe);
  const isAdmin = useAuthStore((s) => s.user?.is_admin ?? false);
  // Manual-entry escape hatch for pre-seeding an unloaded model id.
  const [manualEntry, setManualEntry] = useState<boolean>(false);
  // Canonical chat-model dropdown options (embedding excluded,
  // (unloaded) suffix consistent with other surfaces).
  // `groups` gives provider-grouped optgroups (LM Studio → OpenRouter order).
  const { options: chatModelOptionsRaw, groups: chatModelGroupsRaw } = useChatModelOptions();
  const modelOptions = useMemo(
    () =>
      chatModelOptionsRaw.map((o) => ({
        id: o.id,
        label: o.loaded ? o.label.replace(/ \(unloaded\)$/, "") : o.label,
        loaded: o.loaded,
        // Carry the capability flags through so ModelSelectControl can
        // render the Eye/Wrench/Brain icons on this surface too —
        // previously this remap dropped the field and the icons rendered
        // empty here.
        capabilities: o.capabilities,
        provider: o.provider,
      })),
    [chatModelOptionsRaw],
  );
  // Provider-grouped variant for the optgroup rendering path.
  // Settings uses plain model ids (no composite provider::id prefix),
  // so we map directly without the Chat.tsx composite-id transformation.
  const modelGroups = useMemo(
    () =>
      chatModelGroupsRaw.map((g) => ({
        ...g,
        options: g.options.map((o) => ({
          id: o.id,
          label: o.loaded ? o.label.replace(/ \(unloaded\)$/, "") : o.label,
          loaded: o.loaded,
          capabilities: o.capabilities,
          provider: o.provider,
        })),
      })),
    [chatModelGroupsRaw],
  );

  const refresh = useCallback(async (): Promise<void> => {
    setLoading(true);
    setLoadError(null);
    try {
      const cfg = await api.request<ResolvedConfig>("/api/settings/lmstudio");
      setResolved(cfg);
      // When both user + admin are unset, pre-fill the form fields from
      // the env_suggestion (admin-only,
      // reference-only) so the admin has a starting point to Save.
      // Non-admins will get 403 from env_suggestion and just see empty.
      if (cfg.source_base_url === "unset") {
        try {
          const sugg = await api.request<{
            base_url: string;
            api_key_set: boolean;
            default_model: string;
          }>("/api/settings/lmstudio/env_suggestion");
          setBaseUrl(sugg.base_url);
          setDefaultModel(sugg.default_model);
        } catch {
          setBaseUrl("");
          setDefaultModel("");
        }
      } else {
        setBaseUrl(cfg.base_url);
        setDefaultModel(cfg.default_model);
      }
      // We deliberately do NOT prefill apiKey from the server (the
      // server never returns the cleartext).  Leave the input empty;
      // the placeholder reflects "use saved value".
    } catch (err) {
      const apiErr = err as ApiError;
      setLoadError(
        apiErr.detail ?? "Couldn't load LM Studio config — try again.",
      );
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // ── Save handler ────────────────────────────────────────────────────────
  async function handleSave(event: SubmitEvent<HTMLFormElement>): Promise<void> {
    event.preventDefault();
    // Guard: form is only rendered when resolved is non-null (see the early
    // return below), but TypeScript can't infer that from component state.
    if (resolved === null) return;
    setSaveError(null);
    setSaving(true);

    // Build patch bodies.
    // - base_url + api_key: connection fields — admins route these through
    //   /api/admin/lmstudio/default (probe gate + singleton rewire).
    //   Non-admins use /api/settings/lmstudio (user-tier only).
    // - default_model: ALWAYS written through the user-tier PUT so that
    //   svc.resolve(user.id) returns the saved value. Routing it through
    //   the admin PATCH writes to the server_lm_studio_default table, but
    //   a pre-existing user_lm_studio_overrides row for default_model
    //   shadows the admin tier → resolve() hands back the old user value,
    //   the dropdown reverts to whatever the user previously had (the
    //   "always reverts to 9b" bug).
    // Only include connection fields that the user actually changed.
    // Sending an unchanged base_url triggers a backend probe that returns
    // 401 when LM Studio requires an API key, blocking model-only saves.
    const connBody = buildConnBody(baseUrl, apiKey, resolved.base_url);

    try {
      // Step 1: persist connection fields (admin → admin endpoint with probe
      // gate; non-admin → user endpoint). Skip when neither field changed.
      const hasConnChanges =
        connBody.base_url !== undefined || connBody.api_key !== undefined;
      if (hasConnChanges) {
        const connEndpoint = isAdmin
          ? "/api/admin/lmstudio/default"
          : "/api/settings/lmstudio";
        const connMethod = isAdmin ? "PATCH" : "PUT";
        await api.request<ResolvedConfig>(connEndpoint, {
          method: connMethod,
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(connBody),
        });
      }

      // Step 2: persist default_model through the user-tier PUT so it lands
      // in user_lm_studio_overrides and resolve(user.id) returns it immediately.
      // Always send when non-empty (an empty string means "nothing typed", not
      // "clear the field").
      let next: ResolvedConfig;
      if (defaultModel !== "") {
        next = await api.request<ResolvedConfig>("/api/settings/lmstudio", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ default_model: defaultModel }),
        });
      } else {
        // No model selected — just refresh the resolved view.
        next = await api.request<ResolvedConfig>("/api/settings/lmstudio");
      }

      setResolved(next);
      setBaseUrl(next.base_url);
      setDefaultModel(next.default_model);
      setApiKey("");
      setShowApiKey(false);
      // Trigger the model list refetch so the Default Model field switches
      // from manual-text to dropdown immediately after save.
      await refreshModelList();
      // Invalidate the resolved-config TanStack query so every reader
      // (notably useLmStudioConfig → savedDefaultModel in Chat) gets the
      // freshly-saved default_model on the next SPA navigation instead of
      // serving the 60s stale cache.
      await qc.invalidateQueries({ queryKey: lmStudioConfigKeys.resolved() });
      push({ variant: "success", message: "LM Studio settings saved." });
    } catch (err) {
      const apiErr = err as ApiError;
      setSaveError(apiErr.detail ?? "Save failed.");
    } finally {
      setSaving(false);
    }
  }

  // ── Test connection handler ──────────────────────────────────────────────
  async function handleTest(): Promise<void> {
    setTesting(true);
    setTestResult(null);
    try {
      const result = await api.request<ProbeResponse>(
        "/api/settings/lmstudio/test",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            base_url: baseUrl,
            // Probe with the freshly-typed key if present; otherwise
            // the server falls back to the stored chain (passing null).
            api_key: apiKey !== "" ? apiKey : null,
          }),
        },
      );
      setTestResult(result);
      // Push the probe outcome through the store so the model dropdown
      // and the rest of the app reflect the new state immediately.  On
      // success, refetch /api/models so the dropdown rebuilds without
      // waiting for the next cache tick.
      if (result.ok) {
        resolveProbe({
          ok: true,
          modelCount: result.model_count ?? 0,
          // We don't know the loaded count from this probe shape; assume
          // ≥1 if model_count > 0 so the store reports "connected".  The
          // background TanStack query will refine the loaded count once
          // the refresh below completes.
          loadedCount:
            (result.model_count ?? 0) > 0 ? (result.model_count ?? 0) : 0,
        });
        void refreshModelList();
      } else {
        // Translate 401 error from upstream into a user-friendly message.
        const userError =
          result.error?.includes("401")
            ? "LM Studio requires an API key — set it below"
            : result.error ?? "Probe failed";
        setTestResult({ ok: false, model_count: null, error: userError });
        resolveProbe({ ok: false, error: userError });
      }
    } catch (err) {
      const apiErr = err as ApiError;
      const message = apiErr.detail ?? "Probe failed.";
      setTestResult({
        ok: false,
        model_count: null,
        error: message,
      });
      resolveProbe({ ok: false, error: message });
    } finally {
      setTesting(false);
    }
  }

  // ── Render ───────────────────────────────────────────────────────────────
  if (loading) {
    return (
      <div
        className="lmchat-section-container"
        data-testid="settings-lmstudio-section"
      >
        <p className="lmchat-section-description">Loading…</p>
      </div>
    );
  }

  if (loadError !== null) {
    return (
      <div
        className="lmchat-section-container"
        data-testid="settings-lmstudio-section"
      >
        <p
          className="lmchat-form-error"
          role="alert"
          data-testid="lmstudio-load-error"
        >
          {loadError}
        </p>
      </div>
    );
  }

  if (resolved === null) {
    return null;
  }

  return (
    <div
      className="lmchat-section-container"
      data-testid="settings-lmstudio-section"
    >
      {/* ── Edit form ──────────────────────────────────────────────────────── */}
      <form
        onSubmit={(e) => {
          void handleSave(e);
        }}
        className="lmchat-form"
        style={{ maxWidth: 480 }}
        data-testid="lmstudio-form"
        noValidate
      >
        {/* Base URL */}
        <div className="lmchat-field">
          <label htmlFor="lmstudio-base-url" className="lmchat-field-label">
            Base URL
          </label>
          <input
            id="lmstudio-base-url"
            type="url"
            value={baseUrl}
            onChange={(e) => {
              setBaseUrl(e.target.value);
            }}
            placeholder="http://host.docker.internal:1234"
            autoComplete="off"
            spellCheck={false}
            className="lmchat-input"
            data-testid="lmstudio-base-url"
          />
          <span className="lmchat-field-hint">
            In Docker, use <code>http://host.docker.internal:1234</code>; on
            bare-metal (same machine as LM Studio),{" "}
            <code>http://localhost:1234</code>.
          </span>
        </div>

        {/* API key */}
        <div className="lmchat-field">
          <label htmlFor="lmstudio-api-key" className="lmchat-field-label">
            API key
          </label>
          <div className="lmchat-api-key-row">
            <input
              id="lmstudio-api-key"
              type={showApiKey ? "text" : "password"}
              value={apiKey}
              onChange={(e) => {
                setApiKey(e.target.value);
              }}
              placeholder={
                resolved.api_key_set
                  ? "•••••• (saved — type to replace)"
                  : "(none set)"
              }
              autoComplete="off"
              spellCheck={false}
              className="lmchat-input"
              style={{ flex: 1 }}
              data-testid="lmstudio-api-key"
            />
            <button
              type="button"
              onClick={() => {
                setShowApiKey((s) => !s);
              }}
              className="lmchat-btn-eye"
              data-testid="lmstudio-api-key-show"
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
        </div>

        {/* Default model */}
        <div className="lmchat-field">
          <label
            htmlFor="lmstudio-default-model"
            className="lmchat-field-label"
          >
            Default model
          </label>
          {/* ONE control regardless of sync state. The previous
              hybrid (text + datalist when no models, <select> when synced)
              was jarring — the "weird text/select hybrid…then once
              I sync it disappears" complaint. Now:
                 - Always render ModelSelectControl (full form-width).
                 - When no models reported: a disabled placeholder option
                   tells the user to Test connection / load a model.
                 - "Type manually" toggle is the explicit escape for the
                   rare pre-seed case. */}
          {manualEntry ? (
            <>
              <input
                id="lmstudio-default-model"
                type="text"
                value={defaultModel}
                onChange={(e) => {
                  setDefaultModel(e.target.value);
                }}
                placeholder="model id (e.g. qwen3-8b)"
                autoComplete="off"
                spellCheck={false}
                className="lmchat-input"
                data-testid="lmstudio-default-model"
              />
              <div className="lmchat-model-hint-row">
                <button
                  type="button"
                  onClick={() => {
                    setManualEntry(false);
                    if (!modelOptions.some((o) => o.id === defaultModel)) {
                      setDefaultModel("");
                    }
                  }}
                  className="lmchat-link-btn lmchat-text-link"
                  data-testid="lmstudio-default-model-pick-from-list"
                >
                  ← Pick from dropdown
                </button>
              </div>
            </>
          ) : (
            <>
              <ModelSelectControl
                id="lmstudio-default-model"
                ariaLabel="Default model"
                value={defaultModel}
                onChange={setDefaultModel}
                className="lmchat-input lmchat-model-select--form"
                testId="lmstudio-default-model"
                placeholder={
                  modelOptions.length === 0
                    ? "— No models reported — Test connection to populate —"
                    : "— Select a model —"
                }
                options={modelOptions}
                {...(modelGroups.length > 1 ? { groups: modelGroups } : {})}
              />
              <div className="lmchat-model-hint-row">
                <button
                  type="button"
                  onClick={() => {
                    refreshModelsMutation.mutate(undefined, {
                      onError: (err) => {
                        const detail = err.detail ?? err.message;
                        const isForbidden = err.status === 403;
                        push({
                          variant: isForbidden ? "warning" : "error",
                          message: isForbidden
                            ? "Refresh requires admin access."
                            : `Couldn't refresh models: ${detail}`,
                        });
                      },
                    });
                  }}
                  disabled={refreshModelsMutation.isPending}
                  className="lmchat-link-btn lmchat-text-link"
                  data-testid="lmstudio-default-model-refresh"
                >
                  {refreshModelsMutation.isPending
                    ? "Refreshing…"
                    : "Refresh list"}
                </button>
                <span className="lmchat-field-hint">·</span>
                <button
                  type="button"
                  onClick={() => {
                    setManualEntry(true);
                  }}
                  className="lmchat-link-btn lmchat-text-link"
                  data-testid="lmstudio-default-model-manual"
                >
                  Type manually
                </button>
              </div>
            </>
          )}
        </div>

        {saveError !== null && (
          <p
            id="lmstudio-save-error"
            role="alert"
            className="lmchat-form-error"
            data-testid="lmstudio-save-error"
          >
            {saveError}
          </p>
        )}

        {/* Inline action row — Test + Save share a baseline; result
            chip wraps onto the next line if the form is narrow. */}
        <div className="lmchat-form-actions" style={{ flexWrap: "wrap" }}>
          {/* Admin gate: POST /api/settings/lmstudio/test and the
              admin LM Studio default PATCH both require admin server-
              side. Non-admins otherwise see these buttons and get
              silent 403s on click. Disable + hint instead. */}
          <button
            type="submit"
            disabled={saving}
            className="lmchat-btn-primary"
            data-testid="lmstudio-save"
          >
            {saving ? "Saving…" : "Save"}
          </button>
          <button
            type="button"
            onClick={() => {
              void handleTest();
            }}
            disabled={testing || baseUrl === ""}
            className="lmchat-btn-secondary"
            data-testid="lmstudio-test-connection"
          >
            {testing ? "Probing…" : "Test connection"}
          </button>
          {testResult !== null && (
            <div
              role={testResult.ok ? "status" : "alert"}
              data-testid="lmstudio-test-result"
              className={`lmchat-probe-banner lmchat-probe-banner--${testResult.ok ? "ok" : "err"}`}
            >
              <span className="lmchat-probe-banner-dot" aria-hidden />
              <span className="lmchat-probe-banner-text">
                  {testResult.ok
                    ? `Connected — ${String(testResult.model_count ?? 0)} models reachable`
                    : testResult.error ?? "Probe failed"}
                </span>
            </div>
          )}
        </div>
      </form>

      {/* ── Endpoint mode (native vs OpenAI-compatible) ───────────────────── */}
      <EndpointModeCard />

      {/* ── Memory indexing surface ───────────────────────────────────────── */}
      <MemoryIndexingCard />
    </div>
  );
}

function EndpointModeCard() {
  const { data: lmConfig } = useLmStudioConfig();
  const qc = useQueryClient();
  const { push } = useToast();

  const [pending, setPending] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const endpointMode = lmConfig?.lm_studio_endpoint_mode ?? "native";

  const handleChange = useCallback(
    async (value: "native" | "openai_compat"): Promise<void> => {
      setError(null);
      setPending(true);
      try {
        await api.request<unknown>("/api/settings/lmstudio/endpoint-mode", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ endpoint_mode: value }),
        });
        await qc.invalidateQueries({ queryKey: lmStudioConfigKeys.resolved() });
        push({ variant: "success", message: "Endpoint mode saved." });
      } catch (err) {
        const msg =
          (err as { detail?: string } | null)?.detail ??
          (err instanceof Error ? err.message : "Failed to save endpoint mode.");
        setError(msg);
      } finally {
        setPending(false);
      }
    },
    [qc, push],
  );

  return (
    <div
      className="lmchat-memory-indexing-card"
      data-testid="lmstudio-endpoint-mode-card"
      role="region"
      aria-label="LM Studio endpoint mode"
    >
      <h3 className="lmchat-memory-indexing-card__title">Endpoint mode</h3>
      <div
        className="lmchat-endpoint-mode-options"
        role="radiogroup"
        aria-label="LM Studio endpoint mode"
      >
        <label className="lmchat-endpoint-mode-option">
          <span className="lmchat-endpoint-mode-option__row">
            <input
              type="radio"
              name="lmstudio-endpoint-mode"
              value="native"
              checked={endpointMode === "native"}
              disabled={pending}
              data-testid="lmstudio-endpoint-mode-native"
              onChange={() => {
                void handleChange("native");
              }}
            />
            <span className="lmchat-endpoint-mode-option__label">Native</span>
          </span>
          <p className="lmchat-field-hint">
            LM Studio runs your MCP tools itself (from ~/.lmstudio/mcp.json)
            and keeps the conversation on its side, so LM Chat sends less
            each turn — faster on long chats. Best when LM Chat and LM
            Studio are on the same machine.
          </p>
        </label>
        <label className="lmchat-endpoint-mode-option">
          <span className="lmchat-endpoint-mode-option__row">
            <input
              type="radio"
              name="lmstudio-endpoint-mode"
              value="openai_compat"
              checked={endpointMode === "openai_compat"}
              disabled={pending}
              data-testid="lmstudio-endpoint-mode-openai-compat"
              onChange={() => {
                void handleChange("openai_compat");
              }}
            />
            <span className="lmchat-endpoint-mode-option__label">
              OpenAI-compatible
            </span>
          </span>
          <p className="lmchat-field-hint">
            LM Chat runs tools through its own MCP Store — 1-click install,
            no mcp.json editing, and it works even when LM Studio is on
            another machine. Trade-off: the conversation is resent each
            turn (no server-side chaining), so very long chats cost a
            little more.
          </p>
        </label>
      </div>
      {error !== null && (
        <span
          className="lmchat-field__error"
          role="alert"
          data-testid="lmstudio-endpoint-mode-error"
        >
          {error}
        </span>
      )}
    </div>
  );
}

export function MemoryIndexingCard() {
  const { data, isLoading, isError } = useEmbeddingStatus();
  // Reads preferred_embedding_model_id +
  // loaded_embedding_models from the lmstudio settings GET. The BE
  // fields are optional — guard everywhere with optional chaining so
  // this component doesn't crash before the BE lands these fields.
  const { data: lmConfig } = useLmStudioConfig();
  const qc = useQueryClient();
  const { push } = useToast();

  const [embeddingError, setEmbeddingError] = useState<string | null>(null);
  const [embeddingPending, setEmbeddingPending] = useState<boolean>(false);

  // Defensive dedupe at the list-construction site — same rationale as
  // SetupLmStudio/Memory: an upstream snapshot that isn't unique on `key`
  // would otherwise render duplicate <option>s.
  const loadedEmbedders = dedupeByKey(
    lmConfig?.loaded_embedding_models ?? [],
    (m) => m.key,
  );
  const preferredEmbedderId = lmConfig?.preferred_embedding_model_id ?? null;

  // Background-tasks model: out-of-band auxiliary LLM calls (auto-memory
  // distillation, chat titles, follow-up chips) use this instead of the chat
  // model so they stop competing with the user's next turn. null/"" = "Same
  // as chat model" (today's default). The BE fields are optional — guard with
  // optional chaining so this component is safe before the setting lands.
  const [backgroundError, setBackgroundError] = useState<string | null>(null);
  const [backgroundPending, setBackgroundPending] = useState<boolean>(false);
  const loadedBackgroundModels = dedupeByKey(
    lmConfig?.loaded_background_models ?? [],
    (m) => m.key,
  );
  const preferredBackgroundId = lmConfig?.preferred_background_model_id ?? null;
  const backgroundAvailable =
    lmConfig !== undefined && "loaded_background_models" in lmConfig;

  const handleBackgroundModelChange = useCallback(
    async (value: string): Promise<void> => {
      // value="" means "Same as chat model" (clear preference).
      const body = { background_model_id: value === "" ? null : value };
      setBackgroundError(null);
      setBackgroundPending(true);
      try {
        await api.request<unknown>(
          "/api/settings/lmstudio/background-model",
          { method: "PATCH", body: JSON.stringify(body) },
        );
        await qc.invalidateQueries({ queryKey: lmStudioConfigKeys.resolved() });
        push({
          variant: "success",
          message: "Background-tasks model preference saved.",
        });
      } catch (err) {
        const msg =
          (err as { detail?: string } | null)?.detail ??
          (err instanceof Error
            ? err.message
            : "Failed to save background-tasks model.");
        setBackgroundError(msg);
      } finally {
        setBackgroundPending(false);
      }
    },
    [qc, push],
  );
  // The embedder the index/recall path actually resolves to. The BE marks
  // exactly one loaded entry `active` (the resolver's pick — which, in the
  // Auto case, isn't something the FE can derive from the preference alone).
  const activeEmbedderKey =
    loadedEmbedders.find((m) => m.active === true)?.key ?? null;

  const handleEmbeddingModelChange = useCallback(
    async (value: string): Promise<void> => {
      // value="" means Auto (clear preference)
      const body = { embedding_model_id: value === "" ? null : value };
      setEmbeddingError(null);
      setEmbeddingPending(true);
      try {
        await api.request<unknown>(
          "/api/settings/lmstudio/embedding-model",
          { method: "PATCH", body: JSON.stringify(body) },
        );
        // Invalidate so the badge + selector re-read the new preference.
        await qc.invalidateQueries({ queryKey: lmStudioConfigKeys.resolved() });
        push({ variant: "success", message: "Embedding model preference saved." });
      } catch (err) {
        const msg =
          (err as { detail?: string } | null)?.detail ??
          (err instanceof Error ? err.message : "Failed to save embedding model.");
        setEmbeddingError(msg);
      } finally {
        setEmbeddingPending(false);
      }
    },
    [qc, push],
  );

  // Whether the lmstudio GET has the embedding-model preference fields (BE landed).
  const fixAAvailable = lmConfig !== undefined && "loaded_embedding_models" in lmConfig;

  return (
    <div
      className="lmchat-memory-indexing-card"
      data-testid="lmstudio-memory-indexing-card"
      role="region"
      aria-label="Memory indexing"
    >
      <h3 className="lmchat-memory-indexing-card__title">Memory indexing</h3>
      {isLoading && (
        <p className="lmchat-memory-indexing-card__row">Checking…</p>
      )}
      {isError && (
        <p className="lmchat-memory-indexing-card__row">
          Couldn't read indexing status.
        </p>
      )}
      {data !== undefined && (
        <>
          <p className="lmchat-memory-indexing-card__row">
            <span className="lmchat-memory-indexing-card__label">
              Active embedding model
            </span>
            {fixAAvailable ? (
              <span className="lmchat-memory-indexing-card__value">
                <select
                  data-testid="lmstudio-embedding-model-select"
                  className="lmchat-select"
                  value={preferredEmbedderId ?? ""}
                  disabled={embeddingPending}
                  aria-label="Active embedding model"
                  onChange={(e) => {
                    void handleEmbeddingModelChange(e.target.value);
                  }}
                >
                  {/* Auto = clear the pin → resolver auto-picks the first
                      loaded embedder. The caption below names whichever model
                      that resolves to, so "Auto" is never a mystery. */}
                  <option value="">
                    {activeEmbedderKey !== null
                      ? `Auto (using ${activeEmbedderKey})`
                      : "Auto (first loaded)"}
                  </option>
                  {/* Every entry here is LOADED — the BE filters out
                      downloaded-but-unloaded quant variants (pinning one
                      caused a memory/RAG outage). The active one carries a
                      "· active" marker so it's obvious which is in effect. */}
                  {loadedEmbedders.map((m) => (
                    <option key={m.key} value={m.key}>
                      {m.key} · loaded
                      {m.active === true ? " · active" : ""}
                    </option>
                  ))}
                </select>
                {/* Quiet caption — names the resolved active embedder only when
                    Auto is selected and the resolver's pick differs from the
                    select's own value. When a model is pinned the select already
                    shows which one is active; the extra line is redundant. */}
                {activeEmbedderKey !== null &&
                  activeEmbedderKey !== preferredEmbedderId && (
                  <span
                    className="lmchat-memory-indexing-card__hint"
                    data-testid="lmstudio-embedding-model-active"
                  >
                    Active: <code>{activeEmbedderKey}</code>
                  </span>
                )}
                {embeddingError !== null && (
                  <span
                    className="lmchat-field__error"
                    role="alert"
                    data-testid="lmstudio-embedding-model-error"
                  >
                    {embeddingError}
                  </span>
                )}
              </span>
            ) : (
              <code
                className="lmchat-memory-indexing-card__value"
                data-embedding-status={data.embedding_status}
              >
                {data.active_model_id ?? "none loaded"}
              </code>
            )}
          </p>
          {backgroundAvailable && (
            <p className="lmchat-memory-indexing-card__row">
              <span className="lmchat-memory-indexing-card__label">
                Background-tasks model
              </span>
              <span className="lmchat-memory-indexing-card__value">
                <select
                  data-testid="lmstudio-background-model-select"
                  className="lmchat-select"
                  value={preferredBackgroundId ?? ""}
                  disabled={backgroundPending}
                  aria-label="Background-tasks model"
                  onChange={(e) => {
                    void handleBackgroundModelChange(e.target.value);
                  }}
                >
                  {/* "" = Same as chat model — today's default. The
                      out-of-band tasks then reuse the chat's model. */}
                  <option value="">Same as chat model</option>
                  {/* Every entry is a LOADED LLM (the BE filters out
                      downloaded-but-unloaded models). Only embedding models
                      are excluded — they are not chat LLMs. Coder models ARE
                      eligible: they extract memory fine.
                      Real LM Studio model names are the brand — not prettified. */}
                  {loadedBackgroundModels
                    .filter((m) => !/embed/i.test(m.key))
                    .map((m) => (
                      <option key={m.key} value={m.key}>
                        {m.key} · loaded
                      </option>
                    ))}
                  {/* If the currently-saved pref is an embedding model (not a
                      chat LLM), surface it as a disabled option so the select
                      doesn't jump to "" silently. */}
                  {preferredBackgroundId !== null &&
                    /embed/i.test(preferredBackgroundId) &&
                    loadedBackgroundModels.some(
                      (m) => m.key === preferredBackgroundId,
                    ) && (
                      <option
                        key={preferredBackgroundId}
                        value={preferredBackgroundId}
                        disabled
                      >
                        {preferredBackgroundId} · loaded (not recommended)
                      </option>
                    )}
                </select>
                {/* Recommendation hint — shown only when no background model
                    is pinned AND a loaded chat LLM is available. Guidance
                    only; the select value stays "Same as chat model" until
                    the user explicitly picks one. */}
                {preferredBackgroundId === null && (() => {
                  const recommended = loadedBackgroundModels.find(
                    (m) => !/embed/i.test(m.key),
                  );
                  return recommended !== undefined ? (
                    <span
                      className="lmchat-memory-indexing-card__hint"
                      data-testid="lmstudio-background-model-recommendation"
                    >
                      Recommended: <code>{recommended.key}</code> — a small, fast loaded model keeps auto-memory cheap.
                    </span>
                  ) : null;
                })()}
                <span
                  className="lmchat-memory-indexing-card__hint"
                  data-testid="lmstudio-background-model-hint"
                >
                  Used for memory, titles, and follow-ups — keeps your chat
                  model free.
                </span>
                {backgroundError !== null && (
                  <span
                    className="lmchat-field__error"
                    role="alert"
                    data-testid="lmstudio-background-model-error"
                  >
                    {backgroundError}
                  </span>
                )}
              </span>
            </p>
          )}
          <p className="lmchat-memory-indexing-card__row">
            <span className="lmchat-memory-indexing-card__label">
              Messages indexed
            </span>
            <span className="lmchat-memory-indexing-card__value">
              {data.total_indexed_messages}
            </span>
          </p>
          <p className="lmchat-memory-indexing-card__row">
            <span className="lmchat-memory-indexing-card__label">
              Last indexed
            </span>
            <span className="lmchat-memory-indexing-card__value">
              {data.last_indexed_at !== null
                ? formatRelativeTime(data.last_indexed_at)
                : "never"}
            </span>
          </p>
          {/* Switch on the resolver sentinel (mirrors RagModeBadge.tsx:55-69).
              The prior render only checked `active_model_id === null`,
              which collided with the "list_loaded() raised silently"
              path documented in memory_service.embedding_status's
              docstring. Now the warn copy matches the actual code. */}
          {data.embedding_status === "no_embedding_model" && (
            <p
              className="lmchat-memory-indexing-card__hint"
              data-testid="lmstudio-memory-indexing-card-warn"
            >
              No embedding model is loaded in LM Studio. Load one (e.g.{" "}
              <code>text-embedding-nomic-embed-text-v1.5</code>) to start
              indexing chats for memory recall.
            </p>
          )}
          {data.embedding_status === "pinned_model_unavailable" && (
            <p
              className="lmchat-memory-indexing-card__hint"
              data-testid="lmstudio-memory-indexing-card-warn"
            >
              A project pinned an embedding model that isn't currently loaded.
              Retrieval against that project's documents will be skipped until
              the model is loaded again.
            </p>
          )}
          {Object.keys(data.models_in_use).length > 1 && (
            <p className="lmchat-memory-indexing-card__hint">
              Multiple embedding models present in history. Reindex from the
              Memory page to consolidate.
            </p>
          )}
        </>
      )}
    </div>
  );
}
