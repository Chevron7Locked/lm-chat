/**
 * Vitest global test setup.
 *
 * Polyfills that jsdom doesn't include but are needed by the source code:
 *  - TextDecoderStream: used by useSSE's ReadableStream pipeline.
 *  - BroadcastChannel: stubbed per-test but needs a default global.
 */

// Polyfill TextDecoderStream using the TransformStream API.
// jsdom lacks this; the implementation must satisfy ReadableStream.pipeThrough()
// which requires an object with { readable, writable } properties.
if (typeof globalThis.TextDecoderStream === "undefined") {
  class TextDecoderStreamPolyfill {
    readonly readable: ReadableStream<string>;
    readonly writable: WritableStream<Uint8Array>;

    constructor() {
      const decoder = new TextDecoder();
      const transform = new TransformStream<Uint8Array, string>({
        transform(chunk, controller) {
          controller.enqueue(decoder.decode(chunk, { stream: true }));
        },
        flush(controller) {
          const remaining = decoder.decode();
          if (remaining) controller.enqueue(remaining);
        },
      });
      this.readable = transform.readable;
      this.writable = transform.writable;
    }
  }

  // @ts-expect-error -- polyfill for jsdom
  globalThis.TextDecoderStream = TextDecoderStreamPolyfill;
}

// window.matchMedia stub — jsdom does not implement matchMedia.
// useViewport (added in UX-AUDIT-r1 Wave 3-D) calls window.matchMedia() to
// detect mobile breakpoints; without this stub any component that imports
// useViewport throws on render inside jsdom.
if (typeof window !== "undefined" && typeof window.matchMedia === "undefined") {
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: (query: string) => ({
      matches: false,
      media: query,
      onchange: null,
      addListener: () => undefined,
      removeListener: () => undefined,
      addEventListener: () => undefined,
      removeEventListener: () => undefined,
      dispatchEvent: () => false,
    }),
  });
}

// Default BroadcastChannel stub — tests that need specific behaviour
// override it per-test with vi.fn() / class stub.
if (typeof globalThis.BroadcastChannel === "undefined") {
  class BroadcastChannelPolyfill {
    onmessage: ((ev: MessageEvent) => void) | null = null;
    postMessage(_msg: unknown): void { /* no-op in tests without explicit stub */ }
    close(): void { /* no-op */ }
    addEventListener(): void { /* no-op */ }
    removeEventListener(): void { /* no-op */ }
    dispatchEvent(): boolean { return false; }
  }
  // @ts-expect-error -- polyfill for jsdom
  globalThis.BroadcastChannel = BroadcastChannelPolyfill;
}

// useLmStudioConfig is consumed by AppShell (key-pruned banner) and other
// shell-level components. Page tests that render via AppShell don't supply
// a QueryClientProvider — without a default mock here, useQuery throws on
// import. Tests that want to assert specific config values override this
// per-test with vi.mocked(...). Mirrors the pattern used for other shell-
// scoped hooks (auth store, viewport).
vi.mock("@/hooks/useLmStudioConfig", () => ({
  useLmStudioConfig: () => ({ data: undefined, isLoading: false }),
  lmStudioConfigKeys: {
    all: ["lmstudio-config"],
    resolved: () => ["lmstudio-config", "resolved"],
  },
}));

// Global safety net: restore any globals a test stubbed via vi.stubGlobal and
// clear web storage after every test. Module state is isolated per-file, but
// TRUE globals (vi.stubGlobal, localStorage, sessionStorage) are shared across
// files in the same worker — so a file that stubs a global without restoring it
// can bleed into a later file, producing order-dependent failures. (This was
// latent until added test files reshuffled vitest's file scheduling.)
afterEach(() => {
  vi.unstubAllGlobals();
  try {
    localStorage.clear();
    sessionStorage.clear();
  } catch {
    /* storage not available in this environment — ignore */
  }
});

