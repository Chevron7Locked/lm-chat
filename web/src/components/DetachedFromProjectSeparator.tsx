/* SPDX-License-Identifier: Apache-2.0 */
/**
 * `DetachedFromProjectSeparator` — chat-history separator turn shown
 * above the first message that ran AFTER the chat was moved out of
 * a project.
 *
 * The chat row's
 * `detached_from_project_meta` JSON is captured at detach time by
 * `chat_service.set_project_id` and survives later deletion of the
 * project — the snapshot stores the project's name + the timestamp
 * + a sha256 hash of the project's system_prompt (not the full
 * text). The separator renders:
 *
 *   "Detached from {name} on {date}"
 *
 * The project name links to `/project/{id}` when the project still
 * exists. When the project has been deleted the link 404s; render
 * plain text in that case to avoid a confusing dead link. Detection
 * is by the optional `projectExists` prop the
 * parent computes via `useProject(meta.project_id).isError`.
 */
import { Link } from "react-router-dom";
import type { CSSProperties } from "react";

export interface DetachedFromProjectMeta {
  project_id: number;
  name: string;
  detached_at: number;
  system_prompt_hash: string;
}

interface DetachedFromProjectSeparatorProps {
  meta: DetachedFromProjectMeta;
  /**
   * When true, render the project name as a clickable link to
   * `/project/{id}`. When false, render plain text (the project was
   * later deleted; the link would 404). Parent computes via
   * `useProject(meta.project_id)`.
   */
  projectExists: boolean;
}

export function DetachedFromProjectSeparator({
  meta,
  projectExists,
}: DetachedFromProjectSeparatorProps) {
  // `detached_at` is float UNIX epoch seconds (chat_service writes
  // `time.time()`). Convert to a locale date string for display.
  const detachedDate = new Date(meta.detached_at * 1000);
  const dateLabel = detachedDate.toLocaleDateString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
  });

  return (
    <div
      role="separator"
      data-testid="detached-from-project-separator"
      style={containerStyle}
    >
      <span style={lineStyle} aria-hidden />
      <span style={textStyle}>
        Detached from{" "}
        {projectExists ? (
          <Link
            to={`/project/${String(meta.project_id)}`}
            style={linkStyle}
            data-testid="detached-project-link"
          >
            {meta.name || `project #${String(meta.project_id)}`}
          </Link>
        ) : (
          <span
            style={plainNameStyle}
            data-testid="detached-project-plain"
            title="Project has been deleted"
          >
            {meta.name || `project #${String(meta.project_id)}`}
          </span>
        )}{" "}
        on {dateLabel}
      </span>
      <span style={lineStyle} aria-hidden />
    </div>
  );
}

const containerStyle: CSSProperties = {
  display: "flex",
  alignItems: "center",
  // Spacing grammar tokens — glue + sibling rhythm.
  gap: "var(--space-glue-relaxed)", // 8px glue between bar+label+bar
  margin: "var(--space-sibling-relaxed) 0", // 16px sibling between rows
  color: "var(--color-text-muted)",
};

const lineStyle: CSSProperties = {
  flex: 1,
  height: 1,
  background: "var(--color-border)",
};

const textStyle: CSSProperties = {
  fontSize: 12,
  whiteSpace: "nowrap",
  padding: "0 8px",
};

const linkStyle: CSSProperties = {
  color: "var(--color-text-muted)",
  textDecoration: "underline",
};

const plainNameStyle: CSSProperties = {
  textDecoration: "underline",
  textDecorationStyle: "dotted",
  cursor: "help",
};
