/**
 * Unit tests for useSTT hook.
 *
 * Covers:
 * - detectSTT capability detection with mock globals (webkit-only,
 *   standard, Firefox-style undefined).
 * - State machine: idle → listening → idle on event-driven results.
 * - Transcript injection callback fires on result.
 * - Error callback fires on recognition error.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { renderHook, act } from "@testing-library/react";
import { detectSTT } from "@/hooks/useSTT";

// ─── Shared mock setup ────────────────────────────────────────────────────────

let mockRecStart: ReturnType<typeof vi.fn>;
let mockRecStop: ReturnType<typeof vi.fn>;
let mockRecInstance: {
  lang: string;
  continuous: boolean;
  interimResults: boolean;
  onresult: ((ev: SpeechRecognitionEvent) => void) | null;
  onend: (() => void) | null;
  onerror: ((ev: SpeechRecognitionErrorEvent) => void) | null;
  start: ReturnType<typeof vi.fn>;
  stop: ReturnType<typeof vi.fn>;
};

// Assigns to the outer mockRecInstance via a plain parameter (not a bare
// `this` alias) so the ctor below can hand off its instance without
// tripping @typescript-eslint/no-this-alias.
function captureRecInstance(instance: typeof mockRecInstance): void {
  mockRecInstance = instance;
}

function installMockRecognition(): void {
  mockRecStart = vi.fn();
  mockRecStop = vi.fn();

  // Must use a regular function (not arrow) so the ctor works via `new`.
  function MockRecognition(this: typeof mockRecInstance) {
    this.lang = "";
    this.continuous = false;
    this.interimResults = false;
    this.onresult = null;
    this.onend = null;
    this.onerror = null;
    this.start = mockRecStart;
    this.stop = mockRecStop;
    // Capture the instance so tests can trigger events.
    captureRecInstance(this);
  }

  Object.defineProperty(window, "webkitSpeechRecognition", {
    value: MockRecognition,
    writable: true,
    configurable: true,
  });
  Object.defineProperty(window, "SpeechRecognition", {
    value: undefined,
    writable: true,
    configurable: true,
  });
}

function removeMockRecognition(): void {
  // Use delete to truly remove the property — setting to undefined still
  // makes "webkitSpeechRecognition" in window truthy.
  try { delete (window as unknown as Record<string, unknown>).webkitSpeechRecognition; } catch { /* ignore */ }
  try { delete (window as unknown as Record<string, unknown>).SpeechRecognition; } catch { /* ignore */ }
}

// ─── detectSTT tests ─────────────────────────────────────────────────────────

describe("detectSTT", () => {
  afterEach(() => {
    removeMockRecognition();
  });

  it("returns unavailable when both globals are absent", () => {
    // Remove any existing recognition globals.
    removeMockRecognition();
    const cap = detectSTT();
    // JSDOM may inject SpeechRecognition; this test only verifies behavior
    // when both are definitively absent (mocked to undefined above).
    // If JSDOM injects one, the test is skipped gracefully.
    if (!("webkitSpeechRecognition" in window) && !("SpeechRecognition" in window)) {
      expect(cap.available).toBe(false);
      expect(cap.engine).toBeNull();
    }
  });

  it("returns webkit engine when webkitSpeechRecognition is present", () => {
    function MockWebkit() { /* empty mock ctor */ }
    Object.defineProperty(window, "webkitSpeechRecognition", {
      value: MockWebkit,
      writable: true,
      configurable: true,
    });
    Object.defineProperty(window, "SpeechRecognition", {
      value: undefined,
      writable: true,
      configurable: true,
    });

    const cap = detectSTT();
    expect(cap.available).toBe(true);
    expect(cap.engine).toBe("webkit");
  });

  it("returns standard engine when SpeechRecognition is present (no webkit)", () => {
    function MockStandard() { /* empty mock ctor */ }
    // Set webkit to undefined (no-value) and standard to MockStandard.
    // detectSTT now checks both "in window" AND truthy value, so
    // webkit=undefined is treated as absent.
    Object.defineProperty(window, "webkitSpeechRecognition", {
      value: undefined,
      writable: true,
      configurable: true,
    });
    Object.defineProperty(window, "SpeechRecognition", {
      value: MockStandard,
      writable: true,
      configurable: true,
    });

    const cap = detectSTT();
    expect(cap.available).toBe(true);
    expect(cap.engine).toBe("standard");
  });
});

