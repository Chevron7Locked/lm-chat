/* SPDX-License-Identifier: Apache-2.0 */
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ApiError } from "@/lib/api";
import { modelKeys } from "@/hooks/useModels";

export interface ProviderConfigSafeView {
  provider: string;
  base_url: string;
  default_model: string | null;
  extra_headers: Record<string, string> | null;
  enabled: boolean;
  api_key_set: boolean;
  /** null / absent = all models allowed; non-empty = only these model ids */
  allowed_models?: string[] | null;
}

export interface ProviderStatus {
  provider: string;
  reachable: boolean;
  error: string | null;
}

export interface ProbeResponse {
  ok: boolean;
  model_count?: number | null;
  /** Full list of model ids available from the provider — populated on ok=true */
  model_ids?: string[] | null;
  error?: string | null;
}

const providerKeys = {
  all: ["providers"] as const,
  list: () => [...providerKeys.all, "list"] as const,
  status: () => [...providerKeys.all, "status"] as const,
};

export function useProviders() {
  return useQuery<ProviderConfigSafeView[], ApiError>({
    queryKey: providerKeys.list(),
    queryFn: async () => api.request<ProviderConfigSafeView[]>("/api/admin/providers"),
  });
}

export function useProviderStatus() {
  return useQuery<ProviderStatus[], ApiError>({
    queryKey: providerKeys.status(),
    queryFn: async () => api.request<ProviderStatus[]>("/api/providers/status"),
    staleTime: 30_000,
    refetchInterval: 60_000,
  });
}

export interface UpsertProviderBody {
  base_url: string;
  api_key?: string;
  default_model?: string;
  extra_headers?: Record<string, string>;
  enabled: boolean;
  /** null / [] = allow all; non-empty = restrict to these model ids */
  allowed_models?: string[] | null;
}

export function useUpsertProvider() {
  const qc = useQueryClient();
  return useMutation<ProviderConfigSafeView, ApiError, { provider: string; body: UpsertProviderBody }>({
    meta: { errorHandled: true },
    mutationFn: async ({ provider, body }) =>
      api.request<ProviderConfigSafeView>(`/api/admin/providers/${provider}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: providerKeys.list() });
      void qc.invalidateQueries({ queryKey: providerKeys.status() });
      void qc.invalidateQueries({ queryKey: modelKeys.list() });
    },
  });
}

export function useDeleteProvider() {
  const qc = useQueryClient();
  return useMutation<undefined, ApiError, string>({
    meta: { errorHandled: true },
    mutationFn: async (provider) =>
      api.request<undefined>(`/api/admin/providers/${provider}`, { method: "DELETE" }),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: providerKeys.list() });
      void qc.invalidateQueries({ queryKey: providerKeys.status() });
      void qc.invalidateQueries({ queryKey: modelKeys.list() });
    },
  });
}

interface TestProviderVars {
  provider: string;
  base_url?: string;
  api_key?: string;
  extra_headers?: Record<string, string>;
}

export function useTestProvider() {
  return useMutation<ProbeResponse, ApiError, TestProviderVars>({
    meta: { errorHandled: true },
    mutationFn: async ({ provider, ...body }) =>
      api.request<ProbeResponse>(`/api/admin/providers/${provider}/test`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      }),
  });
}
