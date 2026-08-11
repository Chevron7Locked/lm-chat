/* SPDX-License-Identifier: Apache-2.0 */
/**
 * QuotaSection — Settings → Quota tab.
 *
 * Renders daily request + token usage as progress bars with threshold
 * coloring (accent < 50%, amber 50-80%, danger > 80%) plus a countdown
 * to the reset time.
 *
 * Uses settings.css semantic classes rather than inline CSSProperties.
 * Spacing grammar applied:
 *   - Bar → bar: GROUP (24px) via .lmchat-quota-container gap
 *   - Bar header → track: GLUE-RELAXED (8px) via .lmchat-quota-bar-block gap
 *   - Reset row top separator: GLUE-RELAXED (8px) padding-top
 */
import { useQuotaMe } from "@/hooks/useQuota";
import "@/styles/settings.css";

export function QuotaSection() {
  const { data: quotaData } = useQuotaMe();

  if (quotaData === undefined) {
    return (
      <p
        className="lmchat-section-description"
        data-testid="settings-quota-section"
      >
        Loading quota…
      </p>
    );
  }

  return (
    <div
      className="lmchat-section-container"
      data-testid="settings-quota-section"
    >
      <section className="lmchat-section" aria-label="Daily usage quotas">
        <div className="lmchat-quota-container">
          <QuotaBar
            label="Requests today"
            used={quotaData.requests_consumed_today}
            limit={quotaData.requests_per_day}
          />
          <QuotaBar
            label="Tokens today"
            used={quotaData.tokens_consumed_today}
            limit={quotaData.tokens_per_day}
          />
          <div className="lmchat-quota-reset-row">
            <span className="lmchat-quota-bar-label">Resets</span>
            <span className="lmchat-quota-reset-value">
              {formatReset(quotaData.resets_at)}
            </span>
          </div>
        </div>
      </section>
    </div>
  );
}

// ─── Quota bar ────────────────────────────────────────────────────────────────

function QuotaBar({
  label,
  used,
  limit,
}: {
  label: string;
  used: number;
  limit: number;
}) {
  const pct = limit > 0 ? Math.min(100, (used / limit) * 100) : 0;
  const tone = pct >= 80 ? "danger" : pct >= 50 ? "warning" : "ok";
  const color =
    tone === "danger"
      ? "var(--color-danger)"
      : tone === "warning"
        ? "var(--color-warning)"
        : "var(--color-accent)";

  return (
    <div className="lmchat-quota-bar-block">
      <div className="lmchat-quota-bar-header">
        <span className="lmchat-quota-bar-label">{label}</span>
        <span className="lmchat-quota-bar-value">
          {used.toLocaleString()} / {limit.toLocaleString()}
          <span className="lmchat-quota-bar-pct"> ({Math.round(pct)}%)</span>
        </span>
      </div>
      <div
        className="lmchat-quota-track"
        role="progressbar"
        aria-valuenow={Math.round(pct)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label={`${label}: ${String(Math.round(pct))}% of daily limit`}
      >
        <div
          className="lmchat-quota-fill"
          style={{
            transform: `scaleX(${String(pct / 100)})`,
            background: color,
          }}
        />
      </div>
    </div>
  );
}

// ─── Helpers ───────────────────────────────────────────────────────────────────

function formatReset(iso: string): string {
  const reset = new Date(iso);
  const time = reset.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    timeZoneName: "short",
  });
  const msLeft = reset.getTime() - Date.now();
  if (msLeft <= 0) return time;
  const totalMin = Math.round(msLeft / 60_000);
  const h = Math.floor(totalMin / 60);
  const m = totalMin % 60;
  const rel = h > 0 ? `${String(h)}h ${String(m)}m` : `${String(m)}m`;
  return `${time} (in ${rel})`;
}
