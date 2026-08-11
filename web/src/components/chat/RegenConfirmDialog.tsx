/* SPDX-License-Identifier: Apache-2.0 */
import { ConfirmDialog } from "@/components/chat/ConfirmDialog";
import type { RegenerateConfirmDetail } from "@/hooks/useChats";
import type { MessageRole } from "@/components/ChatMessage";

// ─── RegenConfirmDialog — regenerate / resend confirm modal ─────────────────
// The same confirm gate backs both "Regenerate" (assistant message)
// and "Resend" (user message) — role-aware copy so a resend never reads
// "Regenerating". subsequent_count is the number of later messages the
// action removes before replaying the turn.

interface RegenConfirmDialogProps {
  regenConfirm: RegenerateConfirmDetail;
  targetRole: MessageRole | undefined;
  onConfirm: () => void;
  onCancel: () => void;
}

export function RegenConfirmDialog({
  regenConfirm,
  targetRole,
  onConfirm,
  onCancel,
}: RegenConfirmDialogProps) {
  const n = regenConfirm.subsequent_count;
  const plural = n === 1 ? "" : "s";
  const message =
    targetRole === "user"
      ? `Resend this message? The ${String(n)} message${plural} below will be replaced with a new response.`
      : `Regenerate this response? The ${String(n)} message${plural} below will be replaced with a new response.`;
  return (
    <ConfirmDialog message={message} onConfirm={onConfirm} onCancel={onCancel} />
  );
}
