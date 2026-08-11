/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Admin Users page (/admin/users).
 *
 * Lists every registered user with their role + activity timestamps and
 * exposes the four destructive/elevation actions: promote-to-admin,
 * demote-from-admin, revoke-all-sessions, delete-user.
 *
 * Also surfaces the "Invite admin" button.  The issued token is a
 * one-shot bearer that the admin shares out-of-band; the registrant passes
 * it on `/register?token=...` and lands as an admin (the token reuses the
 * bootstrap-setup wire format).
 *
 * Gated by `<RequireAdmin>` in router.tsx; this component additionally guards
 * the page-level loading state for defensive rendering.
 */
import { useState } from "react";
import type { ReactElement } from "react";
import { useAuthStore } from "@/stores/authStore";
import { useToast } from "@/stores/toastStore";
import {
  useAdminUsers,
  useDeleteUser,
  useIssueAdminInvite,
  useRevokeUserSessions,
  useSetUserRole,
} from "@/hooks/useAdminUsers";
import type { AdminUser } from "@/hooks/useAdminUsers";
import "@/styles/admin.css";

function formatTimestamp(ts: string | null): string {
  if (ts === null) return "—";
  const d = new Date(ts);
  if (Number.isNaN(d.getTime())) return "—";
  return d.toLocaleString();
}

