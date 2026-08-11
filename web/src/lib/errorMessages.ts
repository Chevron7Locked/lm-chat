/* SPDX-License-Identifier: Apache-2.0 */
/**
 * humanizeApiError — convert raw API / SSE error payloads to readable copy.
 *
 * The backend signals failures in three shapes, all of which we have to
 * accept here:
 *
 *   1) `ApiError` (REST): { status, detail }
 *   2) SSE error frame: { code, message }
 *   3) Raw JSON serialised into `.message` by the SSE error path when the
 *      backend returns a structured `{code, ...}` body with no separate
 *      `detail` key — e.g. `'{"code":"stream_in_progress","chat_id":1}'`.
 *
 * This module returns `{ title, body, hint? }` so callers (error banner,
 * inline composer notice, toast) can render the same content in their
 * own layout.  Unknown codes/messages fall through to a sensible default
 * that preserves the original text but strips JSON noise.
 */

export interface HumanError {
  /** Short, headline-style summary. */
  title: string;
  /** Plain-language explanation of what went wrong. */
  body: string;
  /** Optional next-step nudge (e.g. "Open Settings → LM Studio"). */
  hint?: string;
}

type Input =
  | { code?: string | null; message?: string | null; status?: number | null }
  | string
  | null
  | undefined;

const KNOWN: Record<string, HumanError> = {
  stream_in_progress: {
    title: "Another response is already streaming",
    body: "Wait for the current reply to finish, or stop it from the composer before sending again.",
  },
  no_models_loaded: {
    title: "LM Studio has no model loaded",
    body: "Open LM Studio and load a model, then try again.",
    hint: "Settings → LM Studio → Test connection to verify.",
  },
  model_not_loaded: {
    title: "Selected model isn't loaded",
    body: "Pick a different model from the header, or load this one in LM Studio.",
  },
  lmstudio_unreachable: {
    title: "LM Studio is unreachable",
    body: "Check that LM Studio is running and the configured base URL + API key are correct.",
    hint: "Settings → LM Studio → Test connection.",
  },
  rate_limited: {
    title: "Too many requests",
    body: "You're sending faster than the server allows. Wait a moment and try again.",
  },
  context_window_exceeded: {
    title: "Conversation is too long for this model",
    body: "Trim earlier messages, switch to a model with a larger context window, or start a new chat.",
  },
  invalid_api_key: {
    title: "LM Studio rejected the API key",
    body: "The configured key doesn't match LM Studio's current value.",
    hint: "Settings → LM Studio → API key.",
  },
  fetch_error: {
    title: "Network error talking to LM Chat",
    body: "Lost the connection to the backend. Check your network and retry.",
  },
  no_body: {
    title: "Empty response from server",
    body: "The backend replied without a body. Retry; if it persists, check server logs.",
  },
  mtp_suspected: {
    title: "Possible MTP misbehavior",
    body: "This model's draft-MTP may be misbehaving on long tool chains. If this happens often, try disabling MTP in LM Studio's model load config.",
  },
};

const STATUS_DEFAULTS: Record<number, HumanError> = {
  401: {
    title: "You're signed out",
    body: "Sign in again to continue.",
  },
  403: {
    title: "Not allowed",
    body: "You don't have permission for this action.",
  },
  404: {
    title: "Not found",
    body: "The thing you tried to reach doesn't exist (any more).",
  },
  408: {
    title: "Request timed out",
    body: "The server took too long to respond. Try again.",
  },
  413: {
    title: "Upload too large",
    body: "The file or message exceeds the server's limit.",
  },
  429: {
    title: "Too many requests",
    body: "You're sending faster than the server allows. Wait a moment and try again.",
  },
  500: {
    title: "Server error",
    body: "Something broke on the backend. Try again; check server logs if it persists.",
  },
  502: {
    title: "Upstream unavailable",
    body: "LM Chat couldn't reach the model provider. Check that your configured provider is running and reachable.",
  },
  503: {
    title: "Service unavailable",
    body: "The backend is temporarily unavailable. Try again in a moment.",
  },
  504: {
    title: "Upstream timed out",
    body: "The model provider didn't respond in time — it may be loading a model or under load.",
  },
};

/**
 * Parse a string that *might* be a JSON envelope like
 * `'{"code":"...","chat_id":1}'`.  Returns null if it isn't.
 */
function tryParseJsonEnvelope(
  s: string,
): { code?: string; message?: string; detail?: string } | null {
  const trimmed = s.trim();
  if (!trimmed.startsWith("{") || !trimmed.endsWith("}")) return null;
  try {
    const parsed = JSON.parse(trimmed) as Record<string, unknown>;
    const out: { code?: string; message?: string; detail?: string } = {};
    if (typeof parsed.code === "string") out.code = parsed.code;
    if (typeof parsed.message === "string") out.message = parsed.message;
    if (typeof parsed.detail === "string") out.detail = parsed.detail;
    return out;
  } catch {
    return null;
  }
}

export function humanizeApiError(input: Input): HumanError {
  if (input === null || input === undefined) {
    return {
      title: "Something went wrong",
      body: "No error details were provided. Try again or check the server logs.",
    };
  }

  // String form — try to parse out a JSON envelope first.
  if (typeof input === "string") {
    const envelope = tryParseJsonEnvelope(input);
    if (envelope) {
      return humanizeApiError(envelope);
    }
    return {
      title: "Something went wrong",
      body: input.length === 0 ? "Something went wrong." : input,
    };
  }

  // Object form — first look up the code.
  if (input.code !== null && input.code !== undefined && input.code !== "") {
    const hit = KNOWN[input.code];
    if (hit) return hit;

    // `http_<n>` codes (e.g. "http_401") come from the SSE error path and
    // carry the numeric HTTP status in the suffix. STATUS_DEFAULTS is keyed
    // on numeric status, so parse the suffix and look it up.  This fixes:
    // stream 401 → "You're signed out / Sign in again" instead of the
    // generic "Something went wrong" fallback.
    const httpMatch = /^http_(\d{3})$/.exec(input.code);
    if (httpMatch) {
      const numStatus = Number(httpMatch[1]);
      const byHttpCode = STATUS_DEFAULTS[numStatus];
      if (byHttpCode) return byHttpCode;
    }
  }

  // Status code fallback.
  if (input.status !== null && input.status !== undefined) {
    const byStatus = STATUS_DEFAULTS[input.status];
    if (byStatus) return byStatus;
  }

  // The message may itself be a JSON envelope (the SSE error path stuffs
  // the raw HTTP body into `.message`).  Recurse so we collapse it.
  if (
    input.message !== null &&
    input.message !== undefined &&
    input.message !== ""
  ) {
    const envelope = tryParseJsonEnvelope(input.message);
    if (envelope && (envelope.code ?? envelope.detail ?? envelope.message)) {
      return humanizeApiError(envelope);
    }
    return {
      title: "Something went wrong",
      body: input.message,
    };
  }

  return {
    title: "Something went wrong",
    body: "No error details were provided. Try again or check the server logs.",
  };
}
