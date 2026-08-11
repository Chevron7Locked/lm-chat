/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Settings — sidebar-nav shell.
 *
 * Layout: 820px max-width shell, 176px sidebar nav, content pane.
 * At <768px the sidebar flips to a horizontal scrollable chip strip.
 *
 * Navigation pattern: WAI-ARIA tablist/tab/tabpanel.
 * - <nav role="tablist" aria-label="Settings navigation"> on the nav.
 * - Each button has role="tab" + aria-selected={isActive} (not aria-current).
 * - Content pane has role="tabpanel".
 * - Roving tabindex: tabIndex={0} on active, -1 on others.
 * - Keyboard: ArrowDown/Up cycle, Home/End jump, Escape blurs.
 * - data-testid="settings-tab-{id}" preserved on each button for e2e.
 *
 * URL sync via /settings/:tab? route (router.tsx — unchanged).
 *
 * Co-located settings.css replaces all CSSProperties literals.
 * Page-header: ← Back to chat as a breadcrumb pill ABOVE the
 * chapter heading (not in a separate column). Both elements live in
 * .lmchat-settings-header, stacked vertically with --space-group between.
 */
import { useCallback, useRef } from "react";
import type { KeyboardEvent as ReactKeyboardEvent, ReactNode } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { LoginSecuritySection } from "@/components/LoginSecuritySection";
import { AppearanceSection } from "@/components/AppearanceSection";
import { ChatSection } from "@/components/ChatSection";
import { ProfileSection } from "@/components/ProfileSection";
import { QuotaSection } from "@/components/QuotaSection";
import { LmStudioSection } from "@/components/LmStudioSection";
import { ProvidersSection } from "@/components/ProvidersSection";
import { PresetModelsSection } from "@/components/PresetModelsSection";
import { IntegrationsSection } from "@/components/IntegrationsSection";
import { McpStoreSection } from "@/components/McpStoreSection";
import { DeveloperSection } from "@/components/DeveloperSection";
import { MemorySettings } from "@/components/MemorySettings";
import { useViewport } from "@/hooks/useViewport";
import { useAuthStore } from "@/stores/authStore";
import { ArrowLeft } from "lucide-react";
import "@/styles/settings.css";

// ─── Nav structure ────────────────────────────────────────────────────────────

type NavId =
  | "profile"
  | "login-security"
  | "appearance"
  | "chat"
  | "memory-settings"
  | "quota"
  | "lm-studio"
  | "providers"
  | "preset-models"
  | "integrations"
  | "mcp-servers"
  | "developer";

interface NavItem {
  id: NavId;
  label: string;
  render: () => ReactNode;
  adminOnly?: boolean;
}

interface NavGroup {
  label: string;
  items: NavItem[];
}

const NAV_GROUPS: NavGroup[] = [
  {
    label: "Account",
    items: [
      { id: "profile", label: "Profile", render: () => <ProfileSection /> },
      {
        id: "login-security",
        label: "Login & Security",
        render: () => <LoginSecuritySection />,
      },
    ],
  },
  {
    label: "Models",
    items: [
      {
        id: "lm-studio",
        label: "LM Studio",
        render: () => <LmStudioSection />,
        adminOnly: true,
      },
        {
          id: "providers",
          label: "Providers",
          render: () => <ProvidersSection />,
          adminOnly: true,
        },
        {
          id: "preset-models",
          label: "Preset models",
          render: () => <PresetModelsSection />,
          adminOnly: true,
        },
    ],
  },
  {
    label: "Memory",
    items: [
      {
        id: "memory-settings",
        label: "Memory",
        render: () => <MemorySettings />,
      },
    ],
  },
  {
    label: "Tools",
    items: [
        {
          id: "mcp-servers",
          label: "MCP Servers",
          render: () => <McpStoreSection />,
          adminOnly: true,
        },
        {
          id: "integrations",
          label: "Integrations",
          render: () => <IntegrationsSection />,
          // Read-only catalogue view — GET /api/integrations/available is
          // require_user (any authenticated user); IntegrationsSection shows
          // non-admins the list + a "contact admin" note, admins a Manage link.
          // So the TAB is NOT admin-gated (only MCP Servers, which is fully
          // require_admin, is).
        },
    ],
  },
  {
    label: "Preferences",
    items: [
      {
        id: "appearance",
        label: "Appearance",
        render: () => <AppearanceSection />,
      },
      { id: "chat", label: "Chat", render: () => <ChatSection /> },
      { id: "quota", label: "Quota", render: () => <QuotaSection /> },
      {
        id: "developer",
        label: "Developer",
        render: () => <DeveloperSection />,
      },
    ],
  },
];

