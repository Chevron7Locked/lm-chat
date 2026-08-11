/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for McpStoreSection (Settings → MCP Servers).
 *
 * Locked behaviours:
 *   - Catalog renders cards with name, trust badge, install button.
 *   - Install button on an entry with no required secrets fires POST immediately.
 *   - Install button on an entry WITH required secrets expands the secret form.
 *   - Secret validation fires before POST (missing required field blocks submit).
 *   - Installed servers list renders name, transport badge, connected dot.
 *   - Enable toggle fires PATCH {enabled}.
 *   - Delete with window.confirm=true fires DELETE; confirm=false skips it.
 *   - Tool policy expand shows tool list fetched from GET {slug}/tools.
 *   - Unchecking a tool adds it to the pending denylist; Save PATCH fires.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import {
  render,
  screen,
  fireEvent,
  waitFor,
  cleanup,
} from "@testing-library/react";
import { createElement } from "react";

// ─── Mock toast ──────────────────────────────────────────────────────────────

const mockPush = vi.fn();
vi.mock("@/stores/toastStore", () => ({
  useToast: () => ({ push: mockPush }),
  useToastStore: { getState: () => ({ push: mockPush }) },
}));

// ─── Hoisted mutable state for hook mocks ─────────────────────────────────────

const mockInstallMutate = vi.fn();
const mockPatchMutate = vi.fn();
const mockDeleteMutate = vi.fn();

const mockState = vi.hoisted(() => ({
  catalog: [] as {
    id: string;
    name: string;
    description: string;
    transport: string;
    secrets: { key: string; label: string; required: boolean }[];
    source: string;
    trust: string;
  }[],
  servers: [] as {
    id: string;
    slug: string;
    name: string;
    transport: string;
    command: string | null;
    url: string | null;
    secrets_set: string[];
    enabled: boolean;
    source: string;
    trust: string;
    consented: boolean;
    connected: boolean;
    tool_policy: string[];
  }[],
  serverTools: null as {
    slug: string;
    connected: boolean;
    tools: { name: string; description: string; denied: boolean }[];
    error?: string | null;
  } | null,
  toolsLoading: false as boolean,
}));

vi.mock("@/hooks/useMcpStore", () => ({
  useMcpCatalog: () => ({
    data: mockState.catalog,
    isLoading: false,
    isError: false,
  }),
  useMcpServers: () => ({
    data: mockState.servers,
    isLoading: false,
    isError: false,
  }),
  useMcpServerTools: (_slug: string, enabled: boolean) => ({
    data: enabled ? mockState.serverTools ?? undefined : undefined,
    isLoading: mockState.toolsLoading,
    isError: false,
  }),
  useInstallMcpServer: () => ({
    mutate: mockInstallMutate,
    isPending: false,
  }),
  usePatchMcpServer: () => ({
    mutate: mockPatchMutate,
    isPending: false,
  }),
  useDeleteMcpServer: () => ({
    mutate: mockDeleteMutate,
    isPending: false,
  }),
}));

// ─── Fixtures ────────────────────────────────────────────────────────────────

const CATALOG_SIMPLE = {
  id: "firecrawl",
  name: "Firecrawl",
  description: "Web scraping MCP server",
  transport: "stdio",
  secrets: [],
  source: "catalog",
  trust: "verified",
};

const CATALOG_WITH_SECRETS = {
  id: "github-mcp",
  name: "GitHub MCP",
  description: "GitHub integration",
  transport: "stdio",
  secrets: [{ key: "GITHUB_TOKEN", label: "GitHub Token", required: true }],
  source: "catalog",
  trust: "verified",
};

const SERVER_CONNECTED = {
  id: "srv-1",
  slug: "firecrawl",
  name: "Firecrawl",
  transport: "stdio",
  command: "npx",
  url: null,
  secrets_set: [],
  enabled: true,
  source: "catalog",
  trust: "verified",
  consented: true,
  connected: true,
  tool_policy: [],
};

const SERVER_DISABLED = {
  ...SERVER_CONNECTED,
  slug: "github-mcp",
  name: "GitHub MCP",
  enabled: false,
  connected: false,
};

// ─── Suite ───────────────────────────────────────────────────────────────────

