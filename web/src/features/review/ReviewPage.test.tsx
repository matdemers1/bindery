import { useEffect } from "react";
import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router";

import { LiveProvider, useLive, type Topic } from "../../live/LiveProvider";
import ReviewPage from "./ReviewPage";

/**
 * The review queue stays live (CR-085).
 *
 * This screen is the reason live updates were built: a mount-only load is what
 * let the badge and the queue disagree about work that had already been
 * accepted. The property worth holding is narrow and easy to lose in a
 * refactor — it refetches on a `review` hint, and it does not refetch on
 * everything else.
 */

// `review` is the call under test. `why` belongs to the provenance panel this
// screen renders beside the card — stubbed as never-settling so the panel stays
// on its own loading state instead of failing the test with its own error.
vi.mock("../../api", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../api")>()),
  api: {
    review: vi.fn(),
    why: vi.fn(() => new Promise(() => {})),
    pendingReview: vi.fn(),
  },
}));

const { api } = await import("../../api");
const reviewMock = vi.mocked(api.review);
const pendingMock = vi.mocked(api.pendingReview);

function queueOf(...titles: string[]) {
  return {
    total: titles.length,
    documents: titles.map((title, i) => ({
      id: `d${i}`,
      library_id: "lib",
      source_file_id: `f${i}`,
      page_start: 1,
      page_end: 1,
      title,
      summary: null,
      document_date: null,
      sensitivity: "normal",
      redundancy: "unique",
      review_state: "pending_classification",
      is_backlog: false,
      known_form_id: null,
      correspondent_id: null,
      document_type_id: null,
      created_at: "2026-01-01T00:00:00Z",
    })),
  };
}

/** Lets a test stand in for the server pushing a hint.
 *
 * The handle is stashed from an effect rather than during render: assigning to
 * an outer binding while rendering is a side effect, and React may render this
 * more than once.
 */
const live: { publish: (topics: Topic[]) => void } = { publish: () => {} };
function Publisher() {
  const { publishLocal } = useLive();
  useEffect(() => {
    live.publish = publishLocal;
  }, [publishLocal]);
  return null;
}

function renderQueue() {
  return render(
    <MemoryRouter>
      <LiveProvider>
        <Publisher />
        <ReviewPage />
      </LiveProvider>
    </MemoryRouter>,
  );
}

beforeEach(() => {
  // The screen fetches the unreviewed count beside the queue (D-05). Default it
  // to "nothing unreviewed" so the tests that are about live refetching are not
  // also asserting on the empty-state copy.
  pendingMock.mockResolvedValue({ total: 0, reasons: [] });
  // jsdom would attempt a real connection to ws://localhost/api/live and then
  // retry on a timer. The socket is not what is under test here; the hint is.
  vi.stubGlobal(
    "WebSocket",
    class {
      close() {}
    },
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

describe("ReviewPage", () => {
  it("shows what is waiting", async () => {
    reviewMock.mockResolvedValue(queueOf("Vehicle registration"));
    renderQueue();
    expect(await screen.findByText("Vehicle registration")).toBeTruthy();
  });

  it("says nothing is waiting rather than sitting on the loading state", async () => {
    reviewMock.mockResolvedValue(queueOf());
    renderQueue();
    expect(await screen.findByText("Nothing waiting for you.")).toBeTruthy();
  });

  it("refetches when the server says the review queue changed", async () => {
    reviewMock.mockResolvedValue(queueOf("Vehicle registration"));
    renderQueue();
    await screen.findByText("Vehicle registration");
    expect(reviewMock).toHaveBeenCalledTimes(1);

    // Somebody else accepted it, or the worker finished a classification.
    reviewMock.mockResolvedValue(queueOf("Insurance renewal"));
    live.publish(["review"]);

    expect(await screen.findByText("Insurance renewal")).toBeTruthy();
    expect(screen.queryByText("Vehicle registration")).toBeNull();
  });

  it("ignores a hint on a topic it did not ask for", async () => {
    // The failure this guards is the opposite one: subscribing to everything
    // turns each log line into a refetch of the queue.
    reviewMock.mockResolvedValue(queueOf("Vehicle registration"));
    renderQueue();
    await screen.findByText("Vehicle registration");
    expect(reviewMock).toHaveBeenCalledTimes(1);

    live.publish(["logs"]);
    live.publish(["settings"]);

    // Waited out, not polled. Hints are delivered on a trailing debounce, so a
    // `waitFor` asserting "still one call" is satisfied by the first check
    // before the refetch it is meant to catch could possibly have happened —
    // the assertion would pass for the wrong reason and guard nothing.
    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(reviewMock).toHaveBeenCalledTimes(1);
  });

  it("asks the server once and then stays quiet until something happens", async () => {
    // CLAUDE.md: no screen owns a polling timer. The failure this reproduces is
    // the one that actually shipped — a dependency-array feedback loop that
    // reached sixty requests a second — so it asserts on requests over elapsed
    // time rather than on whether a timer was created, which cannot tell this
    // screen's timers from the test harness's.
    reviewMock.mockResolvedValue(queueOf("Vehicle registration"));
    renderQueue();
    await screen.findByText("Vehicle registration");

    await new Promise((resolve) => setTimeout(resolve, 400));
    expect(reviewMock).toHaveBeenCalledTimes(1);
  });
});

describe("the empty queue tells the truth (D-05)", () => {
  beforeEach(() => {
    reviewMock.mockResolvedValue({ total: 0, documents: [] });
  });

  it("does not claim everything filed itself when documents were never reviewed", async () => {
    // The queue only holds what the gate declined to file. Documents the
    // classifier never reached never enter it — so an empty queue said nothing
    // about them, and the screen used to assert it did.
    pendingMock.mockResolvedValue({
      total: 33,
      reasons: [
        {
          code: "never_attempted",
          label: "Waiting for AI review",
          detail: "These arrived before an API key was configured.",
          count: 33,
          rerunnable: true,
          document_ids: [],
        },
      ],
    });
    renderQueue();

    expect(await screen.findByText(/33 documents have not been through AI review/)).toBeTruthy();
    expect(screen.queryByText(/Everything filed itself/)).toBeNull();
    expect(screen.getByRole("link", { name: /See why on Pipeline/ })).toBeTruthy();
  });

  it("says so plainly when the archive really has been reviewed", async () => {
    pendingMock.mockResolvedValue({ total: 0, reasons: [] });
    renderQueue();

    expect(
      await screen.findByText(/Everything in the archive has been through AI review/),
    ).toBeTruthy();
    expect(screen.queryByText(/have not been through AI review/)).toBeNull();
  });

  it("still renders the queue when the count cannot be fetched", async () => {
    // Informational only: a failed count must not take the screen down.
    pendingMock.mockRejectedValue(new Error("offline"));
    renderQueue();

    expect(await screen.findByText("Nothing waiting for you.")).toBeTruthy();
  });
});
