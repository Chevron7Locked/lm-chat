/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Analytics page — per-user usage stats.
 *
 * Route: /analytics (lazy-loaded chunk).
 * Links from UserMenu dropdown.
 *
 * Shows:
 * - Hero trio: saved-vs-cloud (USD), tokens today, avg streaming speed.
 *   Same source as the sidebar footer (useSidebarStats) — single source of truth.
 * - Total messages / chats
 * - Messages last 7 days
 * - Top 5 models by usage (horizontal CSS bars; no recharts dep)
 * - System stats card (admin only, gated by is_admin)
 *
 * Inline CSSProperties replaced with reading-rooms.css semantic classes.
 */
import React, { useEffect, useRef, useState } from "react";
import { AppShell } from "@/components/AppShell";
import { useDocumentTitle } from "@/hooks/useDocumentTitle";
import { useMyAnalytics, useSystemAnalytics } from "@/hooks/useAnalytics";
import type { TopModel, DayCount } from "@/hooks/useAnalytics";
import {
  useSidebarStats,
  formatTokens,
  formatUsd,
} from "@/hooks/useSidebarStats";
import { useAuthStore } from "@/stores/authStore";
import "@/styles/reading-rooms.css";

export default function Analytics() {
  useDocumentTitle("Analytics");
  const { user } = useAuthStore();
  const { data: me, isLoading: meLoading, isError: meError } = useMyAnalytics();

  const isAdmin = user?.is_admin === true;
  const { data: sys, isLoading: sysLoading } = useSystemAnalytics();

  // Hero metrics: identical to the sidebar footer trio so the page and sidebar
  // can never disagree. Saved-vs-cloud is the headline number; tokens-today
  // and avg-streaming-tps support it.
  const sidebarStats = useSidebarStats();

  return (
    <AppShell>
      <div className="rr-page">
        <header className="rr-page-header">
          {/* Context-specific eyebrow */}
          <span className="rr-eyebrow">The Ledger</span>
          <h1 className="rr-page-title">Analytics</h1>
        </header>

        {meLoading && <p className="rr-hint">Loading…</p>}
        {meError && (
          <p className="rr-hint rr-hint--error">
            Couldn't load analytics — try again.
          </p>
        )}

        {/* Analytics hero — asymmetric editorial composition.
          Primary metric breaks out at display scale; secondary metrics flow
          below as plain text lines (value · italic descriptor). No cards. */}
        {sidebarStats.isReady && (
          <div className="rr-analytics-hero">
            {/* Asymmetric primary metric block */}
            <div className="rr-analytics-primary">
              <HeroNumber value={sidebarStats.approxSavedUsd} />
              {/* "saved vs cloud" must remain as visible text — test + screen-reader contract */}
              <p className="rr-analytics-marginalia">
                <span className="rr-analytics-marginalia-label">
                  saved vs cloud
                </span>
                {sidebarStats.approxSavedUsd > 0
                  ? " — your machine kept it out of the cloud."
                  : ""}
              </p>
            </div>

            {/* Secondary metrics — plain text lines, not cards */}
            <div className="rr-analytics-secondary-stack">
              <p className="rr-analytics-secondary-line">
                <span className="rr-analytics-secondary-value">
                  {formatTokens(sidebarStats.tokensToday)}
                </span>
                <span className="rr-analytics-secondary-sep" aria-hidden="true">
                  {" "}
                  ·{" "}
                </span>
                <span className="rr-analytics-secondary-desc">
                  tokens today
                </span>
              </p>
              <p className="rr-analytics-secondary-line">
                <span className="rr-analytics-secondary-value">
                  {sidebarStats.streamingTps ?? "—"}
                </span>
                <span className="rr-analytics-secondary-sep" aria-hidden="true">
                  {" "}
                  ·{" "}
                </span>
                <span className="rr-analytics-secondary-desc">
                  tokens / second · last stream
                </span>
              </p>
            </div>
          </div>
        )}

        {me && (
          <>
            {/* Usage stats — editorial text rows, not stat cards */}
            <section className="rr-analytics-section">
              <h2 className="rr-analytics-heading">Your usage</h2>
              <div className="rr-stat-row-group">
                <div className="rr-stat-row">
                  <span className="rr-stat-value">
                    {me.total_messages.toLocaleString()}
                  </span>
                  <span className="rr-stat-label">total messages</span>
                </div>
                <div className="rr-stat-row">
                  <span className="rr-stat-value">
                    {me.total_chats.toLocaleString()}
                  </span>
                  <span className="rr-stat-label">total chats</span>
                </div>
                <div className="rr-stat-row">
                  <span className="rr-stat-value">
                    {me.messages_last_7_days.toLocaleString()}
                  </span>
                  <span className="rr-stat-label">
                    messages in the last 7 days
                  </span>
                </div>
              </div>
            </section>

            {/* Activity chart — copper bars, ink-bleed axis */}
            {me.messages_by_day.length > 0 && (
              <section className="rr-analytics-section">
                <h2 className="rr-analytics-heading">
                  Activity · last 14 days
                </h2>
                <ActivityChart series={me.messages_by_day} />
              </section>
            )}

            {/* Top models — show baseline placeholder bars when
              no model data yet so the section reads as "axis tick marks" not
              "missing bars". Populated bars replace the placeholder. */}
            {me.top_models.length > 0 ? (
              <section className="rr-analytics-section">
                <h2 className="rr-analytics-heading">Top models</h2>
                <ModelBars models={me.top_models} />
              </section>
            ) : (
              <section className="rr-analytics-section">
                <h2 className="rr-analytics-heading">Top models</h2>
                <div className="rr-model-bars" aria-hidden="true">
                  {[0.6, 0.4, 0.25].map((w, i) => (
                    <div
                      key={i}
                      className="rr-model-bar-row"
                      style={{ opacity: 0.35 }}
                    >
                      <span
                        className="rr-model-label"
                        style={{
                          fontStyle: "italic",
                          color: "var(--color-text-subtle)",
                        }}
                      >
                        —
                      </span>
                      <div className="rr-bar-track">
                        <div
                          className="rr-bar-fill"
                          style={{
                            transform: `scaleX(${String(w)})`,
                            background: "var(--color-border-default)",
                          }}
                        />
                      </div>
                      <span
                        className="rr-bar-count"
                        style={{ color: "var(--color-text-subtle)" }}
                      >
                        0
                      </span>
                    </div>
                  ))}
                </div>
              </section>
            )}
          </>
        )}

        {/* Admin system stats */}
        {isAdmin && (
          <>
            <hr className="rr-divider" aria-hidden="true" />
            <section className="rr-analytics-section" style={{ marginTop: 0 }}>
              <h2 className="rr-analytics-heading">System · admin</h2>
              {sysLoading && <p className="rr-hint">Loading…</p>}
              {sys && (
                <>
                  <div className="rr-stat-row-group">
                    <div className="rr-stat-row">
                      <span className="rr-stat-value">
                        {sys.total_users.toLocaleString()}
                      </span>
                      <span className="rr-stat-label">total users</span>
                    </div>
                    <div className="rr-stat-row">
                      <span className="rr-stat-value">
                        {sys.total_chats.toLocaleString()}
                      </span>
                      <span className="rr-stat-label">total chats</span>
                    </div>
                    <div className="rr-stat-row">
                      <span className="rr-stat-value">
                        {sys.total_messages.toLocaleString()}
                      </span>
                      <span className="rr-stat-label">total messages</span>
                    </div>
                    <div className="rr-stat-row">
                      <span className="rr-stat-value">
                        {sys.messages_last_7_days.toLocaleString()}
                      </span>
                      <span className="rr-stat-label">
                        messages in the last 7 days
                      </span>
                    </div>
                  </div>
                  {sys.top_models.length > 0 && (
                    <>
                      <h3
                        className="rr-analytics-subheading"
                        style={{ marginTop: "var(--space-group)" }}
                      >
                        Top models · system
                      </h3>
                      <ModelBars models={sys.top_models} />
                    </>
                  )}
                </>
              )}
            </section>
          </>
        )}
      </div>
    </AppShell>
  );
}

