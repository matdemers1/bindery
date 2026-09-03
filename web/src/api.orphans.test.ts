import { describe, expect, it } from "vitest";

/**
 * Every client method reaches a screen, or says why not (CR-088).
 *
 * A method on `api` with no caller is three artefacts — a model or query, a
 * route, a client method — that a stranger has to read, keep working through
 * refactors, and cover in the permission sweep, for no user-visible behaviour.
 * The cost is not the code; it is that a dead method and an unfinished one look
 * identical, so the honest answer to "can I delete this?" is always "I'd have to
 * ask", and nobody ever deletes anything.
 *
 * So this does not forbid an unused method. It forbids an *undeclared* one. Add
 * it below with what it is for and what has to happen to it, or wire it up.
 */

// Read through Vite rather than `fs`: `src` is typed for the browser and has no
// Node types, deliberately, so that nothing here can reach for a Node API.
const sources = import.meta.glob("./**/*.{ts,tsx}", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** The exported client objects, and the methods declared on each. */
function declaredMethods(): Map<string, string> {
  const api = sources["./api.ts"];
  const lines = api.split("\n");
  const found = new Map<string, string>();
  let owner: string | null = null;
  for (const line of lines) {
    const opening = /^export const (\w+)(?::[^=]+)? = \{/.exec(line);
    if (opening) {
      owner = opening[1];
      continue;
    }
    if (line === "};") {
      owner = null;
      continue;
    }
    // A member at exactly one level of indentation inside the object literal.
    const member = /^ {2}(?:async )?([a-zA-Z_$][\w$]*)\s*[(<:]/.exec(line);
    if (owner && member) found.set(member[1], owner);
  }
  return found;
}

/** Everything in `src` except the declarations themselves. */
function callSites(): string {
  return Object.entries(sources)
    .filter(([path]) => !path.endsWith("/api.ts") && !path.endsWith(".test.ts"))
    .map(([, text]) => text)
    .join("\n");
}

/**
 * Unused today, and deliberately so. Each entry names the backend it already
 * has and what has to happen next, because "unused" alone is the ambiguity this
 * guard exists to remove.
 *
 * This list may shrink and must not grow without an argument. Wiring one up, or
 * deleting the client method with its route and schema together, is the way an
 * entry leaves.
 */
const UNWIRED: Record<string, string> = {
  shelves:
    "GET/POST /shelves, backed by the SavedSearch model. The Glossary's 'Smart Shelf' " +
    "and 'Packet' are defined and have no user-facing existence. Either build the screen " +
    "or delete all four together — note SavedSearch is in api/db/scope.py LIBRARY_SCOPED, " +
    "so removal touches the access boundary.",
  similar:
    "GET /documents/{id}/similar, backed by the embedding cosine search in " +
    "api/taxonomy_health.py. Intended as a 'More like this' strip on the document view.",
  attachAsset:
    "POST /assets/{id}/documents/{id}. No screen attaches an asset to a document yet.",
  vaultSetPin:
    "POST /api/vault/pin. The vault ships with a passphrase; the PIN is a second, " +
    "unfinished unlock path (ADR-012).",
  setQuota:
    "POST /admin/accounts/{id}/quota. The quota is shown on the People screen and " +
    "enforced in api/quota.py, so the only way to change one after invitation is a " +
    "database write. This wants a control on the existing row in AdminPage.tsx.",
  grantAdmin:
    "POST /admin/accounts/{id}/admin. Same as setQuota — surfaced, enforced, and " +
    "changeable only from psql.",
  totpStatus:
    "GET /account/totp. AccountPage reads TOTP state from the account payload instead; " +
    "this is the standalone endpoint and is a deletion candidate.",
};

describe("the API client", () => {
  it("has no method that reaches no screen and explains nothing", () => {
    const declared = declaredMethods();
    const used = callSites();
    const orphans = [...declared]
      // Two shapes, because the client is used both ways. `api.search(` is the
      // direct call; a bare `.search(` on its own line is the same call inside
      // a multi-line chain, which is how ViewerPage, AskPage and SearchPage all
      // reach it. Matching only the first reported four live methods as dead.
      .filter(
        ([name, owner]) =>
          !new RegExp(`\\b${owner}\\.${name}\\b`).test(used) &&
          !new RegExp(`\\.\\s*${name}\\s*[(<]`).test(used),
      )
      .map(([name]) => name)
      .filter((name) => !(name in UNWIRED))
      .sort();

    expect(
      orphans,
      "these client methods are called from no screen. Wire one up, or delete it " +
        "together with its route and schema, or add it to UNWIRED in this file with " +
        "what it is for and what has to happen to it.",
    ).toEqual([]);
  });

  it("does not carry an exemption for a method that is now used or now gone", () => {
    const declared = declaredMethods();
    const used = callSites();
    const stale = Object.keys(UNWIRED)
      .filter((name) => {
        const owner = declared.get(name);
        if (!owner) return true; // deleted — good, and the entry should go too
        return new RegExp(`\\b${owner}\\.${name}\\b`).test(used); // wired up
      })
      .sort();

    expect(
      stale,
      "UNWIRED still lists methods that have since been wired up or deleted. " +
        "Remove them — an exemption list that outlives its entries stops meaning anything.",
    ).toEqual([]);
  });

  it("every exemption carries an argument rather than a shrug", () => {
    for (const [name, reason] of Object.entries(UNWIRED)) {
      expect(reason.length, `${name}'s exemption needs a real reason`).toBeGreaterThan(60);
    }
  });

  it("is actually looking at something", () => {
    // A guard that silently examines nothing reads green forever — the same
    // failure tests/test_shell_navigation.py guards against on the router.
    const declared = declaredMethods();
    expect(declared.size, "the api.ts parser has stopped matching methods").toBeGreaterThan(100);
    expect(new Set(declared.values())).toContain("api");
    expect(callSites().length, "the call-site scan has stopped reading src").toBeGreaterThan(
      100_000,
    );
  });
});
