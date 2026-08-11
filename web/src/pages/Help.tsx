/* SPDX-License-Identifier: Apache-2.0 */
/**
 * Help — the in-app user reference.
 *
 * Single page that covers what new users need to know:
 *  - Slash commands (auto-rendered from PRESETS so it never drifts)
 *  - Keyboard shortcuts
 *  - MCP integrations chip row
 *  - Per-chat system prompt amendment
 *  - Troubleshooting (LM Studio connection, key_pruned, model selector empty,
 *    session expired, research mode appears stuck)
 *  - Privacy (local-first storage, no telemetry, provider-routed traffic)
 *
 * Pure presentation; no fetches. Renders inside AppShell.
 */
import { Link } from "react-router-dom";
import { AppShell } from "@/components/AppShell";
import { PRESETS } from "@/lib/presets";
import { BUILTIN_COMMANDS } from "@/components/SlashMenu";
import { SHORTCUTS } from "@/components/KeyboardHelp";
import { usePlatform } from "@/hooks/usePlatform";
import { formatShortcut } from "@/lib/formatShortcut";
import "@/styles/help.css";

const PRESET_SLASH_NAMES: Record<string, string> = {
  general: "/general",
  research: "/research",
  coder: "/code",
  creative: "/write",
  analyst: "/analyze",
  architect: "/architect",
};

interface TroubleRow {
  symptom: string;
  cause: string;
  fix: string;
}

const TROUBLE: TroubleRow[] = [
  {
    symptom: "LM Studio not connected — model selector is empty",
    cause:
      "The backend can't reach LM Studio at the configured base URL, or your API key isn't set / isn't accepted.",
    fix: "Open Settings → LM Studio. Verify the base URL (default http://localhost:1234 if LM Studio is on the same machine; use http://host.docker.internal:1234 if LM Chat is in Docker). Paste your LM Studio API key. Press Test connection — you should see your loaded models. Then Save.",
  },
  {
    symptom: "Banner: \"LM Studio API key was cleared by a secret rotation\"",
    cause:
      "LM Chat encrypts saved API keys with LM_CHAT_SECRET. If that secret changed between restarts (common during dev), the stored key becomes undecryptable and is pruned at boot so the app fails loudly instead of returning 401 silently.",
    fix: "Open Settings → LM Studio and re-paste your LM Studio API key. The banner clears once the new key is saved.",
  },
  {
    symptom: "Research / Code / Write mode appears stuck on \"Thinking…\"",
    cause:
      "Reasoning-heavy local models (qwen3.6, qwen3.5-122b, etc.) can take 30–120 seconds before emitting any output token while they plan tool calls. The sub-session UI shows \"Thinking…\" the entire time — this is normal, not a hang.",
    fix: "Wait. If you really need to stop, the Stop generation button in the composer ends the stream cleanly. To skip thinking entirely on long chains, switch to a lighter model in the picker (e.g. qwen3.5-9b).",
  },
  {
    symptom: "Session expired — kicked back to login mid-session",
    cause:
      "Sessions are HttpOnly + SameSite=Strict cookies. They expire after a fixed window or when the server restarts on a new LM_CHAT_SECRET.",
    fix: "Sign in again. Your chats are preserved.",
  },
  {
    symptom: "Models loaded in LM Studio don't appear in the model picker",
    cause:
      "LM Chat caches the model list. The cache refreshes on a successful Test connection in Settings, on login, and on a chat-page hard reload.",
    fix: "Open Settings → LM Studio → Test connection. Or refresh the chat page (Cmd+R).",
  },
  {
    symptom: "Slash command typed but nothing happens",
    cause:
      "The slash palette intercepts Enter to navigate. To both launch the sub-agent mode AND send the message in one keystroke, type the slash command followed by the message text and use ⌘+Enter (or Ctrl+Enter).",
    fix: "Example: \"/research what's the latest Vite release\" then ⌘+Enter. This launches a Research sub-session and sends the query in one shot.",
  },
  {
    symptom: "Tool calls keep retrying or timing out",
    cause:
      "MCP integration servers respond at their own pace, and some (like web fetch) hit external sites. Slow networks or rate-limited APIs amplify the wait.",
    fix: "Reduce active integrations to only the ones your task needs. The Tools chip row in the composer shows what's on; click a chip to toggle.",
  },
];