const DEFAULT_NAV: NavId = "profile";

// Flat list for keyboard navigation indexing
const FLAT_ITEMS: NavItem[] = NAV_GROUPS.flatMap((g) => g.items);

function isValidNav(value: string | undefined): value is NavId {
  return FLAT_ITEMS.some((item) => item.id === value);
}

// ─── Component ──────────────────────────────────────────────────────────────

export default function Settings() {
  const params = useParams<{ tab?: string }>();
  const navigate = useNavigate();
  const { isMobile } = useViewport();
  const isAdmin = useAuthStore((s) => s.user?.is_admin ?? false);

  // Filter nav items based on admin status (admin-only items hidden for non-admins).
  const visibleGroups = NAV_GROUPS.map((g) => ({
    ...g,
    items: g.items.filter((item) => !item.adminOnly || isAdmin),
  })).filter((g) => g.items.length > 0);
  const visibleItems = visibleGroups.flatMap((g) => g.items);

  const rawActive = isValidNav(params.tab) ? params.tab : DEFAULT_NAV;
  // If the requested tab is admin-only but the user is not admin, redirect to default.
  const requestedItem = FLAT_ITEMS.find((it) => it.id === rawActive);
  const active: NavId =
    requestedItem?.adminOnly && !isAdmin ? DEFAULT_NAV : rawActive;

  const activeLabel =
    visibleItems.find((it) => it.id === active)?.label ?? "Settings";
  useDocumentTitle(`Settings · ${activeLabel}`);

  // Roving tabindex: one ref slot per visible nav item
  const navItemRefs = useRef<(HTMLButtonElement | null)[]>([]);

  // Mobile nav: the grouped vertical list collapses into a native
  // <select> (see the JSX below) instead of a custom trigger + slide-in
  // panel. A look-alike trigger that opened a full-height panel with a
  // scrim read as "a sidebar opened", not "a dropdown dropped down" — a
  // real <select> gives the OS's own dropdown/picker affordance, so
  // there's no local open/close state, body scroll-lock, or Esc-handling
  // left for this page to own.
  const handleSelect = useCallback(
    (id: NavId) => {
      void navigate(`/settings/${id}`);
    },
    [navigate],
  );

  // Keyboard handler on the <nav> container — vertical roving tabindex
  function handleNavKey(e: ReactKeyboardEvent<HTMLElement>): void {
    const currentIdx = visibleItems.findIndex((it) => it.id === active);
    if (currentIdx === -1) return;
    // Assigned by every non-returning switch case below before it's read.
    let nextIdx: number;
    switch (e.key) {
      case "ArrowDown":
        e.preventDefault();
        nextIdx = (currentIdx + 1) % visibleItems.length;
        break;
      case "ArrowUp":
        e.preventDefault();
        nextIdx = (currentIdx - 1 + visibleItems.length) % visibleItems.length;
        break;
      case "Home":
        e.preventDefault();
        nextIdx = 0;
        break;
      case "End":
        e.preventDefault();
        nextIdx = visibleItems.length - 1;
        break;
      case "Escape":
        e.preventDefault();
        e.currentTarget.blur();
        return;
      default:
        return;
    }
    const next = visibleItems[nextIdx];
    if (next !== undefined) {
      handleSelect(next.id);
      navItemRefs.current[nextIdx]?.focus();
    }
  }

  const activeDef =
    visibleItems.find((item) => item.id === active) ??
    visibleItems.find((item) => item.id === DEFAULT_NAV) ??
    visibleItems[0];
  if (!activeDef) {
    // visibleItems always has at least the non-adminOnly items; this branch is unreachable.
    throw new Error("Settings: no navigation items configured");
  }

  return (
    <div className="lmchat-settings-page">
      {/* Sidebar moved to AppLayout so
          it persists across navigation. This page renders only the
          settings main column now. */}
      <main id="main-content" tabIndex={-1} className="lmchat-settings-main">
        <div className="lmchat-settings-shell" data-testid="settings-page">
          {/* ── Page header: breadcrumb + chapter title ─────────────────────
              Option B: back-to-chat as pill breadcrumb ABOVE the heading.
              Both sit in .lmchat-settings-header (flex-column, gap=group).
              The heading then owns a --space-chapter margin below before body.
          ── */}
          <header className="lmchat-settings-header">
            <button
              type="button"
              onClick={() => {
                void navigate("/");
              }}
              className="lmchat-back-crumb"
              aria-label="Back to chat"
            >
              <ArrowLeft size={12} strokeWidth={2} aria-hidden="true" />
              <span>Back to chat</span>
            </button>

            <div className="lmchat-settings-title-block">
              <span className="lmchat-settings-eyebrow">LM Chat</span>
              <h1 className="lmchat-settings-title">Settings</h1>
            </div>
          </header>

          {/* ── Body: sidebar nav + content pane ───────────────────────────── */}
          <div
            className={
              isMobile ? "lmchat-settings-body--mobile" : "lmchat-settings-body"
            }
          >
            {/* ── Settings section nav ────────────────────────────────────────
                Desktop: persistent vertical rail (WAI-ARIA tablist). Mobile:
                a real <select> with one <optgroup> per group — tapping it
                opens the platform's own dropdown/picker, matching what the
                control visually promises (previously a look-alike trigger
                opened a full-height slide-in panel with a scrim, which read
                as "a sidebar", not "a dropdown"). */}
            {isMobile ? (
              <select
                aria-label="Settings navigation"
                className="lmchat-settings-nav-select"
                value={active}
                onChange={(e) => {
                  handleSelect(e.target.value as NavId);
                }}
                data-testid="settings-nav-select"
              >
                {visibleGroups.map((group) => (
                  <optgroup key={group.label} label={group.label}>
                    {group.items.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.label}
                      </option>
                    ))}
                  </optgroup>
                ))}
              </select>
            ) : (
              <nav
                role="tablist"
                aria-label="Settings navigation"
                aria-orientation="vertical"
                className="lmchat-settings-nav"
                onKeyDown={handleNavKey}
              >
                {visibleGroups.map((group) => (
                    // Grouped vertical list (label + items).
                    <div key={group.label} className="lmchat-settings-nav-group">
                      <span className="lmchat-settings-nav-group-label">
                        {group.label}
                      </span>
                      <div className="lmchat-settings-nav-items">
                      {group.items.map((item) => {
                        const flatIdx = visibleItems.findIndex(
                          (f) => f.id === item.id,
                        );
                        const isActive = item.id === active;
                        return (
                          <button
                            key={item.id}
                            ref={(el) => {
                              navItemRefs.current[flatIdx] = el;
                            }}
                            type="button"
                            role="tab"
                            id={`settings-tab-${item.id}`}
                            aria-selected={isActive}
                            aria-controls="settings-tabpanel"
                            tabIndex={isActive ? 0 : -1}
                            onClick={() => {
                              handleSelect(item.id);
                            }}
                            className={[
                              "lmchat-settings-nav-item",
                              isActive ? "lmchat-settings-nav-item--active" : "",
                            ]
                              .filter(Boolean)
                              .join(" ")}
                            data-testid={`settings-tab-${item.id}`}
                          >
                            {item.label}
                          </button>
                        );
                      })}
                      </div>
                    </div>
                ))}
              </nav>
            )}

            {/* ── Content pane ────────────────────────────────────────────────── */}
            <div
              role="tabpanel"
              id="settings-tabpanel"
              aria-labelledby={isMobile ? undefined : `settings-tab-${active}`}
              aria-label={isMobile ? activeLabel : undefined}
              tabIndex={0}
              className="lmchat-settings-content lmchat-settings-pane"
              data-testid={`settings-panel-${active}`}
            >
              {activeDef.render()}
            </div>
          </div>
        </div>
      </main>
    </div>
  );
}
