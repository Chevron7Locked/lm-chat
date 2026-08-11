/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Admin model management page — /admin/models.
 *
 * Lists all models known to LM Studio and provides Load / Unload actions.
 * Download button is currently gated pending upstream success-shape confirmation.
 *
 * Unload behaviour:
 *   - N = 0 loaded instances: Unload button disabled.
 *   - N = 1 loaded instance: single-click unload (no picker needed).
 *   - N > 1 loaded instances: opens a picker showing each instance_id with
 *     an "Unload all instances" toggle at the top.
 *
 * Confirmation modal for "Unload all" to prevent accidental mass-unload.
 */
import {
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
} from "react";
import { useModels, useRefreshModels } from "@/hooks/useModels";
import type { ModelInfo } from "@/hooks/useModels";
import { useToast } from "@/stores/toastStore";
import { useLoadModel, useUnloadModel } from "@/hooks/useModelOps";
import "@/styles/admin.css";

// ─── Helper ──────────────────────────────────────────────────────────────────

function formatBytes(bytes: number): string {
  if (bytes <= 0) return "—";
  const gb = bytes / 1024 ** 3;
  if (gb >= 0.5) return `${gb.toFixed(1)} GB`;
  const mb = bytes / 1024 ** 2;
  return `${mb.toFixed(0)} MB`;
}

// ─── Confirmation modal ───────────────────────────────────────────────────────

interface ConfirmModalProps {
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
}

function ConfirmModal({ message, onConfirm, onCancel }: ConfirmModalProps) {
  const cancelRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onCancel();
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [onCancel]);

  useEffect(() => {
    cancelRef.current?.focus();
  }, []);

  function handleKeyDownModal(e: ReactKeyboardEvent<HTMLDivElement>) {
    if (e.key !== "Tab") return;
    const focusable = Array.from<HTMLElement>(
      (e.currentTarget as HTMLElement).querySelectorAll<HTMLElement>(
        "button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])",
      ),
    );
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey) {
      if (document.activeElement === first) {
        e.preventDefault();
        last?.focus();
      }
    } else {
      if (document.activeElement === last) {
        e.preventDefault();
        first?.focus();
      }
    }
  }

  return (
    <div className="admin-modal-backdrop">
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Confirm action"
        className="admin-modal-card"
        onKeyDown={handleKeyDownModal}
      >
        <p className="admin-modal-body">{message}</p>
        <div className="admin-modal-actions">
          <button
            ref={cancelRef}
            type="button"
            onClick={onCancel}
            className="admin-btn-secondary"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={onConfirm}
            className="admin-btn-danger"
          >
            Confirm
          </button>
        </div>
      </div>
    </div>
  );
}

// ─── Instance picker ─────────────────────────────────────────────────────────

interface InstancePickerProps {
  modelId: string;
  instanceIds: string[];
  onUnloadOne: (instanceId: string) => void;
  onUnloadAll: () => void;
  onClose: () => void;
}