function Help() {
  const platform = usePlatform();
  const presetIds = ["general", "research", "coder", "creative", "analyst", "architect"];
  const slashCmds = BUILTIN_COMMANDS;

  return (
    <AppShell>
      <article className="help-page">
        <header className="help-header">
          <h1 className="help-h1">Help</h1>
          <p className="help-lead">
            Everything in one place. LM Chat is local-first: your chats, projects,
            and documents live in a local database on your machine, and there's
            no telemetry — it never phones home. Model requests go only to the
            providers you configure.
          </p>
        </header>

        {/* ─── Slash commands ──────────────────────────────────────────── */}
        <section className="help-section">
          <h2 className="help-h2">Slash commands</h2>
          <p className="help-p">
            Slash commands launch a transient sub-agent session for one
            exchange — clean context, focused system prompt, and sampling
            defaults tuned for the task. The sub-agent runs, injects a summary
            back into the chat, and exits; it does not change the chat's
            persistent persona. Type the slash, then your question; press
            ⌘+Enter to activate and send in one keystroke.
          </p>

          <h3 className="help-h3">Mode commands</h3>
          <div className="help-table">
            <div className="help-tr-header">
              <div className="help-th">Command</div>
              <div className="help-th">What it does</div>
            </div>
            {presetIds.map((id) => {
              const preset = PRESETS[id];
              if (preset === undefined) return null;
              return (
                <div key={id} className="help-tr">
                  <div className="help-td">
                    <code>{PRESET_SLASH_NAMES[id] ?? `/${id}`}</code>
                  </div>
                  <div className="help-td-desc">
                    <strong>{preset.label}</strong>
                    <br />
                    <span className="help-dim">
                      Sampling: temperature {preset.temperature ?? "default"}
                    </span>
                  </div>
                </div>
              );
            })}
          </div>

          <h3 className="help-h3">Utility commands</h3>
          <div className="help-table">
            <div className="help-tr-header">
              <div className="help-th">Command</div>
              <div className="help-th">What it does</div>
            </div>
            {slashCmds
              .filter((c) => !(c.name in PRESET_SLASH_NAMES))
              .map((c) => (
                <div key={c.name} className="help-tr">
                  <div className="help-td">
                    <code>/{c.name}</code>
                  </div>
                  <div className="help-td-desc">{c.description}</div>
                </div>
              ))}
          </div>
        </section>

        {/* ─── Keyboard shortcuts ──────────────────────────────────────── */}
        <section className="help-section">
          <h2 className="help-h2">Keyboard shortcuts</h2>
          <div className="help-table">
            <div className="help-tr-header">
              <div className="help-th">Key</div>
              <div className="help-th">Action</div>
            </div>
            {SHORTCUTS.map((s) => {
              const label =
                s.chord !== null ? formatShortcut(platform, s.chord) : (s.literal ?? "");
              return (
                <div key={label} className="help-tr">
                  <div className="help-td">
                    <code>{label}</code>
                  </div>
                  <div className="help-td-desc">{s.description}</div>
                </div>
              );
            })}
          </div>
        </section>

        {/* ─── MCP integrations ────────────────────────────────────────── */}
        <section className="help-section">
          <h2 className="help-h2">MCP integrations (tools)</h2>
          <p className="help-p">
            The chip row above the composer toggles which MCP tools the model
            can call this turn. How tools execute depends on which provider
            you're using. With LM Studio, MCP servers execute server-side
            inside LM Studio itself via the <code>integrations</code> field —
            LM Chat just forwards the selected integration ids. With a cloud
            provider, LM Chat runs a local MCP host that executes the same
            servers on your machine and relays the results, so cloud models
            get tool access too. Common tools: web search (searxng), web
            scraping (crawl4ai, firecrawl), library docs (context7, deepwiki),
            file system, sequential thinking, paper search.
          </p>
          <p className="help-p">
            Admins curate the available list in Settings → Integrations. A
            normal user picks from that admin-curated set; ids that aren't on
            the allowlist are rejected at the route boundary as a defense
            against prompt injection.
          </p>
        </section>

        {/* ─── Per-chat system prompt ─────────────────────────────────── */}
        <section className="help-section">
          <h2 className="help-h2">Per-chat system prompt</h2>
          <p className="help-p">
            Every chat assembles its system prompt from three layers, in this
            order:
          </p>
          <ol className="help-ol">
            <li>
              The chat's persona prompt, set by the rail picker (General /
              Research / Code / etc.) — the persistent system prompt for this
              chat.
            </li>
            <li>
              The project's prompt, if the chat lives in a project — adds
              project-specific context.
            </li>
            <li>
              Your per-chat amendment — your own instructions for THIS chat
              only. Optional.
            </li>
          </ol>
          <p className="help-p">
            Open the chat settings rail (top-bar menu → Chat settings) to
            inspect the assembled prompt and add your amendment.
          </p>
        </section>

        {/* ─── Troubleshooting ────────────────────────────────────────── */}
        <section className="help-section">
          <h2 className="help-h2">Troubleshooting</h2>
          <dl className="help-dl">
            {TROUBLE.map((t) => (
              <div key={t.symptom} className="help-trouble-row">
                <dt className="help-dt">{t.symptom}</dt>
                <dd className="help-dd">
                  <div className="help-dd-why">
                    <strong>Why:</strong> {t.cause}
                  </div>
                  <div className="help-dd-fix">
                    <strong>Fix:</strong> {t.fix}
                  </div>
                </dd>
              </div>
            ))}
          </dl>
        </section>

        {/* ─── Privacy ─────────────────────────────────────────────────── */}
        <section className="help-section">
          <h2 className="help-h2">Privacy</h2>
          <ul className="help-ul">
            <li>
              Your chats are stored in a local SQLite database on the machine
              running LM Chat. No remote database.
            </li>
            <li>
              Model requests go only to the providers you configure. With just
              LM Studio that's your own machine. If you add a cloud provider
              (OpenAI, OpenRouter, Groq, or any OpenAI-compatible endpoint),
              prompts for that provider's models are sent to that provider —
              exactly like any API client. Nothing is sent anywhere you didn't
              configure.
            </li>
            <li>
              MCP tools call external services on your behalf (web search,
              docs lookup, etc.). With a local model, those calls run inside
              your LM Studio process. With a cloud model, LM Chat's own
              built-in MCP host runs the same tools directly on your machine
              and relays the results. Either way, the calls originate
              locally — there's no LM Chat server for them to route through.
            </li>
            <li>
              There's no telemetry, no analytics beacon, no usage upload.
              Server-side logs are local to your install.
            </li>
            <li>
              API keys are encrypted at rest with an envelope keyed off
              <code>LM_CHAT_SECRET</code>. Rotating that secret invalidates
              stored keys (they're pruned at boot and you're prompted to
              re-save).
            </li>
          </ul>
        </section>

        {/* ─── Where to find more ─────────────────────────────────────── */}
        <section className="help-section">
          <h2 className="help-h2">More</h2>
          <p className="help-p">
            Architectural depth, deployment details, and the full user guide
            are available in the in-app{" "}
            <Link to="/docs">documentation reader</Link>. Start with the{" "}
            <Link to="/docs/00-quickstart">Quickstart</Link> or browse by
            topic.
          </p>
          <p className="help-p">
            Back to the app: <Link to="/">chats</Link> ·{" "}
            <Link to="/settings/lm-studio">LM Studio settings</Link> ·{" "}
            <Link to="/memory">memory</Link> ·{" "}
            <Link to="/documents">documents</Link>.
          </p>
        </section>
      </article>
    </AppShell>
  );
}

export default Help;