// ─── ActivityChart ─────────────────────────────────────────────────────────────

function ActivityChart({ series }: { series: DayCount[] }) {
  const max = Math.max(...series.map((d) => d.count), 1);
  const total = series.reduce((acc, d) => acc + d.count, 0);
  const prefersReduced =
    typeof window !== "undefined" &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  return (
    <div>
      <div
        className="rr-activity-chart"
        role="img"
        aria-label={`${String(total)} messages over the last 14 days`}
      >
        {series.map((d, i) => {
          const scale = Math.max(d.count > 0 ? 0.06 : 0, d.count / max);
          const dayNum = new Date(d.day + "T00:00:00").getDate();
          return (
            <div
              key={d.day}
              className="rr-activity-col"
              title={`${d.day}: ${String(d.count)} messages`}
            >
              <div className="rr-activity-bar-wrap">
                <div
                  className="rr-activity-bar"
                  style={
                    {
                      // Final scale used both as CSS var (for the animation endpoint)
                      // and as the resolved transform (for no-animation / reduced-motion).
                      "--bar-i": String(i),
                      "--bar-scale": String(scale),
                      transform: prefersReduced
                        ? `scaleY(${String(scale)})`
                        : undefined,
                      minHeight: d.count > 0 ? 2 : 0,
                    } as React.CSSProperties
                  }
                />
              </div>
              <span className="rr-activity-tick">{dayNum}</span>
            </div>
          );
        })}
      </div>
      <p className="rr-activity-caption">
        {total.toLocaleString()} messages in the last 14 days
      </p>
    </div>
  );
}