export default function AdminUsers() {
  const currentUser = useAuthStore((s) => s.user);
  const { push } = useToast();
  const { data: users, isLoading, error } = useAdminUsers();
  const setRole = useSetUserRole();
  const revoke = useRevokeUserSessions();
  const del = useDeleteUser();
  const invite = useIssueAdminInvite();

  // Invite modal state.
  const [invitePayload, setInvitePayload] = useState<{
    token: string;
    expires_at: string;
  } | null>(null);

  // Per-row delete confirmation state.
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);

  function copyInvite(): void {
    if (invitePayload === null) return;
    const text = `${window.location.origin}/register?token=${invitePayload.token}`;
    if (typeof navigator.clipboard.writeText === "function") {
      void navigator.clipboard.writeText(text).then(
        () => {
          push({ variant: "success", message: "Invite link copied." });
        },
        () => {
          push({
            variant: "error",
            message: "Could not copy to clipboard — copy manually.",
          });
        },
      );
    } else {
      push({
        variant: "info",
        message: "Clipboard not available — copy the link manually.",
      });
    }
  }

  function handleIssueInvite(): void {
    invite.mutate(undefined, {
      onSuccess: (payload) => {
        setInvitePayload({
          token: payload.token,
          expires_at: payload.expires_at,
        });
      },
    });
  }

  function handleConfirmDelete(userId: number): void {
    del.mutate(userId, {
      onSuccess: () => {
        setConfirmDeleteId(null);
      },
    });
  }

  function roleBadge(user: AdminUser): ReactElement {
    if (user.is_admin) {
      return (
        <span
          className="admin-chip admin-chip--active"
          data-testid={`role-admin-${String(user.id)}`}
        >
          admin
        </span>
      );
    }
    return (
      <span
        className="admin-chip admin-chip--inactive"
        data-testid={`role-user-${String(user.id)}`}
      >
        user
      </span>
    );
  }

  return (
    <div className="admin-page">
      <div className="admin-page-header">
        {/* Context-specific eyebrow */}
        <span className="admin-eyebrow">Admin</span>
        <div className="admin-title-row">
          <h1 className="admin-page-title">Admin — Users</h1>
          <button
            type="button"
            onClick={handleIssueInvite}
            disabled={invite.isPending}
            className="admin-btn-primary"
            data-testid="invite-admin-btn"
          >
            {invite.isPending ? "Issuing…" : "Invite admin"}
          </button>
        </div>
      </div>

      <p className="admin-page-desc">
        Manage user accounts. Invite tokens are one-shot and expire after
        24&nbsp;hours. Deleting a user cascades through their chats, messages,
        and sessions — the audit-log entries remain.
      </p>

      {isLoading && <p className="admin-empty">Loading users…</p>}
      {error != null && (
        <p className="admin-error" data-testid="users-error">
          Couldn't load users: {error.detail ?? error.message}
        </p>
      )}

      {users != null && users.length > 0 && (
        <div className="admin-table-wrap">
          <table className="admin-table" data-testid="admin-users-table">
            <thead>
              <tr>
                <th className="admin-th">Username</th>
                <th className="admin-th">Role</th>
                <th className="admin-th">Created</th>
                <th className="admin-th">Last login</th>
                <th className="admin-th">Actions</th>
              </tr>
            </thead>
            <tbody>
              {users.map((u) => {
                const isSelf = currentUser?.id === u.id;
                return (
                  <tr key={u.id} data-testid={`user-row-${String(u.id)}`}>
                    <td className="admin-td">{u.username}</td>
                    <td className="admin-td">{roleBadge(u)}</td>
                    <td className="admin-td admin-td--muted">
                      {formatTimestamp(u.created_at)}
                    </td>
                    <td className="admin-td admin-td--muted">
                      {formatTimestamp(u.last_login)}
                    </td>
                    <td className="admin-td">
                      <div className="admin-action-group">
                        {u.is_admin ? (
                          <button
                            type="button"
                            onClick={() => {
                              setRole.mutate({ userId: u.id, isAdmin: false });
                            }}
                            disabled={setRole.isPending || isSelf}
                            title={isSelf ? "Cannot demote yourself" : ""}
                            className="admin-btn-secondary"
                            data-testid={`demote-btn-${String(u.id)}`}
                          >
                            Demote
                          </button>
                        ) : (
                          <button
                            type="button"
                            onClick={() => {
                              setRole.mutate({ userId: u.id, isAdmin: true });
                            }}
                            disabled={setRole.isPending}
                            className="admin-btn-secondary"
                            data-testid={`promote-btn-${String(u.id)}`}
                          >
                            Promote
                          </button>
                        )}
                        <button
                          type="button"
                          onClick={() => {
                            revoke.mutate(u.id);
                          }}
                          disabled={revoke.isPending}
                          className="admin-btn-secondary"
                          data-testid={`revoke-btn-${String(u.id)}`}
                        >
                          Revoke sessions
                        </button>
                        {confirmDeleteId === u.id ? (
                          <>
                            <button
                              type="button"
                              onClick={() => {
                                handleConfirmDelete(u.id);
                              }}
                              disabled={del.isPending}
                              className="admin-btn-danger"
                              data-testid={`delete-confirm-btn-${String(u.id)}`}
                            >
                              {del.isPending ? "Deleting…" : "Confirm delete"}
                            </button>
                            <button
                              type="button"
                              onClick={() => {
                                setConfirmDeleteId(null);
                              }}
                              className="admin-btn-secondary"
                              data-testid={`delete-cancel-btn-${String(u.id)}`}
                            >
                              Cancel
                            </button>
                          </>
                        ) : (
                          <button
                            type="button"
                            onClick={() => {
                              setConfirmDeleteId(u.id);
                            }}
                            disabled={isSelf}
                            title={isSelf ? "Cannot delete yourself" : ""}
                            className="admin-btn-danger-outline"
                            data-testid={`delete-btn-${String(u.id)}`}
                          >
                            Delete
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

      {invitePayload !== null && (
        <div
          role="dialog"
          aria-modal="true"
          aria-labelledby="invite-modal-heading"
          className="admin-modal-backdrop"
          data-testid="invite-modal"
        >
          <div className="admin-modal-card">
            <h3 id="invite-modal-heading" className="admin-modal-heading">
              Admin invite issued
            </h3>
            <p className="admin-modal-body">
              Share this link with the new admin. The token is one-shot and
              expires at {formatTimestamp(invitePayload.expires_at)}.
            </p>
            <code className="admin-token-box" data-testid="invite-token-link">
              {`${window.location.origin}/register?token=${invitePayload.token}`}
            </code>
            <div className="admin-modal-actions">
              <button
                type="button"
                onClick={copyInvite}
                className="admin-btn-primary"
                data-testid="invite-copy-btn"
              >
                Copy link
              </button>
              <button
                type="button"
                onClick={() => {
                  setInvitePayload(null);
                }}
                className="admin-btn-secondary"
                data-testid="invite-close-btn"
              >
                Close
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
