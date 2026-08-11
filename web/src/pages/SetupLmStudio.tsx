/* SPDX-License-Identifier: Apache-2.0 */
/**
 * SetupLmStudio — step 2 of the bootstrap-admin onboarding flow.
 *
 * Three-stage flow on a single page:
 *
 *   1. Enter base URL (+ optional API key)
 *   2. "Test connection" — probes the typed creds with
 *      `POST /api/settings/lmstudio/test`, populates the model dropdown
 *      from the probe result. Result + reachable-model-count shown
 *      inline.
 *   3. Pick a default chat model from the dropdown, then "Save and
 *      continue". Save is GATED on a successful probe — the previous
 *      design let Enter save unverified config with a generic "LM
 *      Studio configured" toast that lied about the
 *      live state of the connection.
 *
 * The embedding model is read-only here (auto-detected from whatever's
 * loaded in LM Studio); the visibility card surfaces the active model
 * id + a helpful nudge when nothing is loaded.
 */
import {
  useCallback,
  useEffect,
  useMemo,
  useState,
  type SubmitEvent,
} from "react";
import { useNavigate } from "react-router-dom";
import { useQueryClient } from "@tanstack/react-query";
import { Eye, EyeOff, CheckCircle2, AlertCircle } from "lucide-react";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { useLmStudioStore } from "@/stores/lmStudioStore";
import { useAuthStore } from "@/stores/authStore";
import { modelKeys } from "@/hooks/useModels";
import { lmStudioConfigKeys } from "@/hooks/useLmStudioConfig";
import { embeddingStatusKeys } from "@/hooks/useEmbeddingStatus";
import { dedupeByKey } from "@/lib/dedupeByKey";
import { pickFastestModel } from "@/lib/modelSpeedRank";
import { BrandMark, BRAND_NAME } from "@/components/BrandMark";
import "@/styles/auth.css";
import "@/styles/setup.css";
import "@/styles/settings.css";

interface ResolvedConfig {
  base_url: string;
  default_model: string;
  api_key_set: boolean;
  source_base_url: "user" | "server_admin" | "env" | "unset";
  source_api_key: "user" | "server_admin" | "env" | "unset";
  source_default_model: "user" | "server_admin" | "env" | "unset";
  /** null = no preference saved yet; string = already chosen by admin. */
  preferred_background_model_id?: string | null;
  /** "native" (default) or "openai_compat" — optional until the BE lands it. */
  lm_studio_endpoint_mode?: "native" | "openai_compat";
}

interface ProbedModel {
  id: string;
  name: string;
  loaded: boolean;
  is_embedding: boolean;
}

interface ProbeResponse {
  ok: boolean;
  model_count?: number | null;
  error?: string | null;
  models?: ProbedModel[];
}

