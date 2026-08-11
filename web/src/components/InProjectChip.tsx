/* SPDX-License-Identifier: Apache-2.0 */
/**
 * InProjectChip — small badge shown above the composer when the
 * current chat is in a project.
 *
 * Tells the user "this chat's RAG context +
 * system prompt are scoped to project X." Click to navigate to the
 * project view.
 *
 * Renders nothing when the chat is un-projected (``projectId`` is
 * null) — the legacy behavior.
 */
import { Link } from "react-router-dom";
import { FolderKanban } from "lucide-react";
import { useProject } from "@/hooks/useProjects";
import { useChats } from "@/hooks/useChats";

interface InProjectChipProps {
  /** PK of the current chat. The chip looks up the chat row and
   *  reads ``chats.project_id`` from it; renders nothing if the
   *  chat is un-projected. */
  chatId: number | null;
}

export function InProjectChip({ chatId }: InProjectChipProps) {
  const { data: chatsData } = useChats();
  const chat = chatsData?.chats.find((c) => c.id === chatId);
  const projectId = (chat as { project_id?: number | null } | undefined)
    ?.project_id ?? null;
  // Hook order — always call useProject; pass null to skip the fetch.
  const { data: project } = useProject(projectId);

  if (chatId === null || projectId === null) return null;

  return (
    <Link
      to={`/project/${String(projectId)}`}
      className="lmchat-in-project-chip"
      style={chipStyle}
      data-testid="composer-in-project-chip"
      aria-label={
        project !== undefined ? `In project: ${project.name}` : "In project"
      }
      title="Open project view"
    >
      <FolderKanban size={12} aria-hidden />
      <span>{project?.name ?? "Project"}</span>
    </Link>
  );
}

const chipStyle = {
  display: "inline-flex",
  alignItems: "center",
  // Spacing grammar — glue token.
  gap: "var(--space-glue-relaxed)",
  padding: "var(--space-glue) var(--space-glue-relaxed)",
  fontSize: 11,
  borderRadius: 999,
  background: "var(--color-surface-elevated)",
  color: "var(--color-text-muted)",
  textDecoration: "none",
  marginBottom: 4,
};
