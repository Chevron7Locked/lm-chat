/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Admin quota management page — /admin/quotas.
 *
 * Displays all users' quota rows in a table and allows the admin to edit
 * per-user limits inline.  Read-only for non-admins; gated by the
 * useAdminQuotaList hook (disabled when user is not admin).
 */
import { useState } from "react";
import { useAdminQuotaList, useUpdateQuota } from "@/hooks/useQuota";
import type { QuotaSummary } from "@/hooks/useQuota";
import "@/styles/admin.css";

export default function AdminQuotas() {
  const { data: quotas, isLoading, error, refetch } = useAdminQuotaList();
  const updateQuota = useUpdateQuota();

  // Inline edit state: keyed by userId.
  const [editing, setEditing] = useState<
    Record<number, { tokens: string; requests: string }>
  >({});

  function startEdit(q: QuotaSummary) {
    setEditing((prev) => ({
      ...prev,
      [q.user_id]: {
        tokens: String(q.tokens_per_day),
        requests: String(q.requests_per_day),
      },
    }));
  }

  function cancelEdit(userId: number) {
    setEditing((prev) => {
      return Object.fromEntries(
        Object.entries(prev).filter(([k]) => Number(k) !== userId),
      );
    });
  }

  function handleSave(userId: number) {
    const values = editing[userId];
    if (!values) return;
    const tokensPerDay = parseInt(values.tokens, 10);
    const requestsPerDay = parseInt(values.requests, 10);
    if (isNaN(tokensPerDay) || isNaN(requestsPerDay)) return;

    updateQuota.mutate(
      { userId, tokensPerDay, requestsPerDay },
      {
        onSuccess: () => {
          cancelEdit(userId);
          void refetch();
        },
      },
    );
  }

  return (
    <div className="admin-page">
      <div className="admin-page-header">
        {/* context-specific eyebrow */}
        <span className="admin-eyebrow">Admin</span>
        <h1 className="admin-page-title">Quotas</h1>
      </div>

      <p className="admin-page-desc">
        Users without an explicit quota row use system defaults (100 000
        tokens/day, 1 000 requests/day).
      </p>

      {isLoading && <p className="admin-empty">Loading quotas…</p>}
      {error != null && (
        <p className="admin-error">
          Couldn't load quotas: {error.detail ?? error.message}
        </p>
      )}

      {quotas?.length === 0 && (
        <p className="admin-empty">
          No explicit quota rows. All users use system defaults.
        </p>
      )}

      {quotas != null && quotas.length > 0 && (
        <div className="admin-table-wrap">
          <table className="admin-table">
            <thead>
              <tr>
                <th className="admin-th">User ID</th>
                <th className="admin-th">Tokens / Day</th>
                <th className="admin-th">Requests / Day</th>
                <th className="admin-th">Actions</th>
              </tr>
            </thead>
            <tbody>
              {quotas.map((q) => {
                const isEditingRow = editing[q.user_id] !== undefined;
                const vals = editing[q.user_id];
                return (
                  <tr key={q.user_id}>
                    <td className="admin-td admin-td--muted">{q.user_id}</td>
                    <td className="admin-td">
                      {isEditingRow && vals != null ? (
                        <input
                          type="number"
                          min={0}
                          value={vals.tokens}
                          onChange={(e) => {
                            const v = e.target.value;
                            setEditing((prev) => {
                              const cur = prev[q.user_id];
                              if (!cur) return prev;
                              return {
                                ...prev,
                                [q.user_id]: { ...cur, tokens: v },
                              };
                            });
                          }}
                          className="admin-inline-input"
                          aria-label={`Tokens per day for user ${String(q.user_id)}`}
                        />
                      ) : (
                        q.tokens_per_day.toLocaleString()
                      )}
                    </td>
                    <td className="admin-td">
                      {isEditingRow && vals != null ? (
                        <input
                          type="number"
                          min={0}
                          value={vals.requests}
                          onChange={(e) => {
                            const v = e.target.value;
                            setEditing((prev) => {
                              const cur = prev[q.user_id];
                              if (!cur) return prev;
                              return {
                                ...prev,
                                [q.user_id]: { ...cur, requests: v },
                              };
                            });
                          }}
                          className="admin-inline-input"
                          aria-label={`Requests per day for user ${String(q.user_id)}`}
                        />
                      ) : (
                        q.requests_per_day.toLocaleString()
                      )}
                    </td>
                    <td className="admin-td">
                      <div className="admin-action-group">
                        {isEditingRow ? (
                          <>
                            <button
                              type="button"
                              onClick={() => {
                                handleSave(q.user_id);
                              }}
                              disabled={updateQuota.isPending}
                              className="admin-btn-primary"
                            >
                              {updateQuota.isPending ? "Saving…" : "Save"}
                            </button>
                            <button
                              type="button"
                              onClick={() => {
                                cancelEdit(q.user_id);
                              }}
                              className="admin-btn-secondary"
                            >
                              Cancel
                            </button>
                          </>
                        ) : (
                          <button
                            type="button"
                            onClick={() => {
                              startEdit(q);
                            }}
                            className="admin-btn-secondary"
                          >
                            Edit
                          </button>
                        )}
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
