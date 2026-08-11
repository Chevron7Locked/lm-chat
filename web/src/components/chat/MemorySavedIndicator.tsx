/* SPDX-License-Identifier: Apache-2.0 */

/**
 * MemorySavedIndicator — a quiet, single-line signal that auto-memory
 * stored a durable fact from the just-completed turn.
 *
 * Sourcing: the BE `memory.saved` SSE frame, emitted after `chat.end` (and
 * after `followups`, when both fire) once the detached auto-memory
 * distillation task resolves within the BE's bounded wait — see
 * streaming_service.py `_format_memory_saved_frame` /
 * `_MEMORY_SAVED_FRAME_WAIT_SEC`. A slow/failed distillation still stores
 * the fact server-side; it just won't show this indicator for that turn
 * (the fact is visible on the Memory page regardless).
 *
 * Intentionally unobtrusive — auto-memory is a background feature, not
 * something that should interrupt the reading flow with a toast or modal.
 * Styled like ChatMessage's MessageStatsChip (`.lmchat-stats-chip`), NOT
 * like the more prominent FollowupChips row.
 */
export function MemorySavedIndicator({
  memorySaved,
}: {
  memorySaved: { count: number; msgId: number } | undefined;
}) {
  if (memorySaved === undefined || memorySaved.count <= 0) return null;
  const label =
    memorySaved.count === 1
      ? "Memory updated"
      : `Memory updated · ${String(memorySaved.count)} things remembered`;
  return (
    <div
      className="lmchat-memory-saved"
      role="status"
      data-testid="memory-saved-indicator"
    >
      {label}
    </div>
  );
}
