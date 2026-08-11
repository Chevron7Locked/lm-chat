/* SPDX-License-Identifier: Apache-2.0 */
import { DetachedFromProjectSeparator } from "@/components/DetachedFromProjectSeparator";
import { useProject } from "@/hooks/useProjects";

/**
 * Thin wrapper that queries `useProject(meta.project_id)` to decide
 * whether the separator renders the project name as a clickable link
 * (project still exists) or plain text (project has been deleted).
 * Wraps `DetachedFromProjectSeparator` so the React-Query call doesn't
 * sit inside the main Chat component's render path.
 */
export function DetachFromProjectMarker({
  meta,
}: {
  meta: {
    project_id: number;
    name: string;
    detached_at: number;
    system_prompt_hash: string;
  };
}) {
  const { isError } = useProject(meta.project_id);
  return <DetachedFromProjectSeparator meta={meta} projectExists={!isError} />;
}
