/* SPDX-License-Identifier: Apache-2.0 */
/**
 * McpStoreSection — Settings → MCP Servers tab.
 *
 * Three areas:
 *  1. Browse catalog  — GET /api/mcp-store/catalog → card per entry; Install
 *     button; required-secret inputs before POST /api/mcp-store/servers.
 *  2. Installed servers — GET /api/mcp-store/servers → row per server:
 *     enable toggle (PATCH {enabled}), expandable per-tool allow/deny panel
 *     (GET {slug}/tools → PATCH {tool_policy}), delete with confirm.
 *  3. Add custom (BYO) — small form for slug/name/transport + command/args OR
 *     url → POST /api/mcp-store/servers (no catalog_id).
 *
 * Structure, data-fetching, styling, and testid conventions mirror
 * ProvidersSection.tsx — read that file first if you need the reference.
 *
 * Standards (non-negotiable per AGENTS.md):
 * - Dark mode default; redesign tokens only; no hard-coded widths.
 * - Full WCAG AA focus rings + labels; no emoji; lucide-react icon set.
 * - Modern responsive CSS (gap, logical properties).
 * - Stable data-testids on all interactive elements.
 */
import { useState, type JSX } from "react";
import { Plus, Trash2, ChevronDown, ChevronRight, Check } from "lucide-react";
import { useToast } from "@/stores/toastStore";
import {
  useMcpCatalog,
  useMcpServers,
  useMcpServerTools,
  useInstallMcpServer,
  usePatchMcpServer,
  useDeleteMcpServer,
  type CatalogEntry,
  type McpServer,
} from "@/hooks/useMcpStore";
import "@/styles/settings.css";

// ─── Helpers ─────────────────────────────────────────────────────────────────

function trustBadgeClass(trust: string): string {
  switch (trust) {
    case "curated": return "lmchat-mcp-badge lmchat-mcp-badge--verified";
    case "byo": return "lmchat-mcp-badge lmchat-mcp-badge--custom";
    default: return "lmchat-mcp-badge";
  }
}

function trustLabel(trust: string): string {
  switch (trust) {
    case "curated": return "Curated";
    case "byo": return "BYO";
    default: return trust;
  }
}

function transportLabel(transport: string): string {
  switch (transport) {
    case "stdio": return "stdio";
    case "sse": return "SSE";
    case "streamable_http": return "HTTP";
    default: return transport;
  }
}

/** First line of a (possibly multi-line, stderr-tail-bearing) error detail,
 *  truncated so a crashed server's stack/log doesn't blow out the row. */
function firstErrorLine(text: string, maxLength = 140): string {
  const line = text.split("\n")[0]?.trim() ?? "";
  return line.length > maxLength ? `${line.slice(0, maxLength - 1)}…` : line;
}

// ─── Area 1: Browse catalog ───────────────────────────────────────────────────

interface CatalogCardProps {
  entry: CatalogEntry;
  isInstalling: boolean;
  onInstall: (entry: CatalogEntry, secrets: Record<string, string>) => void;
}

