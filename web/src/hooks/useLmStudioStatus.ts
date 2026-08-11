/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useLmStudioStatus — derive a freshness signal for the LM Studio connection.
 *
 * Drives the green/yellow/red status badge in the chat TopBar.  Combines two
 * data sources:
 *
 *   1. `useLmStudioHealth` (PRIMARY) — polls GET /api/lmstudio/health every
 *      10 s.  The backend's 5-second-TTL probe gives a LIVE reachability
 *      signal: `reachable: false` means LM Studio is DOWN.  This fires within
 *      ~5 s of LM Studio actually dropping, rather than up to 30 minutes later.
 *
 *   2. `useModels` (SECONDARY) — used only for the `loadedCount` display detail
 *      in the "ok" tooltip, and for the existing staleness-tick logic.  The
 *      badge never reads `loadedCount` from the catalog to decide green/red.
 *
 * Status transitions:
 *
 *   - "error":  health.reachable === false   (LM Studio unreachable)
 *             OR health.auth_failed           (401 backoff active)
 *             OR loadedCount === 0            (reachable but nothing to chat with)
 *             OR the health query itself fails (network / 5xx to our own API)
 *   - "idle":   health query has not completed yet (mount-time hydration)
 *   - "stale":  health shows reachable but the probe is old (shouldn't normally
 *               happen — 10s poll keeps it fresh; guard kept for safety)
 *   - "ok":     reachable + at least one LLM loaded
 *
 * The hook self-ticks every second so the "stale" transition fires without a
 * re-render from elsewhere in the tree.  Subscribers only see new values when
 * the status string changes — the tick state is kept local.
 *
 * Closes a finding from docs/postmortem/003-parity-audit.md.
 * Reachability fix: replaces the false-green-when-LM-Studio-is-down bug where
 * the badge read the 30-min catalog cache and saw stale `loadedCount > 0`.
 */
import { useEffect, useState, useMemo } from "react";
import { useModels } from "@/hooks/useModels";
import { useLmStudioConfig } from "@/hooks/useLmStudioConfig";
import { useLmStudioHealth } from "@/hooks/useLmStudioHealth";

type LmStudioStatus = "ok" | "stale" | "error" | "idle";

export interface LmStudioStatusInfo {
  status: LmStudioStatus;
  /** Human-readable tooltip — surfaced on the badge. */
  tooltip: string;
  /** Epoch ms of the last successful probe (0 if none). */
  lastSuccessAt: number;
  /** ms since the last successful probe (Infinity if none). */
  ageMs: number;
}

interface Options {
  /** Probe considered stale after this many ms.  Default 30s per spec. */
  staleAfterMs?: number;
  /** Re-tick interval to refresh `ageMs` derivation.  Default 1s. */
  tickMs?: number;
}

