/* SPDX-License-Identifier: Apache-2.0 */
/**
 * IntegrationsSection — Integrations tab content.
 *
 * Read-only view of the admin-curated MCP integrations list.  Any
 * authenticated user can see the catalogue so the composer chip-row
 * and the per-message integrations picker make sense without
 * an Admin trip.  Admin users see a "Manage" link to the full
 * Admin Integrations page where they can add/remove/sort entries.
 *
 * Backend wiring
 * --------------
 *   GET /api/integrations/available  →  list[IntegrationEntry]
 *
 * The GET endpoint is open to any authenticated user (gated on
 * require_user).  PUT/POST/DELETE remain admin-only.
 *
 * Uses settings.css semantic classes rather than inline CSSProperties.
 * Spacing grammar applied:
 *   - Description ↔ heading: CHAPTER via .lmchat-section-divider
 *   - Heading → description/list: GLUE-RELAXED via .lmchat-section-description
 *   - Item → item: GLUE-RELAXED (8px) via .lmchat-integration-list gap
 */
import { Link } from "react-router-dom";
import { useIntegrationsList } from "@/hooks/useIntegrationsList";
import type { IntegrationEntry } from "@/hooks/useIntegrationsList";
import { useAuthStore } from "@/stores/authStore";
import "@/styles/settings.css";

export function IntegrationsSection() {
  const { data, isLoading, isError } = useIntegrationsList();
  const { user } = useAuthStore();
  const isAdmin = user?.is_admin === true;

  const entries: IntegrationEntry[] = data ?? [];

  return (
    <div
      className="lmchat-section-container"
      data-testid="settings-integrations-section"
    >
      {/* ── Intro block ────────────────────────────────────────────────────── */}
      <section className="lmchat-section" aria-label="Integrations overview">
        <p className="lmchat-section-description" style={{ marginTop: 0 }}>
          MCP integrations are configured by an administrator. Default-on
          integrations are pre-selected on every new chat; the rest are
          available per-message in the composer picker.
        </p>
        {isAdmin ? (
          <p
            className="lmchat-section-description"
            style={{ marginTop: "var(--space-sibling)" }}
          >
            <Link
              to="/admin/integrations"
              className="lmchat-manage-link"
              data-testid="settings-integrations-manage-link"
            >
              Manage integrations →
            </Link>
          </p>
        ) : (
          <p
            className="lmchat-section-description"
            style={{ marginTop: "var(--space-sibling)" }}
          >
            Admin manages the list. Contact your administrator to request
            changes.
          </p>
        )}
      </section>

      {/* ── Ink-bleed divider ──────────────────────────────────────────────── */}
      <hr className="lmchat-section-divider" aria-hidden="true" />

      {/* ── Available integrations list ────────────────────────────────────── */}
      <section className="lmchat-section" aria-label="Available integrations">
        <h3 className="lmchat-section-heading">Available</h3>

        {isLoading && (
          <p
            className="lmchat-section-description"
            style={{ marginTop: "var(--space-glue-relaxed)" }}
            data-testid="settings-integrations-loading"
          >
            Loading integrations…
          </p>
        )}
        {isError && (
          <p
            className="lmchat-form-error"
            style={{ marginTop: "var(--space-glue-relaxed)" }}
            data-testid="settings-integrations-error"
            role="alert"
          >
            Couldn't load integrations — try again.
          </p>
        )}
        {!isLoading && !isError && entries.length === 0 && (
          <p
            className="lmchat-section-description"
            style={{ marginTop: "var(--space-glue-relaxed)" }}
            data-testid="settings-integrations-empty"
          >
            No integrations configured yet.
          </p>
        )}
        {!isLoading && !isError && entries.length > 0 && (
          <ul
            className="lmchat-integration-list"
            style={{ marginTop: "var(--space-group-relaxed)" }}
            data-testid="settings-integrations-list"
          >
            {entries.map((entry) => (
              <li
                key={entry.id}
                className="lmchat-integration-item"
                data-testid={`settings-integrations-item-${entry.value}`}
              >
                <span className="lmchat-integration-value">{entry.value}</span>
                {entry.enabled_by_default === true && (
                  <span
                    className="lmchat-integration-badge"
                    data-testid={`settings-integrations-default-${entry.value}`}
                    title="Pre-selected on every new chat"
                  >
                    Default on
                  </span>
                )}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
