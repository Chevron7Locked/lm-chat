/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Unit tests for buildConnBody — the pure helper that determines which
 * connection fields to include in a Save submission.
 *
 * Locked behaviour (bug fix 2026-06-27):
 *   When the user changes only the default_model and leaves base_url and
 *   api_key untouched, buildConnBody must return an empty object so that
 *   the admin PATCH is skipped entirely.  Sending an unchanged base_url
 *   triggers a backend probe that returns 401 when LM Studio requires an
 *   API key — the "Probe of http://localhost:1234 failed: HTTP 401. Save
 *   aborted" lockout.
 */
import { describe, it, expect } from "vitest";
import { buildConnBody } from "@/components/LmStudioSection";

describe("buildConnBody", () => {
  const LOADED_URL = "http://localhost:1234";

  it("returns empty object when base_url unchanged and api_key not typed", () => {
    // Model-only change: base_url is still the loaded value, api_key field
    // is empty (user never typed into it).  No connection fields should be
    // sent — this skips the backend probe.
    const result = buildConnBody(LOADED_URL, "", LOADED_URL);
    expect(result).toEqual({});
    expect("base_url" in result).toBe(false);
    expect("api_key" in result).toBe(false);
  });

  it("includes base_url only when it differs from the loaded value", () => {
    const result = buildConnBody("http://newhost:5678", "", LOADED_URL);
    expect(result.base_url).toBe("http://newhost:5678");
    expect("api_key" in result).toBe(false);
  });

  it("includes api_key when the user typed a new value", () => {
    // api_key field starts empty; any non-empty value means the user typed it.
    const result = buildConnBody(LOADED_URL, "newkey", LOADED_URL);
    expect(result.api_key).toBe("newkey");
    // base_url unchanged → omitted.
    expect("base_url" in result).toBe(false);
  });

  it("includes both fields when base_url changed AND api_key typed", () => {
    const result = buildConnBody("http://newhost:5678", "newkey", LOADED_URL);
    expect(result.base_url).toBe("http://newhost:5678");
    expect(result.api_key).toBe("newkey");
  });

  it("includes base_url when the user clears it (empty string differs from loaded)", () => {
    // The user explicitly cleared the URL — forward the empty string so the
    // backend can validate/reject it.
    const result = buildConnBody("", "", LOADED_URL);
    expect(result.base_url).toBe("");
    expect("api_key" in result).toBe(false);
  });

  it("does not include api_key when input is empty (user never typed)", () => {
    // The api_key input starts empty and is never prefilled.  An empty
    // string means the field was untouched — never send it.
    const result = buildConnBody(LOADED_URL, "", LOADED_URL);
    expect("api_key" in result).toBe(false);
  });
});