export function useLmStudioStatus(opts: Options = {}): LmStudioStatusInfo {
  const staleAfterMs = opts.staleAfterMs ?? 30_000;
  const tickMs = opts.tickMs ?? 1_000;
  const { data, isError, dataUpdatedAt, isFetching } = useModels();
  const { data: config } = useLmStudioConfig();
  const {
    data: health,
    isError: isHealthError,
    dataUpdatedAt: healthUpdatedAt,
    isFetching: isHealthFetching,
  } = useLmStudioHealth();
  const [now, setNow] = useState<number>(() => Date.now());

  useEffect(() => {
    const id = setInterval(() => {
      setNow(Date.now());
    }, tickMs);
    return () => {
      clearInterval(id);
    };
  }, [tickMs]);

  return useMemo<LmStudioStatusInfo>(() => {
    // Use the models query's dataUpdatedAt for the staleness tick — it's the
    // same source the old badge used and keeps the "stale" / ageMs display
    // consistent with the secondary data source.
    const lastSuccessAt = dataUpdatedAt;
    const ageMs = lastSuccessAt > 0 ? now - lastSuccessAt : Infinity;

    // --- IDLE: neither query has returned yet ---
    if (healthUpdatedAt === 0 && lastSuccessAt === 0) {
      return {
        status: isHealthFetching || isFetching ? "idle" : "idle",
        tooltip: "Probing LM Studio…",
        lastSuccessAt,
        ageMs,
      };
    }

    // --- PRIMARY ERROR: health query itself failed (our own API is down) ---
    if (isHealthError || isError) {
      return {
        status: "error",
        tooltip:
          "LM Studio probe failed — check that LM Studio is running and the API key is set.",
        lastSuccessAt,
        ageMs,
      };
    }

    // --- PRIMARY REACHABILITY SIGNAL from /api/lmstudio/health ---
    // When health data is available, `reachable` is the ground truth.
    if (health !== undefined) {
      // 401 auth failure takes priority over plain unreachability message.
      if (health.auth_failed || config?.auth_failed) {
        return {
          status: "error",
          tooltip:
            "LM Studio requires an API key — set it in Settings → LM Studio.",
          lastSuccessAt,
          ageMs,
        };
      }

      if (!health.reachable) {
        return {
          status: "error",
          tooltip: "LM Studio not reachable — is it running?",
          lastSuccessAt,
          ageMs,
        };
      }

      // Reachable but no LLMs loaded (embedding-only or idle fleet).
      // Use health.loaded_count (the live, authoritative value from the
      // backend probe) NOT the catalog data?.models (which lags up to 25s
      // and is undefined at mount, causing false-RED on mount-race or after
      // a model loads before the catalog poll catches up).
      if (health.loaded_count === 0) {
        return {
          status: "error",
          tooltip: "LM Studio reachable but no models loaded.",
          lastSuccessAt,
          ageMs,
        };
      }

      // Catalog loadedCount is used only for the tooltip detail text.
      const loadedCount = health.loaded_count;

      // Staleness check on the secondary models query (guard for backgrounded
      // tabs where refetchIntervalInBackground=false paused the poll).
      if (ageMs > staleAfterMs) {
        if (isFetching) {
          return {
            status: "ok",
            tooltip: "Re-probing LM Studio…",
            lastSuccessAt,
            ageMs,
          };
        }
        const secs = Math.round(ageMs / 1_000);
        return {
          status: "stale",
          tooltip: `Last LM Studio probe ${String(secs)}s ago — refreshing…`,
          lastSuccessAt,
          ageMs,
        };
      }

      return {
        status: "ok",
        tooltip: `LM Studio connected (${String(loadedCount)} model${loadedCount === 1 ? "" : "s"} loaded)`,
        lastSuccessAt,
        ageMs,
      };
    }

    // --- FALLBACK: health query not yet resolved, fall back to models query ---
    // This path covers the narrow window between mount and the first health
    // response arriving.  Mirrors the old useLmStudioStatus logic exactly.
    const loadedCount = (data?.models ?? []).filter((m) => m.loaded).length;
    if (loadedCount === 0) {
      if (config?.auth_failed) {
        return {
          status: "error",
          tooltip:
            "LM Studio requires an API key — set it in Settings → LM Studio.",
          lastSuccessAt,
          ageMs,
        };
      }
      return {
        status: "error",
        tooltip: "LM Studio reachable but no models loaded.",
        lastSuccessAt,
        ageMs,
      };
    }
    if (ageMs > staleAfterMs) {
      if (isFetching) {
        return {
          status: "ok",
          tooltip: "Re-probing LM Studio…",
          lastSuccessAt,
          ageMs,
        };
      }
      const secs = Math.round(ageMs / 1_000);
      return {
        status: "stale",
        tooltip: `Last LM Studio probe ${String(secs)}s ago — refreshing…`,
        lastSuccessAt,
        ageMs,
      };
    }
    return {
      status: "ok",
      tooltip: `LM Studio connected (${String(loadedCount)} model${loadedCount === 1 ? "" : "s"} loaded)`,
      lastSuccessAt,
      ageMs,
    };
  }, [data, isError, dataUpdatedAt, isFetching, config, health, isHealthError,
      healthUpdatedAt, isHealthFetching, now, staleAfterMs]);
}
