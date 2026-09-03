import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError, api, fileUrl } from "./api";

/**
 * The fetch wrapper and the query builders (CR-123).
 *
 * These are the branches every screen goes through and no screen owns, so a
 * mistake here is a mistake everywhere at once — and until now the only thing
 * that exercised them was a browser walking a happy path against one seeded
 * corpus. The seam under test is the request the client *builds*: the URL and
 * the error it raises, not the server's answer.
 */

function mockFetch(response: {
  status?: number;
  body?: unknown;
  headers?: Record<string, string>;
}) {
  const fetchMock = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({
    status: response.status ?? 200,
    ok: (response.status ?? 200) < 400,
    statusText: "Some Status",
    headers: new Headers(response.headers ?? {}),
    json: async () => {
      if (response.body === undefined) throw new SyntaxError("not json");
      return response.body;
    },
  }));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

/** The path the client asked for, with the `/api` prefix stripped. */
function requestedPath(fetchMock: ReturnType<typeof mockFetch>) {
  const url = fetchMock.mock.calls[0][0] as unknown as string;
  return url.replace(/^\/api/, "");
}

afterEach(() => vi.unstubAllGlobals());

describe("request", () => {
  it("sends cookies, because auth rides on an HTTP-only cookie", async () => {
    const fetchMock = mockFetch({ body: { id: "u1" } });
    await api.me();
    const init = fetchMock.mock.calls[0][1] as unknown as RequestInit;
    expect(init.credentials).toBe("same-origin");
  });

  it("returns undefined for a 204 without trying to parse a body", async () => {
    mockFetch({ status: 204 });
    await expect(api.logout()).resolves.toBeUndefined();
  });

  it("raises ApiError carrying the status, so callers can branch on 401 and 409", async () => {
    mockFetch({ status: 409, body: { detail: "nothing to undo" } });
    const error = await api.undo("d1").catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(409);
    expect((error as ApiError).message).toBe("nothing to undo");
  });

  it("falls back to statusText when the error body is not JSON", async () => {
    // An nginx 502 is an HTML page, and it arrives on exactly the path where
    // the user most needs to be told something.
    mockFetch({ status: 502 });
    const error = (await api.me().catch((e: unknown) => e)) as ApiError;
    expect(error.status).toBe(502);
    expect(error.message).toBe("Some Status");
  });

  it("reads Retry-After off a 429 and leaves it undefined when absent or junk", async () => {
    mockFetch({ status: 429, body: { detail: "slow down" }, headers: { "retry-after": "30" } });
    expect(((await api.me().catch((e: unknown) => e)) as ApiError).retryAfter).toBe(30);

    vi.unstubAllGlobals();
    mockFetch({ status: 429, body: { detail: "slow down" } });
    expect(((await api.me().catch((e: unknown) => e)) as ApiError).retryAfter).toBeUndefined();

    vi.unstubAllGlobals();
    // A date-form Retry-After is legal HTTP and is not a number of seconds.
    mockFetch({
      status: 429,
      body: { detail: "slow down" },
      headers: { "retry-after": "Wed, 21 Oct 2026 07:28:00 GMT" },
    });
    expect(((await api.me().catch((e: unknown) => e)) as ApiError).retryAfter).toBeUndefined();
  });
});

describe("search query string", () => {
  it("repeats library_id and known_form rather than joining them", async () => {
    const fetchMock = mockFetch({ body: {} });
    await api.search({ q: "dd-214", libraryIds: ["a", "b"], knownFormCodes: ["dd214", "w2"] });
    const query = new URLSearchParams(requestedPath(fetchMock).split("?")[1]);
    expect(query.getAll("library_id")).toEqual(["a", "b"]);
    expect(query.getAll("known_form")).toEqual(["dd214", "w2"]);
  });

  it("omits the optional filters entirely when they are not set", async () => {
    const fetchMock = mockFetch({ body: {} });
    await api.search({ q: "brakes" });
    expect(requestedPath(fetchMock)).toBe("/search?q=brakes");
  });

  it("percent-encodes the query rather than pasting it into the URL", async () => {
    const fetchMock = mockFetch({ body: {} });
    await api.search({ q: "tax & fees 2024/25" });
    const query = new URLSearchParams(requestedPath(fetchMock).split("?")[1]);
    expect(query.get("q")).toBe("tax & fees 2024/25");
  });
});

describe("photos query string", () => {
  it("drops an unset filter but keeps offset 0", async () => {
    // `0` and `false` are both falsy and mean opposite things here: page one of
    // the wall is a real request, an unticked checkbox is not a filter.
    const fetchMock = mockFetch({ body: {} });
    await api.photos({ offset: 0, undescribed: false, q: "" });
    expect(requestedPath(fetchMock)).toBe("/photos?offset=0");
  });

  it("sends the filters that are set", async () => {
    const fetchMock = mockFetch({ body: {} });
    await api.photos({ undescribed: true, kind: "video", limit: 50 });
    const query = new URLSearchParams(requestedPath(fetchMock).split("?")[1]);
    expect(query.get("undescribed")).toBe("true");
    expect(query.get("kind")).toBe("video");
    expect(query.get("limit")).toBe("50");
  });

  it("asks for the bare path when nothing is filtered, leaving no trailing ?", async () => {
    const fetchMock = mockFetch({ body: {} });
    await api.photos();
    expect(requestedPath(fetchMock)).toBe("/photos");
  });
});

describe("archive query string", () => {
  it("repeats an array value and drops an empty one", async () => {
    const fetchMock = mockFetch({ body: {} });
    await api.archive({ tag: ["receipt", "car"], correspondent: "" });
    const query = new URLSearchParams(requestedPath(fetchMock).split("?")[1]);
    expect(query.getAll("tag")).toEqual(["receipt", "car"]);
    expect(query.has("correspondent")).toBe(false);
  });
});

describe("fileUrl", () => {
  it("points a vaulted original at the vault route, never at the blob route", () => {
    // ADR-012: a vaulted document has no plaintext original, and the vault
    // route is the only one that refuses while the vault is shut.
    expect(fileUrl.vaultOriginal("d1")).toBe("/api/vault/items/d1/original");
    expect(fileUrl.original("f1")).toBe("/api/files/f1/original");
  });

  it("addresses a page by file and a PDF extract by document", () => {
    // A document is a page range over a file, so these two take different ids
    // and mixing them up silently renders somebody else's page.
    expect(fileUrl.render("f1", 3)).toBe("/api/files/f1/pages/3/render");
    expect(fileUrl.thumb("f1", 3)).toBe("/api/files/f1/pages/3/thumb");
    expect(fileUrl.pdf("f1")).toBe("/api/files/f1/pdf");
    expect(fileUrl.documentPdf("d1")).toBe("/api/documents/d1/pdf");
  });
});
