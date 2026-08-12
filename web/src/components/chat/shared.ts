/* SPDX-License-Identifier: Apache-2.0 */
// ─── Shared types for the chat/ component split ──────────────────────────────
// Types referenced both by Chat.tsx (the Chat() component) and by the
// module-level components split out of it into this folder. Kept here (not
// in Chat.tsx) so the split components never import from "@/pages/Chat".

export type PanelView = "settings" | "memory" | "documents" | null;

// ─── "Auto" model-picker sentinel ────────────────────────────────────────────
// The chat model picker shows "Auto" whenever the chat has NO explicit
// per-chat model override (no memory-tier dropdown pick and no persisted
// ``chats.model_id``). "Auto" resolves to the user's default model at send
// time — the send path (Composer ``modelId`` prop + ``resolveTurnModel``)
// already falls through ``selectedModel ?? currentChat.model_id ??
// savedDefaultModel``, so the sentinel only ever drives DISPLAY + reset, never
// a wire model id.
//
// ``AUTO_MODEL_VALUE`` is the ``<option value>`` / ``<select value>`` used for
// the Auto entry. It deliberately contains no "::" so it can never collide
// with a real "<provider>::<model_id>" composite, and Chat's ``onModelChange``
// intercepts it before the composite decode to clear the override instead of
// persisting it as a model id.
export const AUTO_MODEL_VALUE = "__auto__";
export const AUTO_MODEL_LABEL = "Auto";