function InstancePicker({
  modelId,
  instanceIds,
  onUnloadOne,
  onUnloadAll,
  onClose,
}: InstancePickerProps) {
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [onClose]);

  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  function handleKeyDownPicker(e: ReactKeyboardEvent<HTMLDivElement>) {
    if (e.key !== "Tab") return;
    const focusable = Array.from<HTMLElement>(
      (e.currentTarget as HTMLElement).querySelectorAll<HTMLElement>(
        "button, [href], input, select, textarea, [tabindex]:not([tabindex='-1'])",
      ),
    );
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (e.shiftKey) {
      if (document.activeElement === first) {
        e.preventDefault();
        last?.focus();
      }
    } else {
      if (document.activeElement === last) {
        e.preventDefault();
        first?.focus();
      }
    }
  }

  return (
    <>
      <button
        type="button"
        aria-label="Close dialog"
        className="admin-modal-backdrop"
        onClick={onClose}
        data-testid="admin-models-backdrop"
        style={{ border: "none", cursor: "default" }}
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Select instance to unload"
        className="admin-modal-card"
        style={{
          position: "fixed",
          top: "50%",
          left: "50%",
          transform: "translate(-50%, -50%)",
          zIndex: 1001,
        }}
        onClick={(e) => {
          e.stopPropagation();
        }}
        onKeyDown={handleKeyDownPicker}
      >
        <p className="admin-modal-body">
          {instanceIds.length} instance{instanceIds.length !== 1 ? "s" : ""}{" "}
          loaded for <strong>{modelId}</strong>
        </p>
        <button
          type="button"
          onClick={onUnloadAll}
          className="admin-btn-danger"
          style={{ width: "100%", marginBottom: 8 }}
        >
          Unload all instances
        </button>
        <hr
          style={{
            margin: "8px 0",
            border: "none",
            borderTop: "1px solid var(--color-border)",
          }}
        />
        <div className="admin-picker-list">
          {instanceIds.map((iid) => (
            <div key={iid} className="admin-picker-row">
              <span className="admin-picker-id">{iid}</span>
              <button
                type="button"
                onClick={() => {
                  onUnloadOne(iid);
                }}
                className="admin-btn-secondary"
              >
                Unload
              </button>
            </div>
          ))}
        </div>
        <div className="admin-modal-actions">
          <button
            ref={closeRef}
            type="button"
            onClick={onClose}
            className="admin-btn-secondary"
          >
            Cancel
          </button>
        </div>
      </div>
    </>
  );
}

// ─── Model row ───────────────────────────────────────────────────────────────

interface ModelRowProps {
  model: ModelInfo;
}

