import { describe, expect, it } from "vitest";

/**
 * A control's boundary is not a divider (D-07).
 *
 * `--color-edge` is a card stroke at 1.23:1 on surface, which is all a divider
 * needs to be. `--color-field` exists because WCAG 1.4.11 asks for 3:1 on the
 * boundary of a user-interface component, so the box can be found before it is
 * focused. The token was introduced with that reasoning written into
 * `index.css` and then applied only to form fields, leaving 67 buttons and
 * selects drawing their only outline at 1.23:1 — recoverable on hover, which
 * a phone does not have.
 *
 * Read through Vite rather than `fs`: `src` is typed for the browser and has
 * no Node types, deliberately.
 *
 * The counts below fall as controls move to `@d3cloud/ui`, whose `Button` and
 * `Select` draw their own boundary from the token. They are a floor against the
 * guard silently parsing nothing, not a target — lower them when a migration
 * batch lands. When they reach zero this file should be deleted outright: the
 * rule will live in the library, enforced once, instead of being re-checked
 * here against markup that no longer writes its own outline.
 */
const sources = import.meta.glob("./**/*.tsx", {
  query: "?raw",
  import: "default",
  eager: true,
}) as Record<string, string>;

/** Each `<button …>` / `<select …>` opening tag, brace- and quote-aware. */
function openingTags(src: string): string[] {
  const tags: string[] = [];
  for (const match of src.matchAll(/<(button|select)\b/g)) {
    let i = match.index + match[0].length;
    let depth = 0;
    let quote: string | null = null;
    while (i < src.length) {
      const c = src[i];
      if (quote) {
        if (c === quote && src[i - 1] !== "\\") quote = null;
      } else if (c === '"' || c === "'" || c === "`") {
        quote = c;
      } else if (c === "{") depth++;
      else if (c === "}") depth--;
      else if (c === ">" && depth === 0) {
        tags.push(src.slice(match.index, i + 1));
        break;
      }
      i++;
    }
  }
  return tags;
}

describe("control boundaries", () => {
  it("is looking at something", () => {
    // The failure this guard could have: passing while parsing nothing.
    const all = Object.values(sources).flatMap(openingTags);
    expect(Object.keys(sources).length).toBeGreaterThan(30);
    expect(all.length).toBeGreaterThan(40);
    expect(all.filter((t) => t.includes("border-field")).length).toBeGreaterThan(10);
  });

  it("never draws a button or select outline with the divider token", () => {
    const offenders: string[] = [];
    for (const [path, src] of Object.entries(sources)) {
      for (const tag of openingTags(src)) {
        if (/\bborder-edge\b/.test(tag)) {
          offenders.push(`${path}: ${tag.replace(/\s+/g, " ").slice(0, 110)}`);
        }
      }
    }
    expect(
      offenders,
      "these controls outline themselves with `border-edge` (1.23:1). A control " +
        "boundary needs 3:1 — use `border-field`, which exists for this.",
    ).toEqual([]);
  });
});
