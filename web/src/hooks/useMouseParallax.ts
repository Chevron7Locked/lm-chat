/* SPDX-License-Identifier: Apache-2.0 */
/**
 * useMouseParallax — writes --parallax-x and --parallax-y on <html>.
 *
 * Throttled to ~24fps (41ms intervals) so it's imperceptible as a
 * performance concern on mid-range hardware. Values range -1 to 1
 * (normalised from viewport center). The CSS body::before picks these
 * up via translate() to give the canvas a near-imperceptible "the room
 * notices you" shift.
 *
 * Disabled entirely when prefers-reduced-motion: reduce is active.
 */
import { useEffect } from "react";

const THROTTLE_MS = 41; // ~24fps

export function useMouseParallax(): void {
  useEffect(() => {
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");

    let rafId: number | null = null;
    let lastFire = 0;
    let pendingX = 0;
    let pendingY = 0;
    let attached = false;

    function apply(): void {
      rafId = null;
      document.documentElement.style.setProperty(
        "--parallax-x",
        String(pendingX),
      );
      document.documentElement.style.setProperty(
        "--parallax-y",
        String(pendingY),
      );
      lastFire = performance.now();
    }

    function handleMove(e: MouseEvent): void {
      const now = performance.now();
      // Normalise: -1 to 1, centered at viewport middle.
      pendingX = (e.clientX / window.innerWidth - 0.5) * 2;
      pendingY = (e.clientY / window.innerHeight - 0.5) * 2;

      if (now - lastFire < THROTTLE_MS) return; // throttle
      if (rafId !== null) return; // already queued
      rafId = requestAnimationFrame(apply);
    }

    function teardown(): void {
      if (!attached) return;
      window.removeEventListener("mousemove", handleMove);
      if (rafId !== null) {
        cancelAnimationFrame(rafId);
        rafId = null;
      }
      document.documentElement.style.removeProperty("--parallax-x");
      document.documentElement.style.removeProperty("--parallax-y");
      attached = false;
    }

    function setup(): void {
      if (attached) return;
      window.addEventListener("mousemove", handleMove, { passive: true });
      attached = true;
    }

    // Respect reduced-motion preference — react to mid-session changes.
    if (!mq.matches) setup();
    const onChange = (): void => {
      if (mq.matches) teardown();
      else setup();
    };
    mq.addEventListener("change", onChange);

    return () => {
      mq.removeEventListener("change", onChange);
      teardown();
    };
  }, []);
}
