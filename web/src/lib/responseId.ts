/* SPDX-License-Identifier: Apache-2.0 */
// ─── Response-ID persistence ─────────────────────────────────────────────────

const LS_RID_PREFIX = "lmchat:sse";

export function storeResponseId(chatId: number, rid: string): void {
  try {
    localStorage.setItem(`${LS_RID_PREFIX}:${String(chatId)}:rid`, rid);
  } catch {
    // Ignore.
  }
}

export function loadResponseId(chatId: number): string | null {
  try {
    return localStorage.getItem(`${LS_RID_PREFIX}:${String(chatId)}:rid`);
  } catch {
    return null;
  }
}

export function clearResponseId(chatId: number): void {
  try {
    localStorage.removeItem(`${LS_RID_PREFIX}:${String(chatId)}:rid`);
  } catch {
    // Ignore.
  }
}