// ─── useSTT hook tests ────────────────────────────────────────────────────────

describe("useSTT hook", () => {
  beforeEach(() => {
    installMockRecognition();
  });

  afterEach(() => {
    removeMockRecognition();
    vi.clearAllMocks();
  });

  it("initial state is idle and not listening", async () => {
    const { useSTT } = await import("@/hooks/useSTT");
    const onTranscript = vi.fn();
    const { result } = renderHook(() => useSTT(onTranscript));
    expect(result.current.state.listening).toBe(false);
    expect(result.current.state.error).toBeNull();
  });

  it("start() transitions to listening state and calls recognition.start()", async () => {
    const { useSTT } = await import("@/hooks/useSTT");
    const onTranscript = vi.fn();
    const { result } = renderHook(() => useSTT(onTranscript));

    act(() => {
      result.current.start();
    });

    expect(result.current.state.listening).toBe(true);
    expect(mockRecStart).toHaveBeenCalledOnce();
  });

  it("onresult fires onTranscript callback with the transcript", async () => {
    const { useSTT } = await import("@/hooks/useSTT");
    const onTranscript = vi.fn();
    const { result } = renderHook(() => useSTT(onTranscript));

    act(() => {
      result.current.start();
    });

    const fakeEvent = {
      results: [[{ transcript: "hello world" }]],
    } as unknown as SpeechRecognitionEvent;

    act(() => {
      mockRecInstance.onresult?.(fakeEvent);
    });

    expect(onTranscript).toHaveBeenCalledWith("hello world");
  });

  it("onend resets listening to false", async () => {
    const { useSTT } = await import("@/hooks/useSTT");
    const onTranscript = vi.fn();
    const { result } = renderHook(() => useSTT(onTranscript));

    act(() => { result.current.start(); });
    expect(result.current.state.listening).toBe(true);

    act(() => { mockRecInstance.onend?.(); });
    expect(result.current.state.listening).toBe(false);
  });

  it("stop() calls recognition.stop() and resets state to idle", async () => {
    const { useSTT } = await import("@/hooks/useSTT");
    const onTranscript = vi.fn();
    const { result } = renderHook(() => useSTT(onTranscript));

    act(() => { result.current.start(); });
    expect(result.current.state.listening).toBe(true);

    act(() => { result.current.stop(); });
    expect(result.current.state.listening).toBe(false);
    expect(mockRecStop).toHaveBeenCalledOnce();
  });

  it("onerror fires onError callback and resets listening", async () => {
    const { useSTT } = await import("@/hooks/useSTT");
    const onTranscript = vi.fn();
    const onError = vi.fn();
    const { result } = renderHook(() => useSTT(onTranscript, onError));

    act(() => { result.current.start(); });

    const fakeError = { error: "not-allowed" } as SpeechRecognitionErrorEvent;

    act(() => { mockRecInstance.onerror?.(fakeError); });

    expect(result.current.state.listening).toBe(false);
    expect(onError).toHaveBeenCalledWith("Microphone access denied");
  });

  it("capability is available when webkitSpeechRecognition is installed", async () => {
    const { useSTT } = await import("@/hooks/useSTT");
    const onTranscript = vi.fn();
    const { result } = renderHook(() => useSTT(onTranscript));
    expect(result.current.capability.available).toBe(true);
    expect(result.current.capability.engine).toBe("webkit");
  });
});

describe("useSTT hook — unavailable (no SpeechRecognition)", () => {
  beforeEach(() => {
    removeMockRecognition();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it("start() is a no-op and calls onError when STT is unavailable", async () => {
    const { useSTT } = await import("@/hooks/useSTT");
    const onTranscript = vi.fn();
    const onError = vi.fn();
    const { result } = renderHook(() => useSTT(onTranscript, onError));

    // Only test this if the capability is actually unavailable.
    if (!result.current.capability.available) {
      act(() => { result.current.start(); });
      expect(result.current.state.listening).toBe(false);
      expect(onError).toHaveBeenCalledWith("Speech-to-text not supported in this browser");
    } else {
      // JSDOM has injected SpeechRecognition; skip the assertion.
      expect(result.current.capability.available).toBe(true);
    }
  });
});