function ModelRow({ model }: ModelRowProps) {
  const loadMutation = useLoadModel();
  const unloadMutation = useUnloadModel();

  const [showPicker, setShowPicker] = useState(false);
  const [confirmUnloadAll, setConfirmUnloadAll] = useState(false);
  const [confirmLoadDuplicate, setConfirmLoadDuplicate] = useState(false);

  const instanceCount = model.loaded_instance_ids.length;
  const isLoadInFlight = loadMutation.isPending;
  const isUnloadInFlight = unloadMutation.isPending;
  const isBusy = isLoadInFlight || isUnloadInFlight;

  function handleLoad() {
    if (instanceCount > 0) {
      setConfirmLoadDuplicate(true);
    } else {
      loadMutation.mutate({ model: model.id });
    }
  }

  function handleConfirmLoadDuplicate() {
    setConfirmLoadDuplicate(false);
    loadMutation.mutate({ model: model.id });
  }

  function handleUnloadSingle() {
    if (instanceCount === 1) {
      const id = model.loaded_instance_ids[0];
      if (id != null) {
        unloadMutation.mutate({ instance_id: id });
      }
    } else if (instanceCount > 1) {
      setShowPicker(true);
    }
  }

  function handleUnloadOne(instanceId: string) {
    setShowPicker(false);
    unloadMutation.mutate({ instance_id: instanceId });
  }

  function handleUnloadAll() {
    setShowPicker(false);
    setConfirmUnloadAll(true);
  }

  function handleConfirmUnloadAll() {
    setConfirmUnloadAll(false);
    unloadMutation.mutate({ model: model.id, all: true });
  }

  const quantLabel = model.quantization?.name ?? "—";

  return (
    <>
      <tr>
        <td className="admin-td">
          <span className="admin-model-name">{model.name}</span>
          <span className="admin-model-id">{model.id}</span>
        </td>
        <td className="admin-td admin-td--right admin-td--muted">
          {formatBytes(model.size_bytes)}
        </td>
        <td className="admin-td admin-td--muted">
          {model.params_string || "—"}
        </td>
        <td className="admin-td admin-td--muted">{quantLabel}</td>
        <td className="admin-td admin-td--center">
          {model.loaded ? (
            <span className="admin-chip admin-chip--loaded">
              {instanceCount > 1
                ? `loaded ×${String(instanceCount)}`
                : "loaded"}
            </span>
          ) : (
            <span className="admin-chip admin-chip--inactive">not loaded</span>
          )}
        </td>
        <td className="admin-td">
          <div className="admin-action-group">
            <button
              type="button"
              onClick={handleLoad}
              disabled={isBusy}
              className="admin-btn-primary"
              aria-label={`Load ${model.name}`}
            >
              {isLoadInFlight ? "Loading…" : "Load"}
            </button>
            <button
              type="button"
              onClick={handleUnloadSingle}
              disabled={isBusy || instanceCount === 0}
              className="admin-btn-secondary"
              aria-label={`Unload ${model.name}`}
            >
              {isUnloadInFlight ? "Unloading…" : "Unload"}
            </button>
          </div>
          {(loadMutation.isError || unloadMutation.isError) && (
            <span role="alert" className="admin-row-error">
              {(loadMutation.error ?? unloadMutation.error)?.detail ?? "error"}
            </span>
          )}
        </td>
      </tr>

      {showPicker && (
        <InstancePicker
          modelId={model.id}
          instanceIds={model.loaded_instance_ids}
          onUnloadOne={handleUnloadOne}
          onUnloadAll={handleUnloadAll}
          onClose={() => {
            setShowPicker(false);
          }}
        />
      )}

      {confirmUnloadAll && (
        <ConfirmModal
          message={`Unload all ${String(instanceCount)} instances of ${model.name}?`}
          onConfirm={handleConfirmUnloadAll}
          onCancel={() => {
            setConfirmUnloadAll(false);
          }}
        />
      )}

      {confirmLoadDuplicate && (
        <ConfirmModal
          message={`This model already has ${String(instanceCount)} loaded instance${instanceCount !== 1 ? "s" : ""}. Load another?`}
          onConfirm={handleConfirmLoadDuplicate}
          onCancel={() => {
            setConfirmLoadDuplicate(false);
          }}
        />
      )}
    </>
  );
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default function AdminModels() {
  const { data, isLoading, error } = useModels();
  // Force a BE re-probe of LM Studio's catalog (POST /api/admin/models/refresh),
  // then invalidate the FE list. refetch() alone would only hit the BE's cached
  // /api/models response and surface nothing new.
  const refreshModelsMutation = useRefreshModels();
  const { push } = useToast();

  const models = data?.models ?? [];

  return (
    <div className="admin-page">
      <div className="admin-page-header">
        {/* context-specific eyebrow */}
        <span className="admin-eyebrow">Admin</span>
        <div className="admin-title-row">
          <h1 className="admin-page-title">Models</h1>
          <button
            type="button"
            onClick={() => {
              refreshModelsMutation.mutate(undefined, {
                onError: (err) => {
                  push({
                    variant: "error",
                    message: `Couldn't refresh models: ${err.detail ?? err.message}`,
                  });
                },
              });
            }}
            disabled={refreshModelsMutation.isPending}
            className="admin-btn-secondary"
            aria-label="Refresh model list"
          >
            {refreshModelsMutation.isPending ? "Refreshing…" : "Refresh"}
          </button>
        </div>
      </div>

      <p className="admin-page-desc">
        Manage model instances loaded in LM Studio. Load and Unload operations
        are proxied directly to LM Studio.
      </p>

      {isLoading && <p className="admin-empty">Loading models…</p>}

      {error != null && (
        <p className="admin-error">
          Couldn't load models: {error.detail ?? error.message}
        </p>
      )}

      {!isLoading && models.length === 0 && (
        <p className="admin-empty">No models found in LM Studio.</p>
      )}

      {models.length > 0 && (
        <div className="admin-table-wrap">
          <table className="admin-table">
            <thead>
              <tr>
                <th className="admin-th">Model</th>
                <th className="admin-th admin-th--right">Size</th>
                <th className="admin-th">Params</th>
                <th className="admin-th">Quant</th>
                <th className="admin-th admin-th--center">Status</th>
                <th className="admin-th">Actions</th>
              </tr>
            </thead>
            <tbody>
              {models.map((m) => (
                <ModelRow key={m.id} model={m} />
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
