/**
 * Code-fence regression test.
 *
 * Purpose: verify that react-markdown 10.1.0 + rehype-highlight 7.0.2 renders
 * fenced code blocks correctly WITHOUT the v0.5.x extract-then-restore
 * workaround. The v0.5.1 input that triggered the original bug was a fence
 * containing a nested backtick sequence. If this test passes without the
 * workaround, the bug is fixed upstream.
 *
 * If this test fails: add the workaround and document that the bug is still
 * present (per PLAN §P7b code-fence gating strategy).
 */
import { describe, it, expect } from "vitest";
import { render } from "@testing-library/react";
import { ChatMessage } from "@/components/ChatMessage";

describe("ChatMessage code-fence rendering", () => {
  it("renders a fenced TypeScript block without content corruption", () => {
    const content = "```typescript\nconst x: number = 42;\nconsole.log(x);\n```";
    const { container } = render(
      <ChatMessage
        message={{
          id: 1,
          role: "assistant",
          content,
        }}
      />
    );

    // The raw source text should appear in the rendered output.
    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    expect(pre?.textContent).toContain("const x");
    expect(pre?.textContent).toContain("console.log");
    // The content should NOT contain the raw backtick fence markers.
    expect(pre?.textContent).not.toContain("```");
  });

  it("renders nested backticks in code without corruption (v0.5.1 regression)", () => {
    // The v0.5.1 trigger: a code block containing backtick-quoted text inside.
    const content = "```python\nx = `hello`\nprint(x)\n```";
    const { container } = render(
      <ChatMessage
        message={{
          id: 2,
          role: "assistant",
          content,
        }}
      />
    );

    const pre = container.querySelector("pre");
    expect(pre).not.toBeNull();
    // Content should be present intact.
    expect(pre?.textContent).toContain("x =");
    expect(pre?.textContent).toContain("hello");
    expect(pre?.textContent).toContain("print(x)");
    // Fence markers must not leak into rendered text.
    expect(pre?.textContent).not.toContain("```");
  });

  it("renders inline code without a pre element", () => {
    const content = "Use `const` not `var`.";
    const { container } = render(
      <ChatMessage
        message={{
          id: 3,
          role: "assistant",
          content,
        }}
      />
    );

    // No pre element for inline code.
    expect(container.querySelector("pre")).toBeNull();
    const codes = container.querySelectorAll("code");
    expect(codes.length).toBeGreaterThanOrEqual(2);
  });

  it("renders language label for language-annotated fences", () => {
    const content = "```bash\nls -la\n```";
    const { container } = render(
      <ChatMessage
        message={{
          id: 4,
          role: "assistant",
          content,
        }}
      />
    );
    // CodeBlock renders a language label span when lang is known.
    expect(container.textContent).toContain("bash");
    expect(container.textContent).toContain("ls -la");
  });

  it("renders user messages as plain text, not markdown", () => {
    const content = "# Not a heading\n\n**not bold**";
    const { container } = render(
      <ChatMessage
        message={{
          id: 5,
          role: "user",
          content,
        }}
      />
    );

    // User messages are plain <p> not parsed markdown.
    expect(container.querySelector("h1")).toBeNull();
    expect(container.querySelector("strong")).toBeNull();
    expect(container.textContent).toContain("# Not a heading");
  });
});
