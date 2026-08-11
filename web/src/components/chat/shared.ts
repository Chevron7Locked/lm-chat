/* SPDX-License-Identifier: Apache-2.0 */
// ─── Shared types for the chat/ component split ──────────────────────────────
// Types referenced both by Chat.tsx (the Chat() component) and by the
// module-level components split out of it into this folder. Kept here (not
// in Chat.tsx) so the split components never import from "@/pages/Chat".

export type PanelView = "settings" | "memory" | "documents" | null;