// ─── HeroNumber — count-up from 0 → value over 600ms ease-out-expo ───────────

function useCountUp(target: number, durationMs = 600): number {
  const [display, setDisplay] = useState(0);
  const startTs = useRef<number | null>(null);
  const rafId = useRef<number | null>(null);
  const prefersReduced = useRef(
    typeof window !== "undefined"
      ? window.matchMedia("(prefers-reduced-motion: reduce)").matches
      : false,
  );

  useEffect(() => {
    if (prefersReduced.current) {
      setDisplay(target);
      return;
    }
    startTs.current = null;
    function tick(now: number): void {
      startTs.current ??= now;
      const elapsed = now - startTs.current;
      // ease-out-expo approximation: 1 - 2^(-10 * t)
      const t = Math.min(elapsed / durationMs, 1);
      const eased = t === 1 ? 1 : 1 - Math.pow(2, -10 * t);
      // Keep fractional precision; the caller formats the final value.
      setDisplay(eased * target);
      if (t < 1) {
        rafId.current = requestAnimationFrame(tick);
      }
    }
    rafId.current = requestAnimationFrame(tick);
    return () => {
      if (rafId.current !== null) cancelAnimationFrame(rafId.current);
    };
  }, [target, durationMs]);

  return display;
}

function HeroNumber({ value }: { value: number }) {
  const display = useCountUp(value, 600);
  return (
    <span
      className="rr-hero-number"
      aria-label={`${formatUsd(value)} saved vs cloud`}
    >
      {formatUsd(display)}
    </span>
  );
}

// ─── ModelBars ───────────────────────────────────────────────────────────────

function ModelBars({ models }: { models: TopModel[] }) {
  const max = Math.max(...models.map((m) => m.count), 1);
  return (
    <div className="rr-model-bars">
      {models.map((m) => (
        <div key={m.model_id} className="rr-model-bar-row">
          <span className="rr-model-label">{m.model_id}</span>
          <div className="rr-bar-track">
            <div
              className="rr-bar-fill"
              style={{
                transform: `scaleX(${String(Math.max(0, Math.min(1, m.count / max)))})`,
              }}
              aria-label={`${String(m.count)} messages`}
            />
          </div>
          <span className="rr-bar-count">{m.count}</span>
        </div>
      ))}
    </div>
  );
}
