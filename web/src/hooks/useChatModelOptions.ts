/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useChatModelOptions — canonical option list for every "pick a chat
 * model" dropdown in the app.
 *
 * The embedding model (`text-embedding-nomic-embed-text-v1.5`) must
 * never appear in a chat-model selector — composer in `Chat.tsx`,
 * `AppearanceSection.tsx` Default Model, `LmStudioSection.tsx`
 * Default Model all share this hook to guarantee it. Picking an
 * embedding model as the chat model would route a chat request to an
 * embedding endpoint and fail. Unloaded chat models must also render
 * with a consistent `(unloaded)` suffix across every surface.
 *
 * This hook centralizes the predicate ("is this entry a chat-capable
 * model?") and the display formatting ("Foo · loaded" / "Foo
 * (unloaded)") so every dropdown is consistent and the embedding
 * model is never selectable as a chat model.
 *
 * Embedding detection prefers the explicit `capabilities.embedding`
 * boolean when present (set by the backend's normalizer when
 * /api/models reports `type: embedding`). Falls back to a substring
 * sniff on the model id for legacy LM Studio responses that don't
 * surface the capability flag.
 *
 * Multi-provider update: each option now carries a `provider` field and
 * the hook additionally returns a `groups` array — one ModelGroup per
 * provider, LM Studio first, then others alphabetically. Empty groups
 * are omitted so LM Studio-only deployments see no change.
 */
import { useMemo } from "react";
import { useModels, type ModelInfo } from "@/hooks/useModels";
import { dedupeByKey } from "@/lib/dedupeByKey";

interface ChatModelOption {
  /** Model id sent to the backend in payload.model. */
  id: string;
  /**
   * Display string for the dropdown. Loaded models render bare; unloaded
   * models carry an "(unloaded)" suffix, kept consistent between Settings
   * and the composer.
   */
  label: string;
  /** Raw flag — caller can use this for CSS hints (e.g. dim). */
  loaded: boolean;
  /** Provider slug — "lmstudio" for local, or cloud slug. */
  provider: string;
  /**
   * Capability flags forwarded from ModelInfo so downstream components
   * (ModelSelectControl, Composer, ChatSettingsRail) can gate on them
   * without a second useModels call.
   */
  capabilities: import("@/hooks/useModels").ModelCapabilities;
}

/** One provider group for the grouped dropdown variant. */
interface ModelGroup {
  provider: string;
  label: string;
  options: ChatModelOption[];
}

export interface UseChatModelOptionsResult {
  /**
   * Loaded chat models (local or cloud) first, then unloaded CLOUD models
   * (never unloaded local/lmstudio models — see the build loop below).
   * Embedding always excluded.
   */
  options: ChatModelOption[];
  /** Grouped by provider — LM Studio first, then alphabetical. Empty groups omitted. */
  groups: ModelGroup[];
  isLoading: boolean;
  isError: boolean;
}

function isEmbedding(m: ModelInfo): boolean {
  if (m.capabilities.embedding) return true;
  // Legacy fallback for LM Studio versions / model registries that
  // don't populate the capabilities map. Cover the family-name
  // conventions LM Studio actually surfaces: text-embedding-*,
  // *-embed-*, *-embedding-*, and BGE/GTE/UAE/E5 retrieval-encoder
  // family prefixes. A narrower sniff missed ids like
  // `nomic-embed-text-v1.5` and `bge-large-en-v1.5`; a bare
  // `includes("embedding")` check would also false-positive on any
  // model whose id happens to contain "embedding" (e.g. a
  // hypothetical "reembedding-7b" chat model). The token-anchored
  // variants below catch every realistic LM Studio embedding id
  // without that false-positive surface; the capabilities flag is
  // still the primary guard.
  const id = m.id.toLowerCase();
  if (id.startsWith("text-embedding-")) return true;
  if (id.includes("-embed-")) return true;
  if (id.includes("-embedding-")) return true;
  if (id.endsWith("-embedding")) return true;
  if (/^(bge|gte|uae|e5)[-_]/.test(id)) return true;
  return false;
}

function providerDisplayName(provider: string): string {
  const names: Record<string, string> = {
    lmstudio: "LM Studio",
    openrouter: "OpenRouter",
    groq: "Groq",
    openai: "OpenAI",
  };
  return names[provider] ?? provider;
}

export function useChatModelOptions(): UseChatModelOptionsResult {
  const { data, isLoading, isError } = useModels();
  const result = useMemo<{ options: ChatModelOption[]; groups: ModelGroup[] }>(() => {
    const all = data?.models ?? [];
    const chatModels = all.filter((m) => !isEmbedding(m));
    // Multi-instance expansion.
    //
    // When a model has multiple loaded instances (e.g. qwen3-vl-8b loaded as
    // both "model-a" and "model-b"), emit one option per instance id so
    // the user can target a specific instance. The instance id is sent as the
    // model_id on the payload; the BE resolves it directly without
    // key→instance translation.
    //
    // Single-instance loaded models continue to emit their stable model key
    // (m.id) — the existing behaviour. Unloaded models always emit m.id.
    const loaded: ChatModelOption[] = [];
    const unloaded: ChatModelOption[] = [];

    for (const m of chatModels) {
      if (m.loaded) {
        const instanceIds = m.loaded_instance_ids;
        if (instanceIds.length > 1) {
          // Multi-instance: one option per instance id.
          for (const instanceId of instanceIds) {
            loaded.push({
              id: instanceId,
              label: `${m.name} · ${instanceId}`,
              loaded: true,
              provider: m.provider,
              capabilities: m.capabilities,
            });
          }
        } else {
          // Single-instance (or no instance ids — use stable key).
          loaded.push({
            id: m.id,
            label: m.name,
            loaded: true,
            provider: m.provider,
            capabilities: m.capabilities,
          });
        }
      } else {
        // A model-picker dropdown must NEVER show — and must never
        // default to — an unloaded LOCAL (lmstudio) model. You can't
        // pick a model that isn't loaded on your own machine. Cloud
        // providers (provider !== "lmstudio") have no load/unload
        // concept — "unloaded" there just means "not currently
        // active", not "unselectable" — so cloud entries still
        // surface with the "(unloaded)" label suffix below. Extend
        // this predicate if another *local* provider that has
        // load/unload state is added later.
        if (m.provider === "lmstudio") continue;
        unloaded.push({
          id: m.id,
          label: `${m.name} (unloaded)`,
          loaded: false,
          provider: m.provider,
          capabilities: m.capabilities,
        });
      }
    }

    // Dedupe by id: a multi-instance model whose upstream
    // `loaded_instance_ids` reports the same instance id twice (or any
    // other upstream overlap between the loaded/unloaded halves) would
    // otherwise emit two options with the same `id` — React's "two
    // children with the same key" warning on every consumer of this
    // hook (composer, Settings default model, TopBar, presets). Dedupe
    // at the construction site, keeping the first (loaded) occurrence.
    const options: ChatModelOption[] = dedupeByKey(
      [...loaded, ...unloaded],
      (o) => o.id,
    );

    // Build provider groups: LM Studio first, then alphabetical cloud providers.
    // Empty groups are omitted — regression-safe when no cloud providers configured.
    const providerSlugsSet = new Set<string>();
    for (const o of options) providerSlugsSet.add(o.provider);

    const sortedSlugs = Array.from(providerSlugsSet).sort((a, b) => {
      if (a === "lmstudio") return -1;
      if (b === "lmstudio") return 1;
      return a.localeCompare(b);
    });

    const groups: ModelGroup[] = sortedSlugs.map((slug) => ({
      provider: slug,
      label: providerDisplayName(slug),
      options: options.filter((o) => o.provider === slug),
    })).filter((g) => g.options.length > 0);

    return { options, groups };
  }, [data]);

  return { ...result, isLoading, isError };
}