export default function SetupLmStudio() {
  useDocumentTitle("Connect LM Studio");
  const navigate = useNavigate();
  const qc = useQueryClient();
  const resolveProbe = useLmStudioStore((s) => s.resolveProbe);
  const isAdmin = useAuthStore((s) => s.user?.is_admin ?? false);

  // Form draft state.
  const [baseUrl, setBaseUrl] = useState<string>("");
  const [apiKey, setApiKey] = useState<string>("");
  const [showApiKey, setShowApiKey] = useState<boolean>(false);
  const [defaultModel, setDefaultModel] = useState<string>("");
  const [apiKeyAlreadySet, setApiKeyAlreadySet] = useState<boolean>(false);
  // User's embedding-model selection for this setup session. Seeded from
  // auto-detection after probe; can be overridden by the user.
  const [embeddingModel, setEmbeddingModel] = useState<string>("");
  // Reflects whether an existing background-model preference was already saved —
  // if so, the seed PATCH on save is skipped to avoid clobbering it.
  const [existingBackgroundPref, setExistingBackgroundPref] = useState<
    string | null
  >(null);
  // LM Studio endpoint-mode toggle (native vs OpenAI-compatible). Seeded from
  // the resolved config in refresh(); always sent (unconditional, no
  // "existing preference" concept to protect) on save.
  const [endpointMode, setEndpointMode] = useState<"native" | "openai_compat">(
    "native",
  );

  // Probe + save state.
  const [testing, setTesting] = useState<boolean>(false);
  const [probe, setProbe] = useState<ProbeResponse | null>(null);
  const [saving, setSaving] = useState<boolean>(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  // Re-probing required whenever the URL or API-key fields change after
  // a successful probe — the previous probe result is stale.
  const probeValid = probe?.ok === true;
  const canSave = probeValid && defaultModel !== "" && !saving;

  // Populate the dropdown DIRECTLY from the probe response. `/api/models`
  // can't see freshly-typed creds (the backend client uses the
  // last-saved config), so going through `useModelList` would leave the
  // dropdown empty on first run until the user saves unverified
  // config — exactly the loop to avoid. The probe service
  // returns a normalised model list with the typed creds, so we
  // populate from THAT.
  const modelOptions = useMemo(() => {
    const list = probe?.models ?? [];
    const loaded = list.filter((m) => !m.is_embedding && m.loaded);
    const unloaded = list.filter((m) => !m.is_embedding && !m.loaded);
    // The probe hits LM Studio directly and isn't run through the same
    // dedup /api/models applies — a model reported more than once by the
    // raw probe (e.g. a duplicated loaded instance) would otherwise emit
    // two <option key=...> with the same id. Dedupe here, keeping the
    // first (loaded) occurrence.
    return dedupeByKey([...loaded, ...unloaded], (m) => m.id);
  }, [probe]);

  // Embedding model options: ONLY loaded embedders — auto-selecting an
  // unloaded bare variant sends the embedding PATCH an unloadable key,
  // which kills RAG silently. Unloaded variants are excluded.
  const loadedEmbedderOptions = useMemo(() => {
    const list = probe?.models ?? [];
    return dedupeByKey(
      list.filter((m) => m.is_embedding && m.loaded),
      (m) => m.id,
    );
  }, [probe]);

  // Auto-select the best embedder after probe completes — nomic-family first
  // (most common), then first loaded, then clear. Only fires when the user
  // has not made a manual selection (embeddingModel === "").
  useEffect(() => {
    if (probe === null) return;
    if (embeddingModel !== "") return; // user already chose; keep their pick
    if (loadedEmbedderOptions.length === 0) return; // nothing to select
    const nomic = loadedEmbedderOptions.find((m) =>
      m.id.startsWith("text-embedding-nomic"),
    );
    const best = nomic ?? loadedEmbedderOptions[0];
    if (best !== undefined) setEmbeddingModel(best.id);
  }, [probe, loadedEmbedderOptions, embeddingModel]);

  // Pre-fill from the resolved config on mount. The bootstrap-admin
  // lands here with an env-suggested base URL pre-filled, one probe
  // away from a working config.
  const refresh = useCallback(async (): Promise<void> => {
    setLoadError(null);
    try {
      const cfg = await api.request<ResolvedConfig>("/api/settings/lmstudio");
      setApiKeyAlreadySet(cfg.api_key_set);
      // Record whether an explicit background-model preference already exists.
      // The save handler uses this to decide whether to seed a default.
      setExistingBackgroundPref(cfg.preferred_background_model_id ?? null);
      setEndpointMode(cfg.lm_studio_endpoint_mode ?? "native");
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
          // Non-admin or no env values — leave blank.
        }
      } else {
        setBaseUrl(cfg.base_url);
        setDefaultModel(cfg.default_model);
      }
    } catch (err) {
      const apiErr = err as ApiError;
      setLoadError(
        apiErr.detail ?? "Couldn't load LM Studio config — try again.",
      );
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  // Invalidate the previous probe whenever the user edits the URL or
  // API key. Probe-required-before-save is the whole point.
  function markProbeStale() {
    if (probe !== null) {
      setProbe(null);
      // Clear the embedding selection so auto-select re-fires on the next probe.
      setEmbeddingModel("");
    }
  }

  async function handleTest(): Promise<void> {
    setTesting(true);
    setProbe(null);
    setSaveError(null);
    try {
      const result = await api.request<ProbeResponse>(
        "/api/settings/lmstudio/test",
        {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            base_url: baseUrl,
            api_key: apiKey !== "" ? apiKey : null,
          }),
        },
      );
      setProbe(result);
      // Push the outcome into the connection store so the status badge
      // (rendered the moment we navigate to /) reflects reality
      // instead of the cached-empty "no probe yet" state.
      if (result.ok) {
        const loadedCount = (result.models ?? []).filter(
          (m) => m.loaded && !m.is_embedding,
        ).length;
        resolveProbe({
          ok: true,
          modelCount: result.model_count ?? 0,
          loadedCount,
        });
        // The previously-typed/env-suggested default may not exist on
        // the freshly-probed instance — clear it so the user must
        // pick something LM Studio actually reports AND can serve as
        // a chat model (not an embedding model).
        const chatIds = new Set(
          (result.models ?? []).filter((m) => !m.is_embedding).map((m) => m.id),
        );
        if (defaultModel !== "" && !chatIds.has(defaultModel)) {
          setDefaultModel("");
        }
      } else {
        resolveProbe({
          ok: false,
          error: result.error ?? "Probe failed.",
        });
      }
    } catch (err) {
      const apiErr = err as ApiError;
      const message = apiErr.detail ?? "Probe failed.";
      setProbe({ ok: false, model_count: null, error: message });
      resolveProbe({ ok: false, error: message });
    } finally {
      setTesting(false);
    }
  }

  async function handleSubmit(e: SubmitEvent<HTMLFormElement>): Promise<void> {
    e.preventDefault();
    if (!canSave) return;
    setSaving(true);
    setSaveError(null);
    try {
      const body: {
        base_url?: string;
        api_key?: string;
        default_model?: string;
      } = {};
      if (baseUrl !== "") body.base_url = baseUrl;
      if (apiKey !== "") body.api_key = apiKey;
      if (defaultModel !== "") body.default_model = defaultModel;
      // Admin path is the only one that rewires the live LM Studio
      // singletons AND refreshes the shared model cache; per-user PUT
      // does neither, so the chat shell would land with an empty
      // model dropdown and a red badge (the exact failure mode that
      // prompted this rewrite). Bootstrap-admin onboarding is
      // definitionally an admin path. Mirrors LmStudioSection.tsx:230-233.
      const endpoint = isAdmin
        ? "/api/admin/lmstudio/default"
        : "/api/settings/lmstudio";
      const method = isAdmin ? "PATCH" : "PUT";
      await api.request<ResolvedConfig>(endpoint, {
        method,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });

      // Seed the embedding-model setting when the user made a selection.
      // This is a separate PATCH so the backend records the chosen embedder
      // and future memory recall uses the correct dimension.
      if (embeddingModel !== "") {
        await api.request("/api/settings/lmstudio/embedding-model", {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ embedding_model_id: embeddingModel }),
        });
      }

      // Seed the background-model setting on first setup (when no preference
      // has been saved yet) so auto-memory distillation / titles / follow-ups
      // have a stable general-purpose model to call. Pick the FASTEST loaded
      // non-coder, non-embedding LLM (smallest active-param count) — NOT the
      // first in probe order, which on a multi-model rig was often the largest
      // model (e.g. a 122B-MoE) and made every aux call take tens of seconds /
      // time out. Does NOT fire if the admin already has a preference (re-run).
      if (existingBackgroundPref === null) {
        const generals = (probe.models ?? []).filter(
          (m) =>
            m.loaded &&
            !m.is_embedding &&
            !m.id.toLowerCase().includes("coder"),
        );
        const bgModel = pickFastestModel(generals, (m) => `${m.id} ${m.name}`);
        if (bgModel !== undefined) {
          await api.request("/api/settings/lmstudio/background-model", {
            method: "PATCH",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ background_model_id: bgModel.id }),
          });
        }
      }

      // Persist the endpoint-mode toggle. Unlike the background-model seed
      // above, this has no "existing preference" concept to protect — it's a
      // plain always-current toggle — so it fires unconditionally, every
      // save, mirroring the embedding-model PATCH's unconditional-fire
      // pattern (not the background-model's "only if unset" gating).
      await api.request("/api/settings/lmstudio/endpoint-mode", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ endpoint_mode: endpointMode }),
      });

      // The backend service rewires its upstream HTTP client on
      // override save. Force every consumer of the LM Studio config
      // and model list to refetch on the next render so the chat
      // page lands with a populated dropdown and a green badge
      // instead of inheriting the cached-empty pre-save state.
      await Promise.all([
        qc.invalidateQueries({ queryKey: modelKeys.list() }),
        qc.invalidateQueries({ queryKey: lmStudioConfigKeys.resolved() }),
        qc.invalidateQueries({ queryKey: embeddingStatusKeys.current() }),
      ]);
      void navigate("/", { replace: true });
    } catch (err) {
      const apiErr = err as ApiError;
      setSaveError(apiErr.detail ?? "Save failed.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="lmchat-setup-page">
      <div className="lmchat-setup-panel">
        <div className="lmchat-auth-brand">
          <BrandMark size={56} />
          <h1 className="lmchat-auth-wordmark">{BRAND_NAME}</h1>
          <p className="lmchat-auth-tagline">
            A panel of experts, on your machine.
          </p>
        </div>

        <div className="lmchat-auth-form-gap" aria-hidden="true" />

        <form
          onSubmit={(e) => {
            void handleSubmit(e);
          }}
          className="lmchat-auth-form"
          data-testid="setup-lmstudio-form"
          noValidate
        >
          <header className="lmchat-auth-form-header">
            <h2 className="lmchat-auth-form-title">Connect LM Studio</h2>
            <p className="lmchat-auth-form-subtitle">
              Point LM Chat at your local LM Studio. Test the connection, pick a
              default model, save.
            </p>
          </header>

          {loadError !== null && (
            <p className="lmchat-form-error" role="alert">
              {loadError}
            </p>
          )}

          <div className="lmchat-field">
            <label
              htmlFor="setup-lmstudio-base-url"
              className="lmchat-field-label"
            >
              Base URL
            </label>
            <input
              id="setup-lmstudio-base-url"
              type="url"
              value={baseUrl}
              onChange={(e) => {
                setBaseUrl(e.target.value);
                markProbeStale();
              }}
              className="lmchat-input"
              placeholder="http://localhost:1234"
              data-testid="setup-lmstudio-base-url"
              required
            />
          </div>

          <div className="lmchat-field">
            <label
              htmlFor="setup-lmstudio-api-key"
              className="lmchat-field-label"
            >
              API key{" "}
              {apiKeyAlreadySet ? "(leave blank to keep saved)" : "(optional)"}
            </label>
            <div className="lmchat-api-key-row">
              <input
                id="setup-lmstudio-api-key"
                type={showApiKey ? "text" : "password"}
                value={apiKey}
                onChange={(e) => {
                  setApiKey(e.target.value);
                  markProbeStale();
                }}
                className="lmchat-input"
                placeholder={apiKeyAlreadySet ? "•••••• (saved)" : ""}
                data-testid="setup-lmstudio-api-key"
                autoComplete="off"
              />
              <button
                type="button"
                onClick={() => {
                  setShowApiKey((v) => !v);
                }}
                className="lmchat-btn-eye"
                aria-label={showApiKey ? "Hide API key" : "Show API key"}
              >
                {showApiKey ? <EyeOff size={16} /> : <Eye size={16} />}
              </button>
            </div>
          </div>

          {/* Probe row — has to pass before Save unlocks. */}
          <div className="lmchat-field lmchat-setup-probe-row">
            <button
              type="button"
              onClick={() => {
                void handleTest();
              }}
              disabled={testing || baseUrl === ""}
              className="lmchat-btn-secondary"
              data-testid="setup-lmstudio-test-connection"
            >
              {testing ? "Testing…" : "Test connection"}
            </button>
            {probe !== null && (
              <span
                role="status"
                className={`lmchat-setup-probe-result lmchat-setup-probe-result--${probe.ok ? "ok" : "err"}`}
                data-testid="setup-lmstudio-probe-result"
              >
                {probe.ok ? (
                  <>
                    <CheckCircle2 size={14} aria-hidden="true" />
                    Connected · {String(probe.model_count ?? 0)} model
                    {(probe.model_count ?? 0) === 1 ? "" : "s"} reachable
                  </>
                ) : (
                  <>
                    <AlertCircle size={14} aria-hidden="true" />
                    {probe.error ?? "Probe failed."}
                  </>
                )}
              </span>
            )}
          </div>

          <div className="lmchat-field">
            <label
              htmlFor="setup-lmstudio-default-model"
              className="lmchat-field-label"
            >
              Default chat model
            </label>
            <select
              id="setup-lmstudio-default-model"
              value={defaultModel}
              onChange={(e) => {
                setDefaultModel(e.target.value);
              }}
              className="lmchat-select"
              data-testid="setup-lmstudio-default-model"
              disabled={!probeValid}
            >
              <option value="">
                {probeValid
                  ? modelOptions.length > 0
                    ? "— pick a model —"
                    : "No models reachable from LM Studio"
                  : "Test the connection to load models"}
              </option>
              {modelOptions.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name}
                  {o.loaded ? " · loaded" : ""}
                </option>
              ))}
            </select>
          </div>

          <div className="lmchat-field">
            <label
              htmlFor="setup-lmstudio-embedding-model"
              className="lmchat-field-label"
            >
              Embedding model (for memory)
            </label>
            <select
              id="setup-lmstudio-embedding-model"
              value={embeddingModel}
              onChange={(e) => {
                setEmbeddingModel(e.target.value);
              }}
              className="lmchat-select"
              data-testid="setup-lmstudio-embedding-model"
              disabled={!probeValid}
            >
              <option value="">
                {probeValid
                  ? loadedEmbedderOptions.length === 0
                    ? "No embedding model loaded"
                    : "— pick an embedder —"
                  : "Test the connection to load models"}
              </option>
              {loadedEmbedderOptions.map((o) => (
                <option key={o.id} value={o.id}>
                  {o.name} · loaded
                </option>
              ))}
            </select>
            <p className="lmchat-setup-embedding-hint">
              {!probeValid
                ? "Test the connection to detect embedding models."
                : loadedEmbedderOptions.length > 0
                  ? "Auto-detected. Swap the loaded model in LM Studio to change it."
                  : "Load an embedding model in LM Studio (e.g. text-embedding-nomic-embed-text-v1.5) to enable memory recall."}
            </p>
          </div>

          <div className="lmchat-field">
            <span
              className="lmchat-field-label"
              id="setup-lmstudio-endpoint-mode-label"
            >
              Endpoint mode
            </span>
            <div
              className="lmchat-endpoint-mode-options"
              role="radiogroup"
              aria-labelledby="setup-lmstudio-endpoint-mode-label"
            >
              <label className="lmchat-endpoint-mode-option">
                <span className="lmchat-endpoint-mode-option__row">
                  <input
                    type="radio"
                    name="lmstudio-endpoint-mode"
                    value="native"
                    checked={endpointMode === "native"}
                    data-testid="lmstudio-endpoint-mode-native"
                    onChange={() => {
                      setEndpointMode("native");
                    }}
                  />
                  <span className="lmchat-endpoint-mode-option__label">
                    Native
                  </span>
                </span>
                <p className="lmchat-field-hint">
                  LM Studio runs your MCP tools itself (from
                  ~/.lmstudio/mcp.json) and keeps the conversation on its
                  side, so LM Chat sends less each turn — faster on long
                  chats. Best when LM Chat and LM Studio are on the same
                  machine.
                </p>
              </label>
              <label className="lmchat-endpoint-mode-option">
                <span className="lmchat-endpoint-mode-option__row">
                  <input
                    type="radio"
                    name="lmstudio-endpoint-mode"
                    value="openai_compat"
                    checked={endpointMode === "openai_compat"}
                    data-testid="lmstudio-endpoint-mode-openai-compat"
                    onChange={() => {
                      setEndpointMode("openai_compat");
                    }}
                  />
                  <span className="lmchat-endpoint-mode-option__label">
                    OpenAI-compatible
                  </span>
                </span>
                <p className="lmchat-field-hint">
                  LM Chat runs tools through its own MCP Store — 1-click
                  install, no mcp.json editing, and it works even when LM
                  Studio is on another machine. Trade-off: the conversation
                  is resent each turn (no server-side chaining), so very
                  long chats cost a little more.
                </p>
              </label>
            </div>
          </div>

          {saveError !== null && (
            <p className="lmchat-form-error" role="alert">
              {saveError}
            </p>
          )}

          <div className="lmchat-setup-actions">
            <button
              type="submit"
              className="lmchat-btn-primary atelier-cta"
              disabled={!canSave}
              data-testid="setup-lmstudio-save"
            >
              {saving ? "Saving…" : "Save and continue →"}
            </button>
            {/* Skip is gated on a successful probe — without verified
                config the chat shell lands with an empty model dropdown
                and a red badge (the exact failure mode this page was
                built to prevent). If you genuinely want to defer setup,
                hit Test → see the model list → THEN Skip. The disabled
                button surfaces a hint so the path is discoverable. */}
            <button
              type="button"
              onClick={() => {
                void navigate("/", { replace: true });
              }}
              disabled={!probeValid}
              className="lmchat-btn-ghost"
              data-testid="setup-lmstudio-skip"
              title={probeValid ? "Skip setup" : "Test the connection first"}
            >
              Skip for now
            </button>
          </div>
          {!probeValid && (
            <p className="lmchat-setup-gate-hint">
              Test the connection to enable Save and Skip.
            </p>
          )}
        </form>
      </div>
    </div>
  );
}
