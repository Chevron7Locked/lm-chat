/* SPDX-License-Identifier: Apache-2.0 */
/**
 * formatToolName — maps raw MCP/function names to human-friendly labels.
 * Ported from v0.5.x app.js:3683–3700 (toolLabel function).
 * Falls back to a humanized version: strips `mcp/` server prefixes, replaces
 * underscores/hyphens with spaces, applies Title Case for unknown tools.
 *
 * Extracted from ChatMessage.tsx so both ChatMessage and ProcessStream can
 * share one source of truth for tool-name formatting.
 */

const FRIENDLY_TOOL_NAMES: Record<string, string> = {
  // Web / search
  brave_web_search: "Search the web",
  brave_local_search: "Search locally",
  web_search: "Search the web",
  // Memory
  memory_store: "Store a memory",
  memory_retrieve: "Retrieve memories",
  memory_search: "Search memories",
  // Reasoning
  sequential_thinking: "Reason step by step",
  // Docs / library
  resolve_library_id: "Look up docs",
  get_library_docs: "Retrieve docs",
  // Research
  search_papers: "Search papers",
  search_arxiv: "Search arXiv",
  search_semantic_scholar: "Search papers",
  search_google_scholar: "Search papers",
  // Filesystem (MCP)
  read_file: "Read file",
  read_text_file: "Read file",
  write_file: "Write file",
  create_file: "Create file",
  edit_file: "Edit file",
  list_directory: "List directory",
  search_files: "Search files",
  // Browser / Playwright
  browser_navigate: "Navigate",
  browser_click: "Click",
  browser_type: "Type",
  browser_fill: "Fill form",
  browser_fill_form: "Fill form",
  browser_take_screenshot: "Take screenshot",
  browser_snapshot: "Snapshot",
  browser_evaluate: "Run script",
  browser_wait_for: "Wait",
  browser_hover: "Hover",
  browser_select_option: "Select option",
  browser_press_key: "Press key",
  browser_network_request: "Network request",
  // Generic
  run_terminal_cmd: "Run command",
  execute_command: "Run command",
};

export function formatToolName(raw: string): string {
  if (!raw) return "Used a tool";
  // Exact match in the friendly map
  if (FRIENDLY_TOOL_NAMES[raw] !== undefined) return FRIENDLY_TOOL_NAMES[raw];
  // Strip "mcp/" server prefix (e.g. "mcp/context7/get_docs" → "get_docs")
  const stripped = raw.replace(/^mcp\/[^/]+\//, "").replace(/^mcp\//, "");
  // Check the stripped name in the map too
  if (FRIENDLY_TOOL_NAMES[stripped] !== undefined)
    return FRIENDLY_TOOL_NAMES[stripped];
  // Humanize: replace _ and - with spaces, then Title Case
  const humanized = stripped
    .replace(/[_-]/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
  return humanized || "Used a tool";
}
