/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Admin audit-log page (/admin/audit-log).
 *
 * Read-only viewer over the audit_log table: GET /api/admin/audit-log
 * returns a page of rows plus a total count, with an optional exact-match
 * `event` filter. Client-side pagination walks limit/offset over that
 * total — there is no edit/delete here, the audit log is append-only.
 */
import { useState } from "react";
import { useAuditLog } from "@/hooks/useAuditLog";
import type { AuditLogRow } from "@/hooks/useAuditLog";
import "@/styles/admin.css";

const PAGE_SIZE = 50;

function formatTimestamp(ts: string): string {
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return ts;
  return d.toLocaleString();
}

function formatDetail(detail: unknown): string {
  if (detail === null || detail === undefined) return "—";
  if (typeof detail === "string") return detail;
  try {
    return JSON.stringify(detail);
  } catch {
    return "[unserializable]";
  }
}

export default function AdminAuditLog() {
  const [page, setPage] = useState(0);
  const [eventInput, setEventInput] = useState("");
  const [eventFilter, setEventFilter] = useState<string | undefined>(
    undefined,
  );

  const { data, isLoading, error } = useAuditLog({
    limit: PAGE_SIZE,
    offset: page * PAGE_SIZE,
    event: eventFilter,
  });

  function applyFilter(): void {
    setPage(0);
    setEventFilter(eventInput.trim() === "" ? undefined : eventInput.trim());
  }

  function clearFilter(): void {
    setEventInput("");
    setPage(0);
    setEventFilter(undefined);
  }

  const total = data?.total ?? 0;
  const totalPages = total > 0 ? Math.ceil(total / PAGE_SIZE) : 1;
  const rows: AuditLogRow[] = data?.items ?? [];

  return (
    <div className="admin-page">
      <div className="admin-page-header">
        <span className="admin-eyebrow">Admin</span>
        <h1 className="admin-page-title">Audit Log</h1>
      </div>

      <p className="admin-page-desc">
        Append-only record of security-relevant events — logins, role
        changes, deletions, and admin actions. Filter by exact event name;
        results are ordered most-recent-first.
      </p>

      <form
        className="admin-add-form"
        onSubmit={(e) => {
          e.preventDefault();
          applyFilter();
        }}
      >
        <input
          type="text"
          value={eventInput}
          onChange={(e) => {
            setEventInput(e.target.value);
          }}
          placeholder="Filter by event, e.g. auth.login.success"
          className="admin-add-input"
          data-testid="audit-log-event-filter"
        />
        <button
          type="submit"
          className="admin-btn-primary"
          data-testid="audit-log-filter-btn"
        >
          Filter
        </button>
        {eventFilter !== undefined && (
          <button
            type="button"
            onClick={clearFilter}
            className="admin-btn-secondary"
            data-testid="audit-log-clear-btn"
          >
            Clear
          </button>
        )}
      </form>

      {isLoading && rows.length === 0 && (
        <p className="admin-empty">Loading audit log…</p>
      )}
      {error != null && (
        <p className="admin-error" data-testid="audit-log-error">
          Couldn't load the audit log: {error.detail ?? error.message}
        </p>
      )}
      {!isLoading && error == null && rows.length === 0 && (
        <p className="admin-empty">No audit log entries found.</p>
      )}

      {rows.length > 0 && (
        <div className="admin-table-wrap">
          <table className="admin-table" data-testid="admin-audit-log-table">
            <thead>
              <tr>
                <th className="admin-th">Time</th>
                <th className="admin-th">Event</th>
                <th className="admin-th">User ID</th>
                <th className="admin-th">IP</th>
                <th className="admin-th">Detail</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr
                  key={row.id}
                  data-testid={`audit-log-row-${String(row.id)}`}
                >
                  <td className="admin-td admin-td--muted">
                    {formatTimestamp(row.created_at)}
                  </td>
                  <td className="admin-td">{row.event}</td>
                  <td className="admin-td admin-td--muted">
                    {row.user_id ?? "—"}
                  </td>
                  <td className="admin-td admin-td--mono">
                    {row.ip ?? "—"}
                  </td>
                  <td
                    className="admin-td admin-td--mono"
                    title={formatDetail(row.detail)}
                  >
                    {formatDetail(row.detail)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {total > 0 && (
        <div className="admin-pagination" data-testid="audit-log-pagination">
          <button
            type="button"
            onClick={() => {
              setPage((p) => Math.max(0, p - 1));
            }}
            disabled={page === 0}
            className="admin-btn-secondary"
            data-testid="audit-log-prev-btn"
          >
            Previous
          </button>
          <span
            className="admin-pagination-status"
            data-testid="audit-log-page-status"
          >
            Page {page + 1} of {totalPages} ({total} total)
          </span>
          <button
            type="button"
            onClick={() => {
              setPage((p) => p + 1);
            }}
            disabled={(page + 1) * PAGE_SIZE >= total}
            className="admin-btn-secondary"
            data-testid="audit-log-next-btn"
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}