describe("McpStoreSection", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    mockPush.mockClear();
    mockInstallMutate.mockClear();
    mockPatchMutate.mockClear();
    mockDeleteMutate.mockClear();
    mockState.catalog = [];
    mockState.servers = [];
    mockState.serverTools = null;
    mockState.toolsLoading = false;
    cleanup();
  });

  async function freshSection() {
    vi.resetModules();
    const mod = await import("@/components/McpStoreSection");
    return mod.McpStoreSection;
  }

  // ── Catalog render ──────────────────────────────────────────────────────────

  it("catalog render — shows card per entry with name and install button", async () => {
    mockState.catalog = [CATALOG_SIMPLE];

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("settings-mcp-section")).toBeTruthy();
    });

    const card = screen.getByTestId("mcp-catalog-card-firecrawl");
    expect(card).toBeTruthy();
    expect(card.textContent).toContain("Firecrawl");
    expect(card.textContent).toContain("verified");

    expect(screen.getByTestId("mcp-catalog-install-firecrawl")).toBeTruthy();
  });

  it("catalog install — no-required-secrets → fires POST immediately on Install click", async () => {
    mockState.catalog = [CATALOG_SIMPLE];

    mockInstallMutate.mockImplementation((_body: unknown, opts: { onSuccess?: () => void } | undefined) => {
      opts?.onSuccess?.();
    });

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("mcp-catalog-install-firecrawl")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("mcp-catalog-install-firecrawl"));

    await waitFor(() => {
      expect(mockInstallMutate).toHaveBeenCalledWith(
        expect.objectContaining({ catalog_id: "firecrawl" }),
        expect.any(Object),
      );
    });

    expect(mockPush).toHaveBeenCalledWith(
      expect.objectContaining({ variant: "success" }),
    );
  });

  it("catalog install — required secrets → expands form on first click, NOT an immediate POST", async () => {
    mockState.catalog = [CATALOG_WITH_SECRETS];

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("mcp-catalog-install-github-mcp")).toBeTruthy();
    });

    // First click toggles form open — no POST yet
    fireEvent.click(screen.getByTestId("mcp-catalog-install-github-mcp"));

    await waitFor(() => {
      expect(screen.getByTestId("mcp-catalog-secrets-github-mcp")).toBeTruthy();
    });

    expect(mockInstallMutate).not.toHaveBeenCalled();

    // Secret input should be present
    expect(
      screen.getByTestId("mcp-catalog-secret-input-github-mcp-GITHUB_TOKEN"),
    ).toBeTruthy();
  });

  it("catalog install — secret validation blocks empty required field", async () => {
    mockState.catalog = [CATALOG_WITH_SECRETS];

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("mcp-catalog-install-github-mcp")).toBeTruthy();
    });

    // Open form
    fireEvent.click(screen.getByTestId("mcp-catalog-install-github-mcp"));

    await waitFor(() => {
      expect(screen.getByTestId("mcp-catalog-confirm-install-github-mcp")).toBeTruthy();
    });

    // Submit WITHOUT filling the required field
    fireEvent.click(screen.getByTestId("mcp-catalog-confirm-install-github-mcp"));

    await waitFor(() => {
      expect(screen.getByTestId("mcp-catalog-secret-error-github-mcp")).toBeTruthy();
    });

    expect(mockInstallMutate).not.toHaveBeenCalled();
  });

  it("catalog install POST — fires with secrets when form is filled and submitted", async () => {
    mockState.catalog = [CATALOG_WITH_SECRETS];

    mockInstallMutate.mockImplementation((_body: unknown, opts: { onSuccess?: () => void } | undefined) => {
      opts?.onSuccess?.();
    });

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("mcp-catalog-install-github-mcp")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("mcp-catalog-install-github-mcp"));

    await waitFor(() => {
      expect(screen.getByTestId("mcp-catalog-secret-input-github-mcp-GITHUB_TOKEN")).toBeTruthy();
    });

    fireEvent.change(
      screen.getByTestId("mcp-catalog-secret-input-github-mcp-GITHUB_TOKEN"),
      { target: { value: "ghp_test123" } },
    );

    fireEvent.click(screen.getByTestId("mcp-catalog-confirm-install-github-mcp"));

    await waitFor(() => {
      expect(mockInstallMutate).toHaveBeenCalledWith(
        expect.objectContaining({
          catalog_id: "github-mcp",
          secrets: { GITHUB_TOKEN: "ghp_test123" },
        }),
        expect.any(Object),
      );
    });
  });

  // ── Installed servers ───────────────────────────────────────────────────────

  it("installed list — renders server name, transport, connected dot", async () => {
    mockState.servers = [SERVER_CONNECTED];

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("mcp-server-row-firecrawl")).toBeTruthy();
    });

    const row = screen.getByTestId("mcp-server-row-firecrawl");
    expect(row.textContent).toContain("Firecrawl");
    expect(row.textContent).toContain("stdio");

    // Connected dot present
    expect(screen.getByTestId("mcp-server-dot-firecrawl")).toBeTruthy();
  });

  it("enable toggle — fires PATCH with {enabled: false} when toggled off", async () => {
    mockState.servers = [SERVER_CONNECTED]; // enabled: true

    mockPatchMutate.mockImplementation(
      (_vars: unknown, opts: { onSuccess?: () => void } | undefined) => {
        opts?.onSuccess?.();
      },
    );

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("mcp-server-enable-firecrawl")).toBeTruthy();
    });

    const toggle = screen.getByTestId(
      "mcp-server-enable-firecrawl",
    ) as HTMLInputElement;
    expect(toggle.checked).toBe(true);

    fireEvent.click(toggle);

    await waitFor(() => {
      expect(mockPatchMutate).toHaveBeenCalledWith(
        expect.objectContaining({
          slug: "firecrawl",
          body: expect.objectContaining({ enabled: false }),
        }),
        expect.any(Object),
      );
    });
  });

  it("delete — confirm=true fires DELETE; confirm=false skips it", async () => {
    mockState.servers = [SERVER_CONNECTED];

    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(true);

    mockDeleteMutate.mockImplementation(
      (_slug: unknown, opts: { onSuccess?: () => void } | undefined) => {
        opts?.onSuccess?.();
      },
    );

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("mcp-server-delete-firecrawl")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("mcp-server-delete-firecrawl"));

    expect(confirmSpy).toHaveBeenCalled();
    await waitFor(() => {
      expect(mockDeleteMutate).toHaveBeenCalledWith("firecrawl", expect.any(Object));
    });

    confirmSpy.mockRestore();
  });

  it("delete — confirm=false does NOT call delete mutate", async () => {
    mockState.servers = [SERVER_CONNECTED];

    const confirmSpy = vi.spyOn(window, "confirm").mockReturnValue(false);

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("mcp-server-delete-firecrawl")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("mcp-server-delete-firecrawl"));

    expect(confirmSpy).toHaveBeenCalled();
    expect(mockDeleteMutate).not.toHaveBeenCalled();

    confirmSpy.mockRestore();
  });

  // ── Tool policy panel ───────────────────────────────────────────────────────

  it("tool policy — expand shows tool list; unchecking tool fires PATCH with denylist", async () => {
    mockState.servers = [SERVER_CONNECTED];
    mockState.serverTools = {
      slug: "firecrawl",
      connected: true,
      tools: [
        { name: "firecrawl_scrape", description: "Scrape a URL", denied: false },
        { name: "firecrawl_search", description: "Search the web", denied: false },
      ],
    };

    mockPatchMutate.mockImplementation(
      (_vars: unknown, opts: { onSuccess?: () => void } | undefined) => {
        opts?.onSuccess?.();
      },
    );

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("mcp-server-expand-firecrawl")).toBeTruthy();
    });

    // Expand the tool panel
    fireEvent.click(screen.getByTestId("mcp-server-expand-firecrawl"));

    await waitFor(() => {
      expect(screen.getByTestId("mcp-tools-panel-firecrawl")).toBeTruthy();
    });

    // Both tools should be checked (allowed = not in denylist)
    const scrapeCheckbox = screen.getByTestId(
      "mcp-tool-checkbox-firecrawl-firecrawl_scrape",
    ) as HTMLInputElement;
    expect(scrapeCheckbox.checked).toBe(true);

    // Uncheck firecrawl_scrape → add it to pending denylist
    fireEvent.click(scrapeCheckbox);

    // The "Save tool policy" button should appear (there are now changes)
    await waitFor(() => {
      expect(screen.getByTestId("mcp-tools-save-firecrawl")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("mcp-tools-save-firecrawl"));

    await waitFor(() => {
      expect(mockPatchMutate).toHaveBeenCalledWith(
        expect.objectContaining({
          slug: "firecrawl",
          body: expect.objectContaining({
            tool_policy: expect.arrayContaining(["firecrawl_scrape"]),
          }),
        }),
        expect.any(Object),
      );
    });
  });

  it("installed servers empty state — renders empty description", async () => {
    mockState.servers = [];

    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("mcp-servers-empty")).toBeTruthy();
    });
  });

  // ── Scope note ──────────────────────────────────────────────────────────────

  it("scope note — renders with testid and mentions cloud and LM Studio", async () => {
    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("mcp-store-scope-note")).toBeTruthy();
    });

    const note = screen.getByTestId("mcp-store-scope-note");
    expect(note.textContent).toMatch(/cloud/i);
    expect(note.textContent).toMatch(/LM Studio/i);
  });

  it("custom server form — validation blocks empty slug", async () => {
    const Section = await freshSection();
    render(createElement(Section));

    await waitFor(() => {
      expect(screen.getByTestId("mcp-add-custom-btn")).toBeTruthy();
    });

    fireEvent.click(screen.getByTestId("mcp-add-custom-btn"));

    await waitFor(() => {
      expect(screen.getByTestId("mcp-custom-form")).toBeTruthy();
    });

    // Leave slug blank, click save
    fireEvent.click(screen.getByTestId("mcp-custom-save"));

    await waitFor(() => {
      expect(screen.getByTestId("mcp-custom-error")).toBeTruthy();
    });

    expect(mockInstallMutate).not.toHaveBeenCalled();
  });
});