function CatalogCard({ entry, isInstalling, onInstall }: CatalogCardProps): JSX.Element {
  const hasRequiredSecrets = entry.secrets.some((s) => s.required);
  const [expanded, setExpanded] = useState<boolean>(false);
  const [secrets, setSecrets] = useState<Record<string, string>>(() =>
    Object.fromEntries(entry.secrets.map((s) => [s.key, ""])),
  );
  const [validationError, setValidationError] = useState<string | null>(null);

  function handleInstallClick(): void {
    setValidationError(null);
    if (hasRequiredSecrets) {
      // Toggle form visibility before first expand
      setExpanded((prev) => !prev);
      return;
    }
    onInstall(entry, {});
  }

  function handleInstallSubmit(): void {
    setValidationError(null);
    for (const s of entry.secrets) {
      if (s.required && !secrets[s.key]?.trim()) {
        setValidationError(`"${s.label}" is required.`);
        return;
      }
    }
    onInstall(entry, secrets);
  }

  return (
    <div
      className="lmchat-mcp-card"
      data-testid={`mcp-catalog-card-${entry.id}`}
    >
      <div className="lmchat-mcp-card__header">
        <div className="lmchat-mcp-card__meta">
          <span className="lmchat-mcp-card__name">{entry.name}</span>
          <span className={trustBadgeClass(entry.trust)}>{trustLabel(entry.trust)}</span>
          <span className="lmchat-mcp-badge lmchat-mcp-badge--transport">
            {transportLabel(entry.transport)}
          </span>
        </div>
        <button
          type="button"
          /* /quieter: browse-Install is a secondary discovery action — one
             copper primary per card (×N) gave the catalog no entry point and
             vibrated on the dark surface. The copper fill is reserved for the
             single confirmed action ("Confirm install", below). */
          className="lmchat-btn-secondary"
          onClick={handleInstallClick}
          disabled={isInstalling}
          aria-label={`Install ${entry.name}`}
          aria-expanded={hasRequiredSecrets ? expanded : undefined}
          data-testid={`mcp-catalog-install-${entry.id}`}
        >
          {isInstalling ? "Installing…" : "Install"}
        </button>
      </div>

      <p className="lmchat-mcp-card__desc">{entry.description}</p>

      {/* Secret inputs — shown when the entry requires secrets and the user clicked Install */}
      {expanded && entry.secrets.length > 0 && (
        <div className="lmchat-mcp-secrets" data-testid={`mcp-catalog-secrets-${entry.id}`}>
          {entry.secrets.map((secret) => (
            <div key={secret.key} className="lmchat-field">
              <label
                htmlFor={`mcp-secret-${entry.id}-${secret.key}`}
                className="lmchat-field-label"
              >
                {secret.label}
                {secret.required && (
                  <span className="lmchat-mcp-required" aria-hidden="true"> *</span>
                )}
              </label>
              <input
                id={`mcp-secret-${entry.id}-${secret.key}`}
                type="password"
                className="lmchat-input"
                value={secrets[secret.key] ?? ""}
                onChange={(e) => {
                  setSecrets((prev) => ({ ...prev, [secret.key]: e.target.value }));
                }}
                autoComplete="off"
                spellCheck={false}
                aria-required={secret.required}
                data-testid={`mcp-catalog-secret-input-${entry.id}-${secret.key}`}
              />
            </div>
          ))}

          {validationError !== null && (
            <p role="alert" className="lmchat-form-error" data-testid={`mcp-catalog-secret-error-${entry.id}`}>
              {validationError}
            </p>
          )}

          <div className="lmchat-form-actions">
            <button
              type="button"
              className="lmchat-btn-primary"
              onClick={handleInstallSubmit}
              disabled={isInstalling}
              data-testid={`mcp-catalog-confirm-install-${entry.id}`}
            >
              {isInstalling ? "Installing…" : "Confirm install"}
            </button>
            <button
              type="button"
              className="lmchat-btn-secondary"
              onClick={() => { setExpanded(false); }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ─── Area 2: Tool allow/deny panel (per-server, expandable) ──────────────────

interface ToolPolicyPanelProps {
  slug: string;
  currentDenylist: string[];
  onSave: (denylist: string[]) => void;
  isSaving: boolean;
}

function ToolPolicyPanel({
  slug,
  currentDenylist,
  onSave,
  isSaving,
}: ToolPolicyPanelProps): JSX.Element {
  const { data: toolsData, isLoading, isError } = useMcpServerTools(slug, true);

  // Local pending denylist — starts from current server state
  const [pendingDenylist, setPendingDenylist] = useState<string[]>(currentDenylist);

  function toggleTool(name: string, currentlyDenied: boolean): void {
    if (currentlyDenied) {
      // Remove from denylist → tool becomes allowed
      setPendingDenylist((prev) => prev.filter((n) => n !== name));
    } else {
      // Add to denylist → tool becomes denied
      setPendingDenylist((prev) => [...prev, name]);
    }
  }

  if (isLoading) {
    return (
      <p className="lmchat-section-description" data-testid={`mcp-tools-loading-${slug}`}>
        Loading tools…
      </p>
    );
  }

  if (isError || toolsData === undefined) {
    return (
      <p className="lmchat-form-error" role="alert" data-testid={`mcp-tools-error-${slug}`}>
        Could not load tools.
      </p>
    );
  }

  if (!toolsData.connected) {
    return (
      <p className="lmchat-form-error" role="alert" data-testid={`mcp-tools-disconnected-${slug}`}>
        Server not connected{toolsData.error != null ? `: ${toolsData.error}` : "."}
      </p>
    );
  }

  if (toolsData.tools.length === 0) {
    return (
      <p className="lmchat-section-description" data-testid={`mcp-tools-empty-${slug}`}>
        No tools advertised by this server.
      </p>
    );
  }

  const denySet = new Set(pendingDenylist);
  const hasChanges =
    JSON.stringify([...pendingDenylist].sort()) !==
    JSON.stringify([...currentDenylist].sort());

  return (
    <div className="lmchat-mcp-tools" data-testid={`mcp-tools-panel-${slug}`}>
      <ul className="lmchat-mcp-tools__list" role="list" aria-label="Server tools">
        {toolsData.tools.map((tool) => {
          const isDenied = denySet.has(tool.name);
          return (
            <li key={tool.name} className="lmchat-mcp-tools__item">
              <label className="lmchat-mcp-tools__label">
                <input
                  type="checkbox"
                  className="lmchat-mcp-tools__checkbox"
                  checked={!isDenied}
                  onChange={() => { toggleTool(tool.name, isDenied); }}
                  aria-label={`Allow tool ${tool.name}`}
                  data-testid={`mcp-tool-checkbox-${slug}-${tool.name}`}
                />
                <span className="lmchat-mcp-tools__name">{tool.name}</span>
                {tool.description && (
                  <span className="lmchat-mcp-tools__desc">{tool.description}</span>
                )}
              </label>
            </li>
          );
        })}
      </ul>

      {hasChanges && (
        <div className="lmchat-form-actions" style={{ marginBlockStart: "var(--space-sibling)" }}>
          <button
            type="button"
            className="lmchat-btn-primary"
            onClick={() => { onSave(pendingDenylist); }}
            disabled={isSaving}
            data-testid={`mcp-tools-save-${slug}`}
          >
            {isSaving ? "Saving…" : "Save tool policy"}
          </button>
          <button
            type="button"
            className="lmchat-btn-secondary"
            onClick={() => { setPendingDenylist(currentDenylist); }}
          >
            Discard
          </button>
        </div>
      )}
    </div>
  );
}

// ─── Area 2: Installed server row ────────────────────────────────────────────

interface InstalledServerRowProps {
  server: McpServer;
  onEnabledToggle: (slug: string, enabled: boolean) => void;
  onDelete: (slug: string) => void;
  onToolPolicySave: (slug: string, denylist: string[]) => void;
  isPatching: boolean;
  isDeleting: boolean;
}

function InstalledServerRow({
  server,
  onEnabledToggle,
  onDelete,
  onToolPolicySave,
  isPatching,
  isDeleting,
}: InstalledServerRowProps): JSX.Element {
  const [expanded, setExpanded] = useState<boolean>(false);

  return (
    <div className="lmchat-mcp-server-row" data-testid={`mcp-server-row-${server.slug}`}>
      <div className="lmchat-mcp-server-row__summary">
        {/* Name + transport + status dot */}
        <div className="lmchat-mcp-server-row__identity">
          <span
            className={`lmchat-mcp-dot lmchat-mcp-dot--${server.connected ? "ok" : "err"}`}
            aria-label={server.connected ? "Connected" : "Not connected"}
            data-testid={`mcp-server-dot-${server.slug}`}
          />
          <span className="lmchat-mcp-server-row__name">{server.name}</span>
          <span className="lmchat-mcp-badge lmchat-mcp-badge--transport">
            {transportLabel(server.transport)}
          </span>
          {/* Trust badge for every installed server: "Curated" for catalog
              installs, "BYO" for bring-your-own. (The old `source !== "custom"`
              guard was dead — the backend emits source "official"/"byo", never
              "custom".) */}
          <span className={trustBadgeClass(server.trust)}>{trustLabel(server.trust)}</span>
        </div>

        {/* Error display */}
        {!server.connected && (
          <span
            className="lmchat-mcp-server-row__error"
            data-testid={`mcp-server-error-${server.slug}`}
          >
            Not connected
          </span>
        )}

        {/* Actions */}
        <div className="lmchat-mcp-server-row__actions">
          {/* Enable toggle */}
          <label className="lmchat-mcp-toggle" aria-label={`${server.enabled ? "Disable" : "Enable"} ${server.name}`}>
            <input
              type="checkbox"
              className="lmchat-mcp-toggle__input"
              checked={server.enabled}
              onChange={(e) => { onEnabledToggle(server.slug, e.target.checked); }}
              disabled={isPatching}
              data-testid={`mcp-server-enable-${server.slug}`}
            />
            <span className="lmchat-mcp-toggle__track" aria-hidden="true" />
            <span className="lmchat-sr-only">{server.enabled ? "Enabled" : "Disabled"}</span>
          </label>

          {/* Expand tool panel button */}
          <button
            type="button"
            className="lmchat-btn-secondary lmchat-mcp-server-row__expand-btn"
            onClick={() => { setExpanded((prev) => !prev); }}
            aria-expanded={expanded}
            aria-label={`${expanded ? "Collapse" : "Expand"} tools for ${server.name}`}
            data-testid={`mcp-server-expand-${server.slug}`}
          >
            {expanded ? (
              <ChevronDown size={14} aria-hidden />
            ) : (
              <ChevronRight size={14} aria-hidden />
            )}
            <span>Tools</span>
          </button>

          {/* Delete */}
          <button
            type="button"
            className="lmchat-btn-secondary lmchat-mcp-server-row__delete-btn"
            onClick={() => { onDelete(server.slug); }}
            disabled={isDeleting}
            aria-label={`Delete ${server.name}`}
            data-testid={`mcp-server-delete-${server.slug}`}
          >
            <Trash2 size={14} aria-hidden />
          </button>
        </div>
      </div>

      {/* Real reason the last connect attempt failed, when known — beneath
          the generic "Not connected" dot/label, small and non-alarming. */}
      {!server.connected && server.last_error != null && (
        <p
          className="lmchat-mcp-server-row__error-detail"
          title={server.last_error}
          data-testid={`mcp-server-error-detail-${server.slug}`}
        >
          {firstErrorLine(server.last_error)}
        </p>
      )}

      {/* Expandable tool policy panel */}
      {expanded && (
        <div className="lmchat-mcp-server-row__tools">
          <ToolPolicyPanel
            slug={server.slug}
            currentDenylist={server.tool_policy}
            onSave={(denylist) => { onToolPolicySave(server.slug, denylist); }}
            isSaving={isPatching}
          />
        </div>
      )}
    </div>
  );
}

// ─── Area 3: Add custom BYO form ─────────────────────────────────────────────

interface CustomServerFormProps {
  onClose: () => void;
  onSaved: () => void;
}

function CustomServerForm({ onClose, onSaved }: CustomServerFormProps): JSX.Element {
  const { push } = useToast();
  const installMutation = useInstallMcpServer();

  const [slug, setSlug] = useState<string>("");
  const [name, setName] = useState<string>("");
  const [transport, setTransport] = useState<string>("stdio");
  const [command, setCommand] = useState<string>("");
  const [args, setArgs] = useState<string>("");
  const [url, setUrl] = useState<string>("");
  const [secretsRaw, setSecretsRaw] = useState<string>("");
  const [formError, setFormError] = useState<string | null>(null);

  const isHttpTransport = transport === "sse" || transport === "streamable_http";

  function handleSave(): void {
    setFormError(null);

    if (!slug.trim()) {
      setFormError("Slug is required.");
      return;
    }
    if (!name.trim()) {
      setFormError("Name is required.");
      return;
    }
    if (isHttpTransport && !url.trim()) {
      setFormError("URL is required for HTTP/SSE transport.");
      return;
    }
    if (!isHttpTransport && !command.trim()) {
      setFormError("Command is required for stdio transport.");
      return;
    }

    // Parse secrets — blank is valid (no secrets)
    let parsedSecrets: Record<string, string> | undefined;
    if (secretsRaw.trim()) {
      try {
        parsedSecrets = JSON.parse(secretsRaw) as Record<string, string>;
      } catch {
        setFormError("Secrets must be valid JSON, e.g. {\"API_KEY\": \"value\"}");
        return;
      }
    }

    const body = {
      slug: slug.trim(),
      name: name.trim(),
      transport,
      ...(isHttpTransport ? { url: url.trim() } : {
        command: command.trim(),
        args: args.trim() ? args.trim().split(/\s+/) : [],
      }),
      ...(parsedSecrets !== undefined ? { secrets: parsedSecrets } : {}),
    };

    installMutation.mutate(body, {
      onSuccess: () => {
        push({ variant: "success", message: `"${name.trim()}" installed.` });
        onSaved();
      },
      onError: (err) => {
        setFormError(err.detail ?? "Install failed.");
      },
    });
  }

  return (
    <div className="lmchat-form" data-testid="mcp-custom-form">
      <div className="lmchat-field">
        <label htmlFor="mcp-custom-slug" className="lmchat-field-label">
          Slug
        </label>
        <input
          id="mcp-custom-slug"
          type="text"
          className="lmchat-input"
          value={slug}
          onChange={(e) => { setSlug(e.target.value); }}
          placeholder="my-mcp-server"
          autoComplete="off"
          spellCheck={false}
          data-testid="mcp-custom-slug"
        />
      </div>

      <div className="lmchat-field">
        <label htmlFor="mcp-custom-name" className="lmchat-field-label">
          Display name
        </label>
        <input
          id="mcp-custom-name"
          type="text"
          className="lmchat-input"
          value={name}
          onChange={(e) => { setName(e.target.value); }}
          placeholder="My MCP Server"
          autoComplete="off"
          spellCheck={false}
          data-testid="mcp-custom-name"
        />
      </div>

      <div className="lmchat-field">
        <label htmlFor="mcp-custom-transport" className="lmchat-field-label">
          Transport
        </label>
        <select
          id="mcp-custom-transport"
          className="lmchat-input"
          value={transport}
          onChange={(e) => { setTransport(e.target.value); }}
          data-testid="mcp-custom-transport"
        >
          <option value="stdio">stdio</option>
          <option value="sse">SSE</option>
          <option value="streamable_http">Streamable HTTP</option>
        </select>
      </div>

      {isHttpTransport ? (
        <div className="lmchat-field">
          <label htmlFor="mcp-custom-url" className="lmchat-field-label">
            URL
          </label>
          <input
            id="mcp-custom-url"
            type="url"
            className="lmchat-input"
            value={url}
            onChange={(e) => { setUrl(e.target.value); }}
            placeholder="https://mcp.example.com/sse"
            autoComplete="off"
            spellCheck={false}
            data-testid="mcp-custom-url"
          />
        </div>
      ) : (
        <>
          <div className="lmchat-field">
            <label htmlFor="mcp-custom-command" className="lmchat-field-label">
              Command
            </label>
            <input
              id="mcp-custom-command"
              type="text"
              className="lmchat-input"
              value={command}
              onChange={(e) => { setCommand(e.target.value); }}
              placeholder="npx"
              autoComplete="off"
              spellCheck={false}
              data-testid="mcp-custom-command"
            />
          </div>

          <div className="lmchat-field">
            <label htmlFor="mcp-custom-args" className="lmchat-field-label">
              Arguments (space-separated)
            </label>
            <input
              id="mcp-custom-args"
              type="text"
              className="lmchat-input"
              value={args}
              onChange={(e) => { setArgs(e.target.value); }}
              placeholder="-y @modelcontextprotocol/server-github"
              autoComplete="off"
              spellCheck={false}
              data-testid="mcp-custom-args"
            />
          </div>
        </>
      )}

      <div className="lmchat-field">
        <label htmlFor="mcp-custom-secrets" className="lmchat-field-label">
          Secrets (JSON, optional)
        </label>
        <input
          id="mcp-custom-secrets"
          type="text"
          className="lmchat-input"
          value={secretsRaw}
          onChange={(e) => { setSecretsRaw(e.target.value); }}
          placeholder='{"GITHUB_TOKEN": "ghp_…"}'
          autoComplete="off"
          spellCheck={false}
          data-testid="mcp-custom-secrets"
        />
        <p className="lmchat-field-hint">
          Stored server-side; values never returned to the browser.
        </p>
      </div>

      {formError !== null && (
        <p role="alert" className="lmchat-form-error" data-testid="mcp-custom-error">
          {formError}
        </p>
      )}

      <div className="lmchat-form-actions">
        <button
          type="button"
          className="lmchat-btn-primary"
          onClick={handleSave}
          disabled={installMutation.isPending}
          data-testid="mcp-custom-save"
        >
          {installMutation.isPending ? "Installing…" : "Install server"}
        </button>
        <button
          type="button"
          className="lmchat-btn-secondary"
          onClick={onClose}
          data-testid="mcp-custom-cancel"
        >
          Cancel
        </button>
      </div>
    </div>
  );
}

// ─── Main section ─────────────────────────────────────────────────────────────

export function McpStoreSection(): JSX.Element {
  const { push } = useToast();

  // Catalog
  const { data: catalog, isLoading: catalogLoading, isError: catalogError } = useMcpCatalog();

  // Installed servers
  const { data: servers, isLoading: serversLoading, isError: serversError } = useMcpServers();

  // Mutations
  const installMutation = useInstallMcpServer();
  const patchMutation = usePatchMcpServer();
  const deleteMutation = useDeleteMcpServer();

  // Area 3 form visibility
  const [showCustomForm, setShowCustomForm] = useState<boolean>(false);

  // Track which install is in flight (by catalog_id)
  const [installingId, setInstallingId] = useState<string | null>(null);

  function handleCatalogInstall(entry: CatalogEntry, secrets: Record<string, string>): void {
    setInstallingId(entry.id);
    installMutation.mutate(
      Object.keys(secrets).length > 0
        ? { catalog_id: entry.id, secrets }
        : { catalog_id: entry.id },
      {
        onSuccess: () => {
          push({ variant: "success", message: `"${entry.name}" installed.` });
          setInstallingId(null);
        },
        onError: (err) => {
          push({ variant: "error", message: err.detail ?? "Install failed." });
          setInstallingId(null);
        },
      },
    );
  }

  function handleEnabledToggle(slug: string, enabled: boolean): void {
    patchMutation.mutate(
      { slug, body: { enabled } },
      {
        onError: (err) => {
          push({ variant: "error", message: err.detail ?? "Failed to update server." });
        },
      },
    );
  }

  function handleToolPolicySave(slug: string, denylist: string[]): void {
    patchMutation.mutate(
      { slug, body: { tool_policy: denylist } },
      {
        onSuccess: () => {
          push({ variant: "success", message: "Tool policy saved." });
        },
        onError: (err) => {
          push({ variant: "error", message: err.detail ?? "Failed to save tool policy." });
        },
      },
    );
  }

  function handleDelete(slug: string): void {
    const server = servers?.find((s) => s.slug === slug);
    const displayName = server?.name ?? slug;
    if (!window.confirm(`Delete "${displayName}"? This cannot be undone.`)) return;

    deleteMutation.mutate(slug, {
      onSuccess: () => {
        push({ variant: "success", message: `"${displayName}" removed.` });
      },
      onError: (err) => {
        push({ variant: "error", message: err.detail ?? "Delete failed." });
      },
    });
  }

  // Build a Set of already-installed catalog IDs for quick lookup
  const installedIds = new Set(
    servers?.filter((s) => s.source !== "custom").map((s) => s.slug) ?? [],
  );

  return (
    <div className="lmchat-section-container" data-testid="settings-mcp-section">

      {/* ── Scope note ─────────────────────────────────────────────────────── */}
      <p className="lmchat-section-description" data-testid="mcp-store-scope-note">
        Servers installed here run for cloud providers only. Local LM Studio
        models use the MCP servers you set up in LM Studio itself — those appear
        automatically as tools and don't need installing here.
      </p>

      {/* ── Area 2: Installed servers ───────────────────────────────────────── */}
      <section className="lmchat-mcp-area" aria-labelledby="mcp-installed-heading">
        <h2 id="mcp-installed-heading" className="lmchat-section-heading">
          Installed servers
        </h2>

        {serversLoading && (
          <p className="lmchat-section-description" data-testid="mcp-servers-loading">
            Loading servers…
          </p>
        )}
        {serversError && (
          <p className="lmchat-form-error" role="alert" data-testid="mcp-servers-error">
            Could not load servers — try refreshing.
          </p>
        )}
        {!serversLoading && !serversError && (
          <div data-testid="mcp-servers-list">
            {servers !== undefined && servers.length > 0 ? (
              servers.map((server) => (
                <InstalledServerRow
                  key={server.slug}
                  server={server}
                  onEnabledToggle={handleEnabledToggle}
                  onDelete={handleDelete}
                  onToolPolicySave={handleToolPolicySave}
                  isPatching={patchMutation.isPending}
                  isDeleting={deleteMutation.isPending}
                />
              ))
            ) : (
              <p className="lmchat-section-description" data-testid="mcp-servers-empty">
                No MCP servers installed. Browse the catalog below or add a custom server.
              </p>
            )}
          </div>
        )}

        {/* Add custom server */}
        {!showCustomForm ? (
          <div className="lmchat-form-actions" style={{ marginBlockStart: "var(--space-sibling)" }}>
            <button
              type="button"
              className="lmchat-btn-secondary"
              onClick={() => { setShowCustomForm(true); }}
              data-testid="mcp-add-custom-btn"
            >
              <Plus size={14} aria-hidden />
              Add custom server
            </button>
          </div>
        ) : (
          <div style={{ marginBlockStart: "var(--space-sibling)" }}>
            <h3 className="lmchat-mcp-subheading">Add custom server</h3>
            <CustomServerForm
              onClose={() => { setShowCustomForm(false); }}
              onSaved={() => { setShowCustomForm(false); }}
            />
          </div>
        )}
      </section>

      {/* ── Divider ─────────────────────────────────────────────────────────── */}
      <hr className="lmchat-mcp-divider" aria-hidden="true" />

      {/* ── Area 1: Browse catalog ──────────────────────────────────────────── */}
      <section className="lmchat-mcp-area" aria-labelledby="mcp-catalog-heading">
        <h2 id="mcp-catalog-heading" className="lmchat-section-heading">
          Browse catalog
        </h2>

        {catalogLoading && (
          <p className="lmchat-section-description" data-testid="mcp-catalog-loading">
            Loading catalog…
          </p>
        )}
        {catalogError && (
          <p className="lmchat-form-error" role="alert" data-testid="mcp-catalog-error">
            Could not load catalog — try refreshing.
          </p>
        )}
        {!catalogLoading && !catalogError && (
          <div className="lmchat-mcp-catalog" data-testid="mcp-catalog-list">
            {catalog !== undefined && catalog.length > 0 ? (
              catalog.map((entry) => {
                const isAlreadyInstalled = installedIds.has(entry.id);
                return isAlreadyInstalled ? (
                  <div
                    key={entry.id}
                    className="lmchat-mcp-card lmchat-mcp-card--installed"
                    data-testid={`mcp-catalog-card-${entry.id}`}
                  >
                    <div className="lmchat-mcp-card__header">
                      <div className="lmchat-mcp-card__meta">
                        <span className="lmchat-mcp-card__name">{entry.name}</span>
                        <span className={trustBadgeClass(entry.trust)}>{trustLabel(entry.trust)}</span>
                      </div>
                      <span className="lmchat-mcp-badge lmchat-mcp-badge--installed">
                        <Check size={11} aria-hidden />
                        Installed
                      </span>
                    </div>
                    <p className="lmchat-mcp-card__desc">{entry.description}</p>
                  </div>
                ) : (
                  <CatalogCard
                    key={entry.id}
                    entry={entry}
                    isInstalling={installingId === entry.id}
                    onInstall={handleCatalogInstall}
                  />
                );
              })
            ) : (
              <p className="lmchat-section-description" data-testid="mcp-catalog-empty">
                No catalog entries available.
              </p>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
