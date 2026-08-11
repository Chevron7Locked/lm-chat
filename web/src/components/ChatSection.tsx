/* SPDX-License-Identifier: Apache-2.0 */
/**
 * ChatSection — Settings → Chat tab.
 *
 * Houses the chat-specific defaults that were previously inlined into
 * Appearance (which now only carries true visual/theme prefs). Today
 * this surface is intentionally minimal — Default model selector —
 * with room to grow as the chat-baseline upgrade lands additional
 * per-account toggles (auto-regenerate, edit-confirm, followups-on).
 *
 * The default-model selector is now CONTROLLED: onChange fires
 * PUT /api/settings/lmstudio with the new default_model, then
 * invalidates the lmstudio-config query so the value persists
 * across the app.
 */
import { useCallback } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useChatModelOptions } from "@/hooks/useChatModelOptions";
import { useLmStudioConfig, lmStudioConfigKeys } from "@/hooks/useLmStudioConfig";
import { ModelSelectControl } from "@/components/ModelSelectControl";
import { api } from "@/lib/api";
import "@/styles/settings.css";

export function ChatSection() {
  const { options: chatModelOptions, groups: chatModelGroups, isLoading: modelsLoading } =
    useChatModelOptions();
  const { data: resolvedLmConfig } = useLmStudioConfig();
  const qc = useQueryClient();
  const defaultModelFallback = resolvedLmConfig?.default_model ?? "";

  const handleModelChange = useCallback(
    async (value: string): Promise<void> => {
      await api.request<unknown>("/api/settings/lmstudio", {
        method: "PUT",
        body: JSON.stringify({ default_model: value || null }),
      });
      await qc.invalidateQueries({ queryKey: lmStudioConfigKeys.resolved() });
    },
    [qc],
  );

  return (
    <div
      className="lmchat-section-container"
      data-testid="settings-chat-section"
    >
      <p className="lmchat-section-description">
        Defaults for new chats. Per-chat overrides via the model selector in the
        chat top bar take precedence over these choices.
      </p>

      <div className="lmchat-meta-block">
        <div className="lmchat-field-row">
          <span className="lmchat-field-row-label">Default model</span>
          {modelsLoading ? (
            <span className="lmchat-meta-value">Loading models…</span>
          ) : (
              <ModelSelectControl
                ariaLabel="Default model"
                value={defaultModelFallback}
                onChange={handleModelChange}
                className="lmchat-select"
                testId="settings-chat-default-model"
                placeholder="Select a model"
                options={chatModelOptions}
                {...(chatModelGroups.length > 1 ? { groups: chatModelGroups } : {})}
              />
          )}
        </div>
      </div>
    </div>
  );
}
