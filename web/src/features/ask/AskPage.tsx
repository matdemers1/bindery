import { useState } from "react";
import { Link } from "react-router";

import { ApiError, api, type AskAnswer } from "../../api";

/**
 * Ask — questions answered from the archive, cited to a page (REQ-116).
 *
 * The design constraint here is unusual: **the feature is allowed to refuse.**
 * An uncited answer never reaches this screen, because a confident summary of
 * someone's medical or financial records with no source is worse than no
 * feature at all — a plausible wrong answer gets acted on.
 *
 * So there are three outcomes, and all three are legitimate:
 *
 *   1. An answer, with the pages it came from, each one a link.
 *   2. No answer, plus the pages that mention it — which is what the search box
 *      would have given you, and is still useful.
 *   3. Nothing matched.
 *
 * The second case is not an error state and is deliberately not styled as one.
 */
const EXAMPLES = [
  "when did I last get the brakes done?",
  "what is my policy number?",
  "how much was the roof?",
];

export default function AskPage() {
  const [question, setQuestion] = useState("");
  const [result, setResult] = useState<AskAnswer | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(asked: string) {
    if (asked.trim().length < 3) return;
    setLoading(true);
    setError(null);
    setResult(null);
    try {
      setResult(await api.ask(asked.trim()));
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="mx-auto max-w-3xl space-y-5">
      <header className="space-y-1">
        <h1 className="text-lg font-semibold">Ask</h1>
        <p className="text-sm text-muted">
          Questions answered from your own documents, with the page each answer came
          from. If nothing in the archive supports an answer, you get the pages
          instead of a guess.
        </p>
      </header>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          void submit(question);
        }}
      >
        <label htmlFor="ask-question" className="sr-only">
          Your question
        </label>
        <input
          id="ask-question"
          autoFocus
          value={question}
          onChange={(event) => setQuestion(event.target.value)}
          placeholder="Ask about anything in the archive…"
          className="w-full rounded-lg border border-edge bg-surface px-4 py-3 text-lg outline-none focus:border-accent"
        />
      </form>

      {!result && !loading && !error && (
        <div className="text-sm text-muted">
          <p>For example:</p>
          <ul className="mt-2 space-y-1">
            {EXAMPLES.map((example) => (
              <li key={example}>
                <button
                  type="button"
                  onClick={() => {
                    setQuestion(example);
                    void submit(example);
                  }}
                  className="underline underline-offset-2 hover:text-neutral-100"
                >
                  {example}
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {loading && (
        <div className="space-y-3" aria-live="polite">
          <p className="text-sm text-muted">Reading the pages that mention it…</p>
          <div className="h-24 animate-pulse rounded-lg border border-edge bg-surface" />
        </div>
      )}

      {error && (
        <p role="alert" className="rounded-md border border-red-900 bg-red-950/40 p-3 text-sm text-red-300">
          {error}
        </p>
      )}

      {result && <Answer result={result} />}
    </div>
  );
}

function Answer({ result }: { result: AskAnswer }) {
  if (result.answer) {
    return (
      <section className="space-y-4" aria-live="polite">
        <p className="whitespace-pre-wrap text-[15px] leading-relaxed">{result.answer}</p>
        <div>
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
            From these pages
          </h2>
          <ul className="mt-2 space-y-2">
            {result.citations.map((citation, index) => (
              <li
                key={`${citation.document_id}-${citation.page_number}-${index}`}
                className="rounded-lg border border-edge bg-surface p-3"
              >
                <Link
                  to={`/document/${citation.document_id}/page/${citation.page_number}`}
                  className="font-medium underline-offset-2 hover:underline"
                >
                  {citation.title}
                </Link>
                <span className="ml-2 text-xs text-muted">page {citation.page_number}</span>
                {citation.quote && (
                  <blockquote className="mt-1 border-l-2 border-edge pl-2 text-sm text-muted">
                    {citation.quote}
                  </blockquote>
                )}
              </li>
            ))}
          </ul>
        </div>
        {result.model && (
          <p className="text-xs text-muted">
            Written by {result.model} from the pages above, and nothing else.
          </p>
        )}
      </section>
    );
  }

  return (
    <section className="space-y-3" aria-live="polite">
      <p className="rounded-md border border-edge bg-surface p-3 text-sm">
        {result.unavailable_reason}
      </p>
      {result.consulted.length > 0 && (
        <div>
          <h2 className="text-xs font-semibold uppercase tracking-wide text-muted">
            Pages that mention it
          </h2>
          <ul className="mt-2 space-y-1">
            {result.consulted.map((page, index) => (
              <li key={`${page.document_id}-${page.page_number}-${index}`}>
                <Link
                  to={`/document/${page.document_id}/page/${page.page_number}`}
                  className="text-sm underline underline-offset-2 hover:text-neutral-100"
                >
                  {page.title}
                  <span className="ml-2 text-xs text-muted">page {page.page_number}</span>
                </Link>
              </li>
            ))}
          </ul>
        </div>
      )}
    </section>
  );
}
