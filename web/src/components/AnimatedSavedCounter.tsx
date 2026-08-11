/* SPDX-License-Identifier: Apache-2.0 */
/**
 * AnimatedSavedCounter — a local-pride delight moment.
 *
 * The sidebar "Saved" stat is the most direct expression of the design
 * principle that local identity is the brag.  We turn the
 * plain dollar-value text into a live counter that tweens between
 * the prior and current value using requestAnimationFrame, and flares
 * mint-coloured for ~700ms whenever the displayed cent value ticks up.
 *
 * Restraint guards (per the same design principle):
 *   - Tween duration capped at 600ms (no Disney-like dramatics).
 *   - Easing is ease-out-quart — matches the rest of the app's motion.
 *   - The flare is a single subtle background flash, not a glow ring
 *     or a confetti burst.  Mint is the design-token accent for
 *     positive cost-savings only.
 *   - Honours prefers-reduced-motion: skips the tween, skips the flare.
 */
import {
  useEffect,
  useRef,
  useState,
  type CSSProperties,
  type ReactElement,
} from "react";
import { formatUsd } from "@/hooks/useSidebarStats";

interface AnimatedSavedCounterProps {
  /** Authoritative current value from useSidebarStats. */
  valueUsd: number;
  /** Style applied to the outer span so the parent can match the row. */
  style?: CSSProperties;
}

function easeOutQuart(t: number): number {
  return 1 - Math.pow(1 - t, 4);
}

const TWEEN_MS = 600;

export function AnimatedSavedCounter({
  valueUsd,
  style,
}: AnimatedSavedCounterProps): ReactElement {
  const [displayed, setDisplayed] = useState<number>(valueUsd);
  const [flaring, setFlaring] = useState<boolean>(false);
  // Dollar-milestone celebration — when the displayed value
  // crosses an integer-dollar boundary upwards (1, 5, 10, 25, 50, 100…)
  // we flag a "milestone" flare that lasts longer + uses the accent
  // colour instead of the standard mint.  Tiny cent-tick flares stay
  // brief; the dollar moments get a beat.
  const [milestone, setMilestone] = useState<boolean>(false);
  const startRef = useRef<number>(valueUsd);
  const targetRef = useRef<number>(valueUsd);
  const rafRef = useRef<number | null>(null);
  const startTimeRef = useRef<number | null>(null);
  const lastFlareCentRef = useRef<number>(Math.floor(valueUsd * 100));
  const lastDollarRef = useRef<number>(Math.floor(valueUsd));
  const reduceMotionRef = useRef<boolean>(false);

  useEffect(() => {
    reduceMotionRef.current = window.matchMedia(
      "(prefers-reduced-motion: reduce)",
    ).matches;
  }, []);

  useEffect(() => {
    // Skip tween + flare entirely for reduced-motion users.
    if (reduceMotionRef.current) {
      setDisplayed(valueUsd);
      return;
    }
    // No work if the value didn't move.
    if (Math.abs(targetRef.current - valueUsd) < 1e-6) return;
    startRef.current = displayed;
    targetRef.current = valueUsd;
    startTimeRef.current = null;
    if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    const tick = (now: number): void => {
      startTimeRef.current ??= now;
      const elapsed = now - startTimeRef.current;
      const t = Math.min(1, elapsed / TWEEN_MS);
      const eased = easeOutQuart(t);
      const next =
        startRef.current + (targetRef.current - startRef.current) * eased;
      setDisplayed(next);
      // Flare whenever the *displayed cent* crosses an integer boundary
      // upwards — the dollar value advancing by a penny is the moment
      // worth celebrating in this micro-interaction.
      const nextCent = Math.floor(next * 100);
      if (nextCent > lastFlareCentRef.current) {
        lastFlareCentRef.current = nextCent;
        setFlaring(true);
        window.setTimeout(() => {
          setFlaring(false);
        }, 700);
      }
      // Dollar-milestone — fires once when the integer dollar
      // value advances.  Lasts longer than the cent flare (1400ms) so
      // the user has time to notice.
      const nextDollar = Math.floor(next);
      if (nextDollar > lastDollarRef.current) {
        lastDollarRef.current = nextDollar;
        setMilestone(true);
        window.setTimeout(() => {
          setMilestone(false);
        }, 1400);
      }
      if (t < 1) {
        rafRef.current = requestAnimationFrame(tick);
      } else {
        rafRef.current = null;
      }
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => {
      if (rafRef.current !== null) cancelAnimationFrame(rafRef.current);
    };
    // We intentionally exclude `displayed` — the tween reads its own
    // closure-captured start value and rebases via the ref pattern.
  }, [valueUsd]);

  return (
    <span
      style={{
        ...style,
        position: "relative",
        display: "inline-block",
        padding: "0 4px",
        borderRadius: "var(--radius-xs)",
        background: milestone
          ? "var(--color-accent-quiet)"
          : flaring
            ? "color-mix(in oklch, var(--color-success) 18%, transparent)"
            : "transparent",
        // Conditional spread: avoids `color: undefined` shadowing the style
        // prop's color (spread `{color: undefined}` wins over prior spread).
        ...(milestone ? { color: "var(--color-accent)", fontWeight: 600 } : {}),
        transition:
          "background 700ms cubic-bezier(0.165, 0.84, 0.44, 1), color 700ms cubic-bezier(0.165, 0.84, 0.44, 1)",
      }}
      data-flaring={flaring ? "true" : undefined}
      data-milestone={milestone ? "true" : undefined}
      data-testid="sidebar-saved-counter"
      // Announces dollar-milestone crossings to AT users.
      role={milestone ? "status" : undefined}
      aria-live={milestone ? "polite" : undefined}
    >
      {formatUsd(displayed)}
    </span>
  );
}
