/* SPDX-License-Identifier: Apache-2.0 */
import "@/styles/code-block.css";
/**
 * CodeBlock — syntax-highlighted code fence with copy button.
 *
 * Used as the custom `code` component for react-markdown's `components` prop.
 * Inline code (no language class) renders as a plain <code> element.
 * Fenced blocks (`language-*` class) get rehype-highlight styling + a copy
 * button using the Clipboard API.
 */
import { useRef, useState } from "react";
import type { ReactNode } from "react";
import type { ExtraProps } from "react-markdown";
import type { Element } from "hast";
import { Check, Copy } from "lucide-react";

interface CodeBlockProps extends ExtraProps {
  className?: string | undefined;
  children?: ReactNode | undefined;
}

export function CodeBlock({ className, children, node }: CodeBlockProps) {
  // react-markdown passes class="language-<lang>" on fenced code blocks.
  const match = /language-(\w+)/.exec(className ?? "");
  const isBlock = Boolean(match);
  const lang = match?.[1] ?? "";

  // For inline code, render a simple <code> element.
  if (!isBlock) {
    return <code className="lmchat-inline-code">{children}</code>;
  }

  // Fenced block: extract raw string content from node for the copy button.
  const rawText = extractRawText(node);

  return (
    <CodeFence lang={lang} rawText={rawText} className={className}>
      {children}
    </CodeFence>
  );
}

// ─── Fenced block ───────────────────────────────────────────────────────────

interface FenceProps {
  lang: string;
  rawText: string;
  className?: string | undefined;
  children?: ReactNode | undefined;
}

function CodeFence({ lang, rawText, className, children }: FenceProps) {
  const [copied, setCopied] = useState(false);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  function handleCopy(): void {
    navigator.clipboard.writeText(rawText).then(
      () => {
        setCopied(true);
        if (copyTimerRef.current !== null) clearTimeout(copyTimerRef.current);
        copyTimerRef.current = setTimeout(() => {
          setCopied(false);
        }, 2_500);
      },
      () => {
        // Clipboard write failed (permission denied / insecure context) — no-op.
      },
    );
  }

  return (
    <div className="lmchat-code-fence">
      {/* Header: language label + copy button */}
      <div className="lmchat-code-header">
        {lang !== "" && <span className="lmchat-code-lang-label">{lang}</span>}
        <button
          type="button"
          onClick={handleCopy}
          aria-label={copied ? "Copied!" : "Copy code"}
          className={`lmchat-code-copy${copied ? " lmchat-code-copy--copied" : ""}`}
        >
          {copied ? (
            <>
              <Check size={12} aria-hidden /> Copied
            </>
          ) : (
            <>
              <Copy size={12} aria-hidden /> Copy
            </>
          )}
        </button>
      </div>

      {/* Code content — rendered by rehype-highlight via className passthrough */}
      <pre className="lmchat-code-pre">
        <code className={className} style={{ display: "block" }}>
          {children}
        </code>
      </pre>
    </div>
  );
}

// ─── Helpers ────────────────────────────────────────────────────────────────

type HastNode =
  | Element
  | { type: string; value?: string; children?: HastNode[] };

/** Extract the plain text content from the hast node for clipboard. */
function extractRawText(node: Element | undefined): string {
  if (node === undefined) return "";
  let text = "";
  function walk(n: HastNode): void {
    if (n.type === "text" && "value" in n && typeof n.value === "string") {
      text += n.value;
    }
    if ("children" in n && Array.isArray(n.children)) {
      for (const child of n.children) {
        walk(child);
      }
    }
  }
  walk(node);
  return text;
}

// Styles moved to web/src/styles/code-block.css
