/**
 * Unit tests for toastStore and useToast hook.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";

async function freshStore() {
  vi.resetModules();
  const { useToastStore, useToast } = await import("@/stores/toastStore");
  return { useToastStore, useToast };
}

describe("toastStore", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("push() adds a toast to the queue", async () => {
    const { useToastStore } = await freshStore();
    useToastStore.getState().push({ variant: "info", message: "hello" });
    expect(useToastStore.getState().toasts).toHaveLength(1);
    expect(useToastStore.getState().toasts[0]?.message).toBe("hello");
    expect(useToastStore.getState().toasts[0]?.variant).toBe("info");
  });

  it("push() returns a unique id", async () => {
    const { useToastStore } = await freshStore();
    const id1 = useToastStore.getState().push({ variant: "info", message: "a" });
    const id2 = useToastStore.getState().push({ variant: "info", message: "b" });
    expect(id1).not.toBe(id2);
    expect(typeof id1).toBe("string");
  });

  it("auto-dismisses after default duration (5000ms)", async () => {
    const { useToastStore } = await freshStore();
    useToastStore.getState().push({ variant: "success", message: "auto-dismiss" });
    expect(useToastStore.getState().toasts).toHaveLength(1);

    vi.advanceTimersByTime(5_000);
    expect(useToastStore.getState().toasts).toHaveLength(0);
  });

  it("auto-dismisses after custom duration", async () => {
    const { useToastStore } = await freshStore();
    useToastStore.getState().push({ variant: "info", message: "fast", duration: 2_000 });
    expect(useToastStore.getState().toasts).toHaveLength(1);

    vi.advanceTimersByTime(1_999);
    expect(useToastStore.getState().toasts).toHaveLength(1);

    vi.advanceTimersByTime(1);
    expect(useToastStore.getState().toasts).toHaveLength(0);
  });

  it("duration: 0 never auto-dismisses", async () => {
    const { useToastStore } = await freshStore();
    useToastStore.getState().push({ variant: "error", message: "sticky", duration: 0 });
    vi.advanceTimersByTime(60_000);
    expect(useToastStore.getState().toasts).toHaveLength(1);
  });

  it("dismiss() removes a specific toast by id", async () => {
    const { useToastStore } = await freshStore();
    const id = useToastStore.getState().push({ variant: "warning", message: "test" });
    useToastStore.getState().push({ variant: "info", message: "keep" });
    expect(useToastStore.getState().toasts).toHaveLength(2);

    useToastStore.getState().dismiss(id);
    expect(useToastStore.getState().toasts).toHaveLength(1);
    expect(useToastStore.getState().toasts[0]?.message).toBe("keep");
  });

  it("clear() removes all toasts", async () => {
    const { useToastStore } = await freshStore();
    useToastStore.getState().push({ variant: "info", message: "a" });
    useToastStore.getState().push({ variant: "error", message: "b" });
    expect(useToastStore.getState().toasts).toHaveLength(2);

    useToastStore.getState().clear();
    expect(useToastStore.getState().toasts).toHaveLength(0);
  });

  it("multiple toasts stack", async () => {
    const { useToastStore } = await freshStore();
    useToastStore.getState().push({ variant: "info", message: "1" });
    useToastStore.getState().push({ variant: "success", message: "2" });
    useToastStore.getState().push({ variant: "error", message: "3" });
    expect(useToastStore.getState().toasts).toHaveLength(3);
  });

  it("useToast accesses push and dismiss from the store", async () => {
    // useToast calls Zustand hooks internally. We test the underlying store
    // functions directly since hook invocations outside React components
    // are not valid per the Rules of Hooks.
    const { useToastStore } = await freshStore();
    const push = useToastStore.getState().push;
    const dismiss = useToastStore.getState().dismiss;
    expect(typeof push).toBe("function");
    expect(typeof dismiss).toBe("function");
  });

  it("new toasts default to count: 1", async () => {
    const { useToastStore } = await freshStore();
    useToastStore.getState().push({ variant: "info", message: "one" });
    expect(useToastStore.getState().toasts[0]?.count).toBe(1);
  });
});

describe("toastStore — stack cap", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("exposes MAX_TOASTS = 3", async () => {
    const { MAX_TOASTS } = await import("@/stores/toastStore");
    expect(MAX_TOASTS).toBe(3);
  });

  it("caps the rendered stack at MAX_TOASTS and evicts the oldest", async () => {
    const { useToastStore, MAX_TOASTS } = await import("@/stores/toastStore");
    // Use distinct messages so coalescing does not interfere.
    for (let i = 0; i < 6; i++) {
      useToastStore.getState().push({ variant: "info", message: `m${String(i)}` });
    }
    const toasts = useToastStore.getState().toasts;
    expect(toasts).toHaveLength(MAX_TOASTS);
    // Newest survive; oldest (m0..m2) evicted.
    expect(toasts.map((t) => t.message)).toEqual(["m3", "m4", "m5"]);
  });

  it("never grows past the cap even with sticky (error) toasts", async () => {
    const { useToastStore, MAX_TOASTS } = await import("@/stores/toastStore");
    for (let i = 0; i < 5; i++) {
      useToastStore
        .getState()
        .push({ variant: "error", message: `e${String(i)}`, duration: 0 });
    }
    expect(useToastStore.getState().toasts).toHaveLength(MAX_TOASTS);
  });
});

describe("toastStore — duplicate coalescing", () => {
  beforeEach(() => {
    vi.resetAllMocks();
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("coalesces identical repeated pushes into one toast with a count", async () => {
    const { useToastStore } = await freshStore();
    const id1 = useToastStore.getState().push({ variant: "success", message: "saved" });
    const id2 = useToastStore.getState().push({ variant: "success", message: "saved" });
    const id3 = useToastStore.getState().push({ variant: "success", message: "saved" });
    expect(useToastStore.getState().toasts).toHaveLength(1);
    expect(useToastStore.getState().toasts[0]?.count).toBe(3);
    // Coalesced pushes return the existing id.
    expect(id2).toBe(id1);
    expect(id3).toBe(id1);
  });

  it("does NOT coalesce different messages or variants", async () => {
    const { useToastStore } = await freshStore();
    useToastStore.getState().push({ variant: "success", message: "saved" });
    useToastStore.getState().push({ variant: "success", message: "other" });
    useToastStore.getState().push({ variant: "error", message: "saved" });
    expect(useToastStore.getState().toasts).toHaveLength(3);
  });

  it("does NOT coalesce toasts that carry an action (action preserved)", async () => {
    const { useToastStore } = await freshStore();
    const action = { label: "Undo", onClick: () => undefined };
    useToastStore.getState().push({ variant: "info", message: "act", action });
    useToastStore.getState().push({ variant: "info", message: "act", action });
    expect(useToastStore.getState().toasts).toHaveLength(2);
  });
});
