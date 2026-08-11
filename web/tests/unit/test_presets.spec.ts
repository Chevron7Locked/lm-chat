/**
 * Unit tests for the system-prompt PRESETS module.
 *
 * Pins:
 * - 6 presets (general + the 5 mode-specific ones).
 * - "You are a [X] agent" / "assistant" framing removed everywhere.
 * - Anti-sycophancy + calibrated-uncertainty clauses in every STANDARDS section.
 */
import { describe, it, expect } from "vitest";
import {
  PRESETS,
  PRESET_BY_SLASH_CMD,
  PRESET_LIST,
  getPreset,
  DEFAULT_PRESET_ID,
  RAW_PRESET_ID,
} from "@/lib/presets";

describe("PRESETS", () => {
  it("includes all six entries — general + 5 mode-specific", () => {
    expect(PRESETS.general).toBeDefined();
    expect(PRESETS.coder).toBeDefined();
    expect(PRESETS.creative).toBeDefined();
    expect(PRESETS.research).toBeDefined();
    expect(PRESETS.analyst).toBeDefined();
    expect(PRESETS.architect).toBeDefined();
    expect(Object.keys(PRESETS)).toHaveLength(6);
  });

  it("each preset has a non-empty system_prompt and a label", () => {
    for (const p of Object.values(PRESETS)) {
      expect(typeof p.system_prompt).toBe("string");
      expect(p.system_prompt.length).toBeGreaterThan(100);
      expect(typeof p.label).toBe("string");
      expect(p.label.length).toBeGreaterThan(0);
    }
  });

  it("preset temperatures match the configured tuning", () => {
    expect(PRESETS.general?.temperature).toBe(0.7);
    expect(PRESETS.coder?.temperature).toBe(0.1);
    expect(PRESETS.creative?.temperature).toBe(0.9);
    expect(PRESETS.research?.temperature).toBe(0.4);
    expect(PRESETS.analyst?.temperature).toBe(0.3);
    expect(PRESETS.architect?.temperature).toBe(0.2);
  });

  it("no preset carries the old {{current_date}}/{{tools}} placeholder tokens", () => {
    // Presets used to carry inline {{current_date}}/{{tools}} tokens that
    // were never actually substituted on the main-chat path (Composer sends
    // system_prompt raw) — they shipped to the model as literal bytes. Fixed
    // by dropping the tokens entirely: main-chat gets date/tools framing
    // from the BE's [Context]/[Capabilities] blocks, sub-sessions from
    // buildSubSessionSystemPrompt's prepend/append (subSession.ts).
    for (const p of Object.values(PRESETS)) {
      expect(p.system_prompt).not.toContain("{{current_date}}");
      expect(p.system_prompt).not.toContain("{{tools}}");
    }
  });

  it("removes the banned 'You are a [X] agent/assistant' framing", () => {
    // No preset should open with "You are a [X] agent/assistant" —
    // that framing is banned in favor of direct, non-servant language.
    for (const p of Object.values(PRESETS)) {
      const opener = p.system_prompt.split("\n")[0];
      expect(opener).not.toMatch(/\byou are an? .*\bassistant\b/i);
      expect(opener).not.toMatch(/\byou are an? .*\bagent\b/i);
    }
  });

  it("every preset carries anti-sycophancy + calibrated-uncertainty clauses (§1.2)", () => {
    // The _STANDARDS_TAIL block is pinned at the end of every preset's
    // system_prompt. Verify the load-bearing phrases are present so a
    // future preset rewrite can't drop them silently.
    for (const p of Object.values(PRESETS)) {
      // Anti-sycophancy:
      expect(p.system_prompt).toMatch(
        /silent agreement you don't hold is a lie of omission/i,
      );
      // Calibrated uncertainty:
      expect(p.system_prompt).toMatch(
        /tag confidence on every non-trivial claim/i,
      );
      expect(p.system_prompt).toMatch(/never a guess dressed as fact/i);
    }
  });
});

describe("PRESET_BY_SLASH_CMD", () => {
  it("maps every slash command to its preset", () => {
    expect(PRESET_BY_SLASH_CMD.research?.id).toBe("research");
    expect(PRESET_BY_SLASH_CMD.code?.id).toBe("coder");
    expect(PRESET_BY_SLASH_CMD.write?.id).toBe("creative");
    expect(PRESET_BY_SLASH_CMD.analyze?.id).toBe("analyst");
    expect(PRESET_BY_SLASH_CMD.architect?.id).toBe("architect");
    expect(PRESET_BY_SLASH_CMD.general?.id).toBe("general");
  });
});

describe("PRESET_LIST", () => {
  it("contains all six presets with general first (default-when-unset)", () => {
    expect(PRESET_LIST).toHaveLength(6);
    const ids = PRESET_LIST.map((p) => p.id);
    expect(ids).toEqual([
      "general",
      "research",
      "coder",
      "creative",
      "analyst",
      "architect",
    ]);
  });
});

describe("getPreset", () => {
  it("returns the preset object for a known id", () => {
    expect(getPreset("general")?.id).toBe("general");
    expect(getPreset("coder")?.id).toBe("coder");
    expect(getPreset("research")?.label).toBe("Research");
  });

  it("returns null for null / undefined / empty", () => {
    expect(getPreset(null)).toBeNull();
    expect(getPreset(undefined)).toBeNull();
    expect(getPreset("")).toBeNull();
  });

  it("returns null for the RAW_PRESET_ID sentinel ('none')", () => {
    // RAW_PRESET_ID is the escape hatch for raw-model mode (no system prompt).
    // getPreset("none") must return null so the Composer's null-preset →
    // empty-system_prompt path fires correctly.
    expect(getPreset(RAW_PRESET_ID)).toBeNull();
    expect(getPreset("none")).toBeNull();
  });

  it("returns null for an unknown id", () => {
    expect(getPreset("nonexistent")).toBeNull();
  });
});

describe("DEFAULT_PRESET_ID / RAW_PRESET_ID constants", () => {
  it("DEFAULT_PRESET_ID is 'general' and resolves to the General preset", () => {
    expect(DEFAULT_PRESET_ID).toBe("general");
    expect(getPreset(DEFAULT_PRESET_ID)?.id).toBe("general");
  });

  it("RAW_PRESET_ID is 'none' and is NOT in PRESETS or PRESET_LIST", () => {
    expect(RAW_PRESET_ID).toBe("none");
    expect(getPreset(RAW_PRESET_ID)).toBeNull();
    expect(PRESETS[RAW_PRESET_ID]).toBeUndefined();
    expect(PRESET_LIST.find((p) => p.id === RAW_PRESET_ID)).toBeUndefined();
  });
});
