import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import SourceBadge from "./SourceBadge";

/**
 * Who decided a field's value (REQ-064, CR-123).
 *
 * The distinction this badge draws is the one people get wrong in the
 * dangerous direction: a value *you* typed has to look different from one the
 * model guessed, or re-running AI review and finding your correction still
 * there reads as the review having failed. It is four lines of mapping and it
 * is the only thing standing between "you set this" and "AI set this", so it
 * is worth pinning by name rather than by whether it renders.
 */

afterEach(cleanup);

describe("SourceBadge", () => {
  it("names each source in words, not by colour alone", () => {
    render(<SourceBadge source="human" />);
    expect(screen.getByText("you set this")).toBeTruthy();
    cleanup();

    render(<SourceBadge source="rule" />);
    expect(screen.getByText("a rule set this")).toBeTruthy();
    cleanup();

    render(<SourceBadge source="ai" />);
    expect(screen.getByText("AI set this")).toBeTruthy();
    cleanup();

    render(<SourceBadge source="file" />);
    expect(screen.getByText("from the file")).toBeTruthy();
  });

  it("says nothing at all when nobody has claimed the field", () => {
    // Every document older than Phase 17 has no row here, and guessing "ai"
    // for those would be a claim the archive cannot support.
    const { container } = render(<SourceBadge source={undefined} />);
    expect(container.textContent).toBe("");
  });

  it("says nothing for a source it does not recognise", () => {
    // The server's enum can grow before this map does; an unknown source must
    // not throw on the edit panel, which is the screen you reach for when
    // something is already wrong.
    const { container } = render(
      <SourceBadge source={"import" as unknown as "human"} />,
    );
    expect(container.textContent).toBe("");
  });

  it("dates the claim in the tooltip when it knows when", () => {
    render(<SourceBadge source="human" when="2026-04-11T09:30:00Z" />);
    expect(screen.getByTitle("you set this on 2026-04-11")).toBeTruthy();
  });
});
