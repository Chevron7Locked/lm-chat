/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useMcpStore — data hooks for Settings → MCP Servers.
 *
 * Mirrors the useProviders.ts pattern:
 *   - useQuery for reads (catalog, servers, server tools)
 *   - useMutation for writes (install, patch, delete)
 *   - api client from @/lib/api (never hand-rolled fetch)
 *
 * Shapes match CONTRACT-mcp-store.md exactly.
 * PATCH + GET {slug}/tools are B4 additions — typed and wired here so the FE
 * compiles; the live round-trip lands when B4 merges.
 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";

// ─── Contract types ──────────────────────────────────────────────────────────

interface CatalogSecret {
  key: string;
  label: string;
  required: boolean;
}

export interface CatalogEntry {
  id: string;
  name: string;
  description: string;
  transport: string;
  command?: string | null;
  args?: string[];
  url?: string | null;
  secrets: CatalogSecret[];
  source: string;
  trust: string;
}

export interface McpServer {
  id: string;
  slug: string;
  name: string;
  transport: string;
  command?: string | null;
  args?: string[];
  url?: string | null;
  /** Key names with stored values — values are NEVER returned. */
  secrets_set: string[];
  enabled: boolean;
  source: string;
  trust: string;
  /** Vestigial — install=consent; always true. Keep the field. */
  consented: boolean;
  connected: boolean;
  /** Real reason the last connect attempt failed, if any (e.g. the
   *  crashed server's own stderr). Absent/null when never attempted or
   *  currently connected. */
  last_error?: string | null;
  /** Denylist of namespaced tool names. Empty/absent ⇒ all tools allowed. */
  tool_policy: string[];
}

interface McpToolEntry {
  name: string;
  description: string;
  denied: boolean;
}

export interface McpServerTools {
  slug: string;
  connected: boolean;
  tools: McpToolEntry[];
  error?: string | null;
}

// ─── Query keys ─────────────────────────────────────────────────────────────

const mcpStoreKeys = {
  all: ["mcp-store"] as const,
  catalog: () => [...mcpStoreKeys.all, "catalog"] as const,
  servers: () => [...mcpStoreKeys.all, "servers"] as const,
  tools: (slug: string) => [...mcpStoreKeys.all, "tools", slug] as const,
};

// ─── Queries ─────────────────────────────────────────────────────────────────

export function useMcpCatalog() {
  return useQuery<CatalogEntry[], ApiError>({
    queryKey: mcpStoreKeys.catalog(),
    queryFn: async () => api.request<CatalogEntry[]>("/api/mcp-store/catalog"),
  });
}

export function useMcpServers() {
  return useQuery<McpServer[], ApiError>({
    queryKey: mcpStoreKeys.servers(),
    queryFn: async () => api.request<McpServer[]>("/api/mcp-store/servers"),
  });
}

export function useMcpServerTools(slug: string, enabled: boolean) {
  return useQuery<McpServerTools, ApiError>({
    queryKey: mcpStoreKeys.tools(slug),
    queryFn: async () =>
      api.request<McpServerTools>(`/api/mcp-store/servers/${encodeURIComponent(slug)}/tools`),
    enabled,
    staleTime: 30_000,
  });
}

// ─── Mutations ───────────────────────────────────────────────────────────────

interface InstallFromCatalogBody {
  catalog_id: string;
  secrets?: Record<string, string>;
}

interface InstallCustomBody {
  slug: string;
  name: string;
  transport: string;
  command?: string;
  args?: string[];
  url?: string;
  secrets?: Record<string, string>;
  tool_policy?: string[];
}

export type InstallServerBody = InstallFromCatalogBody | InstallCustomBody;

export function useInstallMcpServer() {
  const qc = useQueryClient();
  return useMutation<McpServer, ApiError, InstallServerBody>({
    meta: { errorHandled: true },
    mutationFn: async (body) =>
      api.request<McpServer>("/api/mcp-store/servers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: mcpStoreKeys.servers() });
    },
  });
}

export interface PatchMcpServerBody {
  enabled?: boolean;
  tool_policy?: string[];
}

export function usePatchMcpServer() {
  const qc = useQueryClient();
  return useMutation<McpServer, ApiError, { slug: string; body: PatchMcpServerBody }>({
    meta: { errorHandled: true },
    mutationFn: async ({ slug, body }) =>
      api.request<McpServer>(`/api/mcp-store/servers/${encodeURIComponent(slug)}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    onSuccess: (_data, { slug }) => {
      void qc.invalidateQueries({ queryKey: mcpStoreKeys.servers() });
      void qc.invalidateQueries({ queryKey: mcpStoreKeys.tools(slug) });
    },
  });
}

export function useDeleteMcpServer() {
  const qc = useQueryClient();
  return useMutation<undefined, ApiError, string>({
    meta: { errorHandled: true },
    mutationFn: async (slug) =>
      api.request<undefined>(`/api/mcp-store/servers/${encodeURIComponent(slug)}`, {
        method: "DELETE",
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: mcpStoreKeys.servers() });
    },
  });
}
