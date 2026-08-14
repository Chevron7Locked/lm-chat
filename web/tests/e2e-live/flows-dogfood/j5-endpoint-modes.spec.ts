/* SPDX-License-Identifier: Apache-2.0 */
/**
 * J5 — both LM Studio endpoint modes (native / openai_compat) reach a
 * terminal against a REAL model.
 *
 * Deliberately lightweight: a trivial turn per mode, asserting only that the
 * stream reaches its terminal (composer re-enables) — not a full grey-box
 * check like J2. Each mode gets its own fresh chat so neither carries
 * server-side chain state (previous_response_id) across the mode switch.
 */
import { test, expect } from "../_fixtures";
import {
  attachErrorCollector,
  assertNoConsoleErrors,
  loginAndWait,
  createChatViaRequest,
} from "../flows/_flow-helpers";
import {
  classifyFleet,
  configureLmStudio,
  pinBackgroundModel,
  sendTurnAndWait,
} from "./_dogfood-helpers";

const TURN_TIMEOUT_MS = 1_800_000;
const ENDPOINT_MODES = ["native", "openai_compat"] as const;

test(
  "j5: native and openai_compat endpoint modes both reach a terminal",
  async ({ page, backendURL, adminUsername, adminPassword }) => {
    test.setTimeout(3_600_000);
    const collectErrors = attachErrorCollector(page);

    await loginAndWait(page, backendURL, adminUsername, adminPassword);
    const fleet = await classifyFleet(page, backendURL);
    await configureLmStudio(page, backendURL, fleet.fastId);
    await pinBackgroundModel(page, backendURL, fleet.fastId);

    for (const mode of ENDPOINT_MODES) {
      const resp = await page.request.patch(
        `${backendURL}/api/settings/lmstudio/endpoint-mode`,
        { data: { endpoint_mode: mode } },
      );
      expect(
        resp.ok(),
        `endpoint-mode=${mode} → HTTP ${resp.status()}`,
      ).toBe(true);

      const chatId = await createChatViaRequest(page, backendURL, `J5 ${mode}`);
      await page.goto(`${backendURL}/chats/${String(chatId)}`);
      await page.waitForFunction(
        () => {
          const sel = document.querySelector<HTMLSelectElement>(
            '[data-testid="chat-header-model-select"]',
          );
          if (sel === null) return false;
          return sel.value !== "" && sel.options.length > 1;
        },
        null,
        { timeout: 30_000 },
      );

      // sendTurnAndWait already awaits the composer re-enabling at the
      // stream's terminal — reaching the next iteration/assertion IS the
      // "reached a terminal" check for this mode.
      await sendTurnAndWait(page, "Say hi in one word.", TURN_TIMEOUT_MS);
    }

    assertNoConsoleErrors(collectErrors(), "j5");
  },
);
