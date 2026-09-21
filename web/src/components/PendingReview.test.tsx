import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { LiveProvider } from "../live/LiveProvider";
import PendingReviewPanel from "./PendingReview";

/**
 * A refusal is an answer, not a queue (ADR-011, BND-FR-005).
 *
 * The panel's whole job is to tell apart situations that look identical on screen.
 * A document the model declined used to land in "Waiting for AI review", under a
 * heading saying nothing had failed, beside a button offering to run the review that
 * had already happened — which would reach the same refusal, for money, every time.
 */

vi.mock("../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api")>()),
  api: {
    pendingReview: vi.fn(),
    reclassify: vi.fn(),
  },
}));

const { api } = await import("../api");
const pendingMock = vi.mocked(api.pendingReview);

function reason(code: string, count: number, rerunnable: boolean) {
  return {
    code: code as "never_attempted",
    label: code,
    detail: `detail for ${code}`,
    count,
    rerunnable,
    document_ids: [],
  };
}

function renderPanel() {
  return render(
    <LiveProvider>
      <PendingReviewPanel />
    </LiveProvider>,
  );
}

afterEach(cleanup);

describe("the pending-review panel", () => {
  beforeEach(() => vi.clearAllMocks());

  it("offers no re-run for a bucket the model has already refused", async () => {
    pendingMock.mockResolvedValue({
      total: 4,
      reasons: [reason("never_attempted", 3, true), reason("declined", 1, false)],
    });
    renderPanel();

    // Both buckets are still reported — a refused document has no title, date or
    // tags, so hiding it would shrink the archive's own count of what is unreviewed.
    expect(await screen.findByText("declined")).toBeTruthy();
    expect(screen.getByText("4 documents without AI review")).toBeTruthy();

    expect(screen.getByRole("button", { name: /Run AI review on these 3/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Run AI review on these 1/ })).toBeNull();
  });

  it("counts only what a re-run could reach in the all button", async () => {
    // The button used to promise `pending.total`, which includes the refusals —
    // a number it could not deliver, on the screen whose purpose is being believed.
    pendingMock.mockResolvedValue({
      total: 9,
      reasons: [
        reason("never_attempted", 3, true),
        reason("failed", 2, true),
        reason("declined", 4, false),
      ],
    });
    renderPanel();

    expect(await screen.findByRole("button", { name: /Run AI review on all 5/ })).toBeTruthy();
    expect(screen.queryByRole("button", { name: /Run AI review on all 9/ })).toBeNull();
  });

  it("still says nothing at all when nothing is waiting", async () => {
    pendingMock.mockResolvedValue({ total: 0, reasons: [] });
    const { container } = renderPanel();
    await vi.waitFor(() => expect(pendingMock).toHaveBeenCalled());
    expect(container.textContent).toBe("");
  });
});
