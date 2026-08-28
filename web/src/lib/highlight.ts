// Turning a search query into the words to light up on a page.
//
// Postgres matched stems, so the query term "discharge" found "discharged".
// Prefix matching approximates that closely enough for a visual overlay, and a
// near-miss highlight is far better than a missing one — the overlay's job is to
// pull the eye to the right part of the page, not to be a second search engine.

const OPERATORS = /\bor\b|\band\b/gi;

export function queryTerms(query: string): string[] {
  const cleaned = query
    .replace(/"/g, " ")
    .replace(OPERATORS, " ")
    // `-foo` excludes, so it must never be highlighted.
    .replace(/(^|\s)-\S+/g, " ")
    .toLowerCase();

  return [...new Set(cleaned.split(/[^\p{L}\p{N}]+/u).filter((term) => term.length > 2))];
}

export function matchesTerm(word: string, terms: string[]): boolean {
  const normalized = word.toLowerCase().replace(/^[^\p{L}\p{N}]+|[^\p{L}\p{N}]+$/gu, "");
  if (!normalized) return false;
  return terms.some(
    (term) => normalized.startsWith(term) || term.startsWith(normalized.slice(0, term.length)),
  );
}
