/* SPDX-License-Identifier: Apache-2.0 */
/**
 * SharePublic — read-only public view of a shared chat.
 *
 * Mounted at /share/:token in the router; UNAUTHENTICATED — no session
 * cookie required.  Fetches GET /api/share/:token and renders the
 * resulting conversation with the same `ChatMessage` component the
 * authenticated Chat view uses, plus a sticky public-view banner that
 * makes the read-only posture obvious.
 *
 * Privacy invariant: the backend refuses to mint share tokens
 * for incognito chats, and the GET /api/share/:token handler does a
 * defensive second check on read.  This page therefore never has to
 * worry about leaking incognito content — a known token plus an
 * incognito flag means a 404 from the backend, which renders as
 * "Share not found" here.
 */
import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import { api } from "@/lib/api";
import { ChatMessage } from "@/components/ChatMessage";
import type { MessageRole } from "@/components/ChatMessage";
import "@/styles/share-public.css";

interface PublicMessage {
  id: number;
  role: string;
  content: string;
  reasoning_content: string | null;
  created_at: string;
}

interface PublicShareView {
  title: string;
  created_at: string;
  messages: PublicMessage[];
}

type LoadState =
  | { kind: "loading" }
  | { kind: "ok"; data: PublicShareView }
  | { kind: "not_found" }
  | { kind: "error"; message: string };

// ─── Component ──────────────────────────────────────────────────────────────

export default function SharePublic() {
  const params = useParams<{ token: string }>();
  const token = params.token ?? "";
  const [state, setState] = useState<LoadState>({ kind: "loading" });

  useEffect(() => {
    const ctl = { cancelled: false };
    if (token === "") {
      setState({ kind: "not_found" });
      return () => {
        /* noop */
      };
    }
    void (async () => {
      try {
        const data = await api.request<PublicShareView>(
          `/api/share/${encodeURIComponent(token)}`,
        );
        if (!ctl.cancelled) setState({ kind: "ok", data });
      } catch (err) {
        if (ctl.cancelled) return;
        const status = (err as { status?: number }).status;
        if (status === 404) {
          setState({ kind: "not_found" });
        } else {
          const msg = err instanceof Error ? err.message : "Unknown error";
          setState({ kind: "error", message: msg });
        }
      }
    })();
    return () => {
      ctl.cancelled = true;
    };
  }, [token]);

  if (state.kind === "loading") {
    return (
      <div className="sp-page">
        <div className="sp-status" data-testid="share-loading">
          <p className="sp-status-copy">Loading shared chat…</p>
        </div>
      </div>
    );
  }

  if (state.kind === "not_found") {
    return (
      <div className="sp-page">
        <div className="sp-status" data-testid="share-not-found">
          <p className="sp-status-title">Link not found</p>
          <p className="sp-status-copy">
            This share link has expired, been revoked, or never existed.
          </p>
        </div>
      </div>
    );
  }

  if (state.kind === "error") {
    return (
      <div className="sp-page">
        <div className="sp-status" data-testid="share-error">
          <p className="sp-status-copy">
            Couldn't load shared chat: {state.message}
          </p>
        </div>
      </div>
    );
  }

  const { data } = state;

  // Format share metadata for the eyebrow
  const sharedAt = new Date(data.created_at);
  const eyebrow = `Shared conversation · ${sharedAt.toLocaleDateString(undefined, { year: "numeric", month: "long", day: "numeric" })}`;

  return (
    <div className="sp-page">
      <header className="sp-header" role="banner">
        <span className="sp-eyebrow" data-testid="share-public-banner">
          {eyebrow}
        </span>
        <h1 className="sp-title" data-testid="share-title">
          {data.title}
        </h1>
      </header>

      <main className="sp-messages" aria-label="Shared messages">
        {data.messages.length === 0 ? (
          <div className="sp-empty">This chat has no messages yet.</div>
        ) : (
          data.messages.map((m) => (
            <ChatMessage
              key={m.id}
              message={{
                id: m.id,
                role: m.role as MessageRole,
                content: m.content,
                reasoning_content: m.reasoning_content ?? undefined,
              }}
            />
          ))
        )}
      </main>

      <footer className="sp-footer">
        <p className="sp-attribution">
          Powered by{" "}
          <a href="/" aria-label="LM Chat home">
            LM Chat
          </a>
        </p>
      </footer>
    </div>
  );
}
