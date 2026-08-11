/**
 * Unit tests for slash command parsing and autocomplete.
 */
import { describe, it, expect } from "vitest";
import { filterCommands, parseSlashCommand, BUILTIN_COMMANDS } from "@/components/SlashMenu";

describe("filterCommands", () => {
  it("returns all commands for empty query", () => {
    const result = filterCommands("");
    expect(result).toHaveLength(BUILTIN_COMMANDS.length);
  });

  it("filters by prefix match on name", () => {
    const result = filterCommands("cl");
    expect(result.some((c) => c.name === "clear")).toBe(true);
    expect(result.every((c) => c.name.startsWith("cl"))).toBe(true);
  });

  it("exact match returns single command", () => {
    const result = filterCommands("help");
    expect(result).toHaveLength(1);
    expect(result[0]?.name).toBe("help");
  });

  it("no match returns empty array", () => {
    const result = filterCommands("zzz");
    expect(result).toHaveLength(0);
  });

  it("filter is case-insensitive", () => {
    const result = filterCommands("MEM");
    expect(result.some((c) => c.name === "memory")).toBe(true);
  });

  it("returns all commands when query is whitespace", () => {
    const result = filterCommands("   ");
    expect(result).toHaveLength(BUILTIN_COMMANDS.length);
  });
});

describe("parseSlashCommand", () => {
  it("returns null for non-slash input", () => {
    expect(parseSlashCommand("hello world")).toBeNull();
    expect(parseSlashCommand("")).toBeNull();
    expect(parseSlashCommand("   ")).toBeNull();
  });

  it("parses /help with no args", () => {
    const result = parseSlashCommand("/help");
    expect(result).toEqual({ name: "help", args: "" });
  });

  it("parses /memory with args", () => {
    const result = parseSlashCommand("/memory important context here");
    expect(result).toEqual({ name: "memory", args: "important context here" });
  });

  it("normalises command name to lowercase", () => {
    const result = parseSlashCommand("/CLEAR");
    expect(result?.name).toBe("clear");
  });

  it("parses leading whitespace correctly", () => {
    const result = parseSlashCommand("  /fork");
    expect(result?.name).toBe("fork");
  });

  it("command with no args has empty args string", () => {
    const result = parseSlashCommand("/compact");
    expect(result?.args).toBe("");
  });

  it("args preserves spaces", () => {
    const result = parseSlashCommand("/memory hello   world");
    expect(result?.args).toBe("hello   world");
  });
});

describe("BUILTIN_COMMANDS", () => {
  it("includes required commands from spec", () => {
    const names = BUILTIN_COMMANDS.map((c) => c.name);
    expect(names).toContain("help");
    expect(names).toContain("clear");
    expect(names).toContain("memory");
    expect(names).toContain("compact");
    expect(names).toContain("fork");
    expect(names).toContain("panel");
  });

  it("panel command is marked as comingSoon", () => {
    const panel = BUILTIN_COMMANDS.find((c) => c.name === "panel");
    expect(panel?.comingSoon).toBe(true);
  });

  it("all commands have name and description", () => {
    for (const cmd of BUILTIN_COMMANDS) {
      expect(typeof cmd.name).toBe("string");
      expect(cmd.name.length).toBeGreaterThan(0);
      expect(typeof cmd.description).toBe("string");
      expect(cmd.description.length).toBeGreaterThan(0);
    }
  });

  // ── P13b: preset-mode slash commands (SL-01..SL-05) ─────────────────────
  it("includes the five preset-mode commands", () => {
    const names = BUILTIN_COMMANDS.map((c) => c.name);
    expect(names).toContain("research");
    expect(names).toContain("code");
    expect(names).toContain("write");
    expect(names).toContain("analyze");
    expect(names).toContain("architect");
  });

  it("preset commands carry a presetId pointing at a PRESETS entry", () => {
    const presetCmds = BUILTIN_COMMANDS.filter((c) => c.presetId !== undefined);
    expect(presetCmds).toHaveLength(5);
    // The mapping is verified more strictly in test_presets.spec.ts; here we
    // assert that every preset command has a non-empty presetId.
    for (const cmd of presetCmds) {
      expect(typeof cmd.presetId).toBe("string");
      expect(cmd.presetId).not.toBe("");
    }
  });
});
