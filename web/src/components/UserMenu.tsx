/* SPDX-License-Identifier: Apache-2.0 */
/**
 * UserMenu — avatar/initials button in the Sidebar footer that opens a
 * dropdown with username, Settings link, and Sign out button.
 *
 * Outside-click semantics:
 *   - Press inside the menu + drag outside + release outside MUST NOT close
 *     the menu (press-inside-drag-out).
 *   - True outside press-release (mousedown AND mouseup both outside the
 *     menu) MUST close the menu.
 *   - This is achieved by tracking whether the mousedown originated inside
 *     the menu root. On document mouseup: if the origin was inside, do not
 *     close; if the origin was outside AND the mouseup target is also outside,
 *     close.
 */
import { useState, useRef, useEffect, useCallback } from "react";
import type { CSSProperties } from "react";
import { Link } from "react-router-dom";
import { useAuthStore } from "@/stores/authStore";
import { useDropdownKeyboard } from "@/hooks/useDropdownKeyboard";

interface UserMenuProps {
  /** Called when the user clicks "Sign out". */
  onSignOut?: () => void;
  /** Called when the user clicks "Settings". */
  onSettings?: () => void;
}

export function UserMenu({ onSignOut, onSettings }: UserMenuProps) {
  const { user, logout } = useAuthStore();
  const [open, setOpen] = useState(false);

  const handleClose = useCallback(() => {
    setOpen(false);
  }, []);
  const { containerProps: dropdownKeyboardProps } = useDropdownKeyboard({
    open,
    onClose: handleClose,
  });

  // Ref for the whole menu root (button + dropdown).
  const menuRef = useRef<HTMLDivElement>(null);

  // Track whether the most-recent mousedown originated inside the menu.
  const pressedInsideRef = useRef(false);

  // Outside-click: close only on TRUE outside press-release.
  useEffect(() => {
    function onMouseDown(e: MouseEvent) {
      pressedInsideRef.current =
        menuRef.current?.contains(e.target as Node) ?? false;
    }

    function onMouseUp(e: MouseEvent) {
      // If the press started inside, never close on this mouseup.
      if (pressedInsideRef.current) {
        pressedInsideRef.current = false;
        return;
      }
      // Press started outside — if release is also outside, close.
      const releaseInside =
        menuRef.current?.contains(e.target as Node) ?? false;
      if (!releaseInside && open) {
        setOpen(false);
      }
      pressedInsideRef.current = false;
    }

    document.addEventListener("mousedown", onMouseDown, true);
    document.addEventListener("mouseup", onMouseUp, true);
    return () => {
      document.removeEventListener("mousedown", onMouseDown, true);
      document.removeEventListener("mouseup", onMouseUp, true);
    };
  }, [open]);

  const handleSignOut = useCallback(async () => {
    setOpen(false);
    await logout();
    onSignOut?.();
  }, [logout, onSignOut]);

  const handleSettings = useCallback(() => {
    setOpen(false);
    onSettings?.();
  }, [onSettings]);

  if (user === null) return null;

  // Derive initials from username (up to 2 chars).
  const initials = user.username
    .split(/[^a-zA-Z0-9]/)
    .filter(Boolean)
    .slice(0, 2)
    .map((s) => s[0]?.toUpperCase() ?? "")
    .join("");

  return (
    <div ref={menuRef} style={containerStyle} data-testid="user-menu-root">
      <button
        type="button"
        aria-label={
          user.username ? `${user.username} — account menu` : "Account menu"
        }
        aria-expanded={open}
        aria-haspopup="menu"
        onClick={() => {
          setOpen((v) => !v);
        }}
        style={avatarButtonStyle}
        data-testid="user-menu-avatar"
      >
        {initials || "U"}
      </button>

      {open && (
        <div
          role="menu"
          aria-label="User menu"
          style={dropdownStyle}
          data-testid="user-menu-dropdown"
          onKeyDown={dropdownKeyboardProps.onKeyDown}
          ref={dropdownKeyboardProps.ref}
        >
          {/* Username read-only */}
          <div style={usernameLabelStyle}>
            <span
              style={{
                fontSize: "0.6875rem",
                textTransform: "uppercase",
                letterSpacing: "0.06em",
                color: "var(--color-text-subtle)",
              }}
            >
              Signed in as
            </span>
            <span
              style={{
                fontSize: "0.875rem",
                fontWeight: 500,
                color: "var(--color-text)",
              }}
            >
              {user.username}
            </span>
          </div>

          <hr style={dividerStyle} />

          <button
            type="button"
            role="menuitem"
            onClick={handleSettings}
            style={menuItemStyle}
            data-testid="user-menu-settings"
          >
            Settings
          </button>

          <Link
            to="/analytics"
            role="menuitem"
            onClick={() => {
              setOpen(false);
            }}
            style={{
              ...menuItemStyle,
              display: "block",
              textDecoration: "none",
            }}
            data-testid="user-menu-analytics"
          >
            Analytics
          </Link>

          <Link
            to="/prompts"
            role="menuitem"
            onClick={() => {
              setOpen(false);
            }}
            style={{
              ...menuItemStyle,
              display: "block",
              textDecoration: "none",
            }}
            data-testid="user-menu-prompts"
          >
            Prompt Library
          </Link>

          {user.is_admin && (
            <>
              <hr style={dividerStyle} />
              <Link
                to="/admin/quotas"
                role="menuitem"
                onClick={() => {
                  setOpen(false);
                }}
                style={{
                  ...menuItemStyle,
                  display: "block",
                  textDecoration: "none",
                }}
                data-testid="user-menu-admin-quotas"
              >
                Admin: Quotas
              </Link>
              <Link
                to="/admin/models"
                role="menuitem"
                onClick={() => {
                  setOpen(false);
                }}
                style={{
                  ...menuItemStyle,
                  display: "block",
                  textDecoration: "none",
                }}
                data-testid="user-menu-admin-models"
              >
                Admin: Models
              </Link>
              <Link
                to="/admin/integrations"
                role="menuitem"
                onClick={() => {
                  setOpen(false);
                }}
                style={{
                  ...menuItemStyle,
                  display: "block",
                  textDecoration: "none",
                }}
                data-testid="user-menu-admin-integrations"
              >
                Admin: Integrations
              </Link>
              <Link
                to="/admin/users"
                role="menuitem"
                onClick={() => {
                  setOpen(false);
                }}
                style={{
                  ...menuItemStyle,
                  display: "block",
                  textDecoration: "none",
                }}
                data-testid="user-menu-admin-users"
              >
                Admin: Users
              </Link>
              <Link
                to="/admin/audit-log"
                role="menuitem"
                onClick={() => {
                  setOpen(false);
                }}
                style={{
                  ...menuItemStyle,
                  display: "block",
                  textDecoration: "none",
                }}
                data-testid="user-menu-admin-audit-log"
              >
                Admin: Audit Log
              </Link>
            </>
          )}

          <button
            type="button"
            role="menuitem"
            onClick={() => {
              void handleSignOut();
            }}
            style={{ ...menuItemStyle, color: "var(--color-danger)" }}
            data-testid="user-menu-signout"
          >
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}

// ─── Styles ──────────────────────────────────────────────────────────────────

const containerStyle: CSSProperties = {
  position: "relative",
  display: "inline-flex",
};

// 36×36 meets the desktop WCAG 2.5.5 target-size floor (was 30×30).
const avatarButtonStyle: CSSProperties = {
  width: "36px",
  height: "36px",
  borderRadius: "50%",
  background: "var(--color-accent-quiet)",
  border: "1px solid var(--color-accent-border)",
  color: "var(--color-accent)",
  fontSize: "0.75rem",
  fontWeight: 600,
  cursor: "pointer",
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
  userSelect: "none",
  flexShrink: 0,
};

const dropdownStyle: CSSProperties = {
  position: "absolute",
  bottom: "calc(100% + 6px)",
  left: 0,
  minWidth: "160px",
  background: "var(--color-surface-elevated)",
  border: "1px solid var(--color-border-strong)",
  borderRadius: "var(--radius-sm)",
  boxShadow: "var(--shadow-md)",
  zIndex: 200,
  display: "flex",
  flexDirection: "column",
};

const usernameLabelStyle: CSSProperties = {
  padding: "var(--space-glue-relaxed) var(--space-sibling-relaxed)",
  display: "flex",
  flexDirection: "column",
  gap: "2px",
};

const dividerStyle: CSSProperties = {
  margin: 0,
  border: "none",
  borderTop: "1px solid var(--color-border)",
};

const menuItemStyle: CSSProperties = {
  padding: "8px var(--space-sibling-relaxed)",
  background: "none",
  border: "none",
  color: "var(--color-text)",
  fontSize: "0.875rem",
  cursor: "pointer",
  textAlign: "left",
  width: "100%",
};
