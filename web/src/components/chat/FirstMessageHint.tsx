/* SPDX-License-Identifier: Apache-2.0 */
import { useViewport } from "@/hooks/useViewport";

// Light hint shown inside an open chat that has no messages
// yet — more inviting than the bare "Send a message…" line.  The legacy
// "Send a message to start the conversation." line is retained as the card
// headline so chat.spec.ts's /Send a message/i visibility assertion keeps
// passing.
export function FirstMessageHint() {
  const { isMobile } = useViewport();
  return (
    <div className="lmchat-first-msg-wrap">
      <div
        className="lmchat-first-msg-card"
        data-testid="chat-first-message-hint"
      >
        {/* Copper marginalia dot left of title on mobile */}
        <p
          className={
            isMobile
              ? "lmchat-first-msg-title lmchat-first-msg-title--mobile"
              : "lmchat-first-msg-title"
          }
        >
          Send a message to begin.
        </p>
        <p className="lmchat-first-msg-copy">
          Ask a question, paste code, or attach a document. The model streams as
          it thinks — pin anything worth keeping.
        </p>
        {/* Book-design asterism — textural pause, Hanken italic */}
        <p className="lmchat-first-msg-asterism">* * *</p>
      </div>
    </div>
  );
}
