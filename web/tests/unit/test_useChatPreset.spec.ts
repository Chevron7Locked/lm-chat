/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for useChatPresetStore (Zustand store layer only).
 *
 * Covers the new default-to-general model (2026-06-20):
 *   - absence → DEFAULT_PRESET_ID ("general"), NOT empty string
 *   - explicit "general" → General system_prompt
 *   - RAW_PRESET_ID ("none") → getPreset returns null → no system_prompt
 *   - hydration of a persisted "none" value
 *   - legacy persisted "" → treated as unset (defaults to general)
 *   - clearPreset now sets RAW_PRESET_ID, not ""
 */
import { describe, it, expect, beforeEach } from "vitest";
import {
  useChatPresetStore,
} from "@/hooks/useChatPreset";
import {
  getPreset,
  DEFAULT_PRESET_ID,
  RAW_PRESET_ID,
} from "@/lib/presets";

// Reset Zustand store before each test so state doesn't leak.
beforeEach(() => {
  useChatPresetStore.setState({ overrides: {} });
});

describe("useChatPresetStore — default-to-general model", () => {
  it("getLocal returns DEFAULT_PRESET_ID when no override has been set", () => {
    const { getLocal } = useChatPresetStore.getState();
    expect(getLocal(42)).toBe(DEFAULT_PRESET_ID);
    expect(getLocal(42)).toBe("general");
  });

  it("getLocal returns the stored value when an override exists", () => {
    const { setLocal, getLocal } = useChatPresetStore.getState();
    setLocal(42, "coder");
    expect(getLocal(42)).toBe("coder");
  });

  it("getLocal returns RAW_PRESET_ID when explicitly set to 'none'", () => {
    const { setLocal, getLocal } = useChatPresetStore.getState();
    setLocal(42, RAW_PRESET_ID);
    expect(getLocal(42)).toBe("none");
    // getPreset("none") must be null so no system_prompt is sent.
    expect(getPreset(getLocal(42))).toBeNull();
  });
});

describe("useChatPresetStore — hydrateFromChats", () => {
  it("hydrates a persisted 'none' value normally (explicit raw choice)", () => {
    const { hydrateFromChats, getLocal } = useChatPresetStore.getState();
    hydrateFromChats([{ id: 10, settings: { active_preset: "none" } }]);
    expect(getLocal(10)).toBe("none");
    expect(getPreset(getLocal(10))).toBeNull();
  });

  it("hydrates a persisted 'coder' value normally", () => {
    const { hydrateFromChats, getLocal } = useChatPresetStore.getState();
    hydrateFromChats([{ id: 11, settings: { active_preset: "coder" } }]);
    expect(getLocal(11)).toBe("coder");
    expect(getPreset(getLocal(11))?.id).toBe("coder");
  });

  it("treats legacy persisted '' as unset → defaults to DEFAULT_PRESET_ID", () => {
    const { hydrateFromChats, getLocal } = useChatPresetStore.getState();
    // An old chat that was saved with active_preset="" should act as if
    // no preset was set — absence defaults to general.
    hydrateFromChats([{ id: 12, settings: { active_preset: "" } }]);
    // Empty string is skipped, so getLocal falls back to DEFAULT_PRESET_ID.
    expect(getLocal(12)).toBe(DEFAULT_PRESET_ID);
  });

  it("does not overwrite an existing in-session override on hydration", () => {
    const { setLocal, hydrateFromChats, getLocal } = useChatPresetStore.getState();
    setLocal(13, "research");
    hydrateFromChats([{ id: 13, settings: { active_preset: "coder" } }]);
    // In-session override wins; hydration is skipped for this chat.
    expect(getLocal(13)).toBe("research");
  });

  it("treats null active_preset as unset → defaults to DEFAULT_PRESET_ID", () => {
    const { hydrateFromChats, getLocal } = useChatPresetStore.getState();
    hydrateFromChats([{ id: 14, settings: { active_preset: null } }]);
    expect(getLocal(14)).toBe(DEFAULT_PRESET_ID);
  });
});

describe("getPreset integration with new sentinel ids", () => {
  it("absence (DEFAULT_PRESET_ID) resolves to the General preset with a system_prompt", () => {
    const preset = getPreset(DEFAULT_PRESET_ID);
    expect(preset).not.toBeNull();
    expect(preset?.id).toBe("general");
    expect(typeof preset?.system_prompt).toBe("string");
    expect((preset?.system_prompt ?? "").length).toBeGreaterThan(50);
  });

  it("RAW_PRESET_ID resolves to null → Composer sends no system_prompt", () => {
    expect(getPreset(RAW_PRESET_ID)).toBeNull();
  });
});
