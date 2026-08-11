/**
 * DetachedFromProjectSeparator unit tests.
 *
 * PROJECTS-V1 additions Phase 11. Pins:
 *  - renders name as a clickable link when projectExists=true
 *  - renders name as plain text when projectExists=false
 *  - includes the formatted detached_at date
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

import {
  DetachedFromProjectSeparator,
  type DetachedFromProjectMeta,
} from "@/components/DetachedFromProjectSeparator";

const META: DetachedFromProjectMeta = {
  project_id: 42,
  name: "MyProject",
  // 2026-06-05 12:00 UTC
  detached_at: 1780500000,
  system_prompt_hash: "sha256:deadbeef",
};

function wrap(node: React.ReactNode) {
  return render(<MemoryRouter>{node}</MemoryRouter>);
}

describe("DetachedFromProjectSeparator", () => {
  it("renders the project name as a link when projectExists=true", () => {
    wrap(
      <DetachedFromProjectSeparator meta={META} projectExists={true} />,
    );
    const link = screen.getByTestId("detached-project-link");
    expect(link.tagName).toBe("A");
    expect(link.getAttribute("href")).toBe("/project/42");
    expect(link.textContent).toBe("MyProject");
  });

  it("renders the project name as plain text when projectExists=false", () => {
    wrap(
      <DetachedFromProjectSeparator meta={META} projectExists={false} />,
    );
    expect(screen.getByTestId("detached-project-plain").textContent).toBe(
      "MyProject",
    );
    expect(
      screen.queryByTestId("detached-project-link"),
    ).toBeNull();
  });

  it("includes a date label derived from detached_at", () => {
    wrap(
      <DetachedFromProjectSeparator meta={META} projectExists={true} />,
    );
    const separator = screen.getByTestId(
      "detached-from-project-separator",
    );
    // The date is locale-dependent, but the year must always appear.
    expect(separator.textContent).toMatch(/2026/);
  });

  it("falls back to 'project #id' when the snapshot name is empty", () => {
    wrap(
      <DetachedFromProjectSeparator
        meta={{ ...META, name: "" }}
        projectExists={true}
      />,
    );
    expect(
      screen.getByTestId("detached-project-link").textContent,
    ).toBe("project #42");
  });
});
