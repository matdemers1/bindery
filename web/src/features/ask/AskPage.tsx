import { useEffect, useRef, useState } from "react";
import { Link } from "react-router";
import {
  ArrowUp,
  BookOpen,
  FileText,
  PanelRightClose,
  Quote,
  ShieldCheck,
  Sparkles,
  X,
} from "lucide-react";

import { ApiError, api, type AskAnswer, type Document, type Library } from "../../api";
import { Logo } from "../../components/brand/Logo";
import Modal from "../../components/Modal";
import FirstRun from "../firstrun/FirstRun";
import NextSteps from "../firstrun/NextSteps";
import { hasFoundSomething } from "../firstrun/onboarding";
import { Alert } from "@d3cloud/ui";

/**
 * Ask — the landing screen.
 *
 * This is the front door because it is the only screen that answers the
 * question people actually arrive with. "When did I last get the brakes done"
 * is the thought; "search for *brakes*, scan results, open a PDF, find the
 * line" is the chore that used to stand between the thought and the answer.
 *
 * The design constraint is unusual and worth stating plainly: **the feature is
 * allowed to refuse.** An uncited answer never reaches this screen, because a
 * confident summary of someone's medical or financial records with no source is
 * worse than no feature at all — a plausible wrong answer gets acted on. So
 * there are three legitimate outcomes: an answer with its sources, no answer
 * plus the pages that mention it, or nothing matched.
 *
 * Sources live in a panel that slides in from the right rather than a list
 * underneath. Underneath, they were a footnote — read after the answer had
 * already been believed. Beside it, the answer and the evidence for it are on
 * screen together, and checking costs one click instead of a scroll.
 */
const EXAMPLES = [
  "When did I last get the brakes done?",
  "What is my policy number?",
  "How much was the roof?",
  "When does my passport expire?",
];

/**
 * Whether a model is configured, for the suggestion chips only.
 *
 * The chips are four promises. With no key they promise answers this archive
 * cannot give — and running an archive without a key is the ordinary state,
 * not a broken one, because retrieval never depended on the model (D-02).
 * Asking is still allowed with no key: `api/ask.py` degrades to handing back
 * the matching pages, which is the honest answer. What changes here is only
 * what the screen *offers* before you have typed anything.
 */
function useProviderConfigured(): boolean | null {
  const [configured, setConfigured] = useState<boolean | null>(null);
  useEffect(() => {
    let live = true;
    api
      .settings()
      .then((s) => live && setConfigured(s.anthropic_key_configured))
      // Unknown is not the same as absent: on a failed read, leave the screen
      // exactly as it was rather than telling somebody their key is missing.
      .catch(() => live && setConfigured(null));
    return () => {
      live = false;
    };
  }, []);
  return configured;
}

export default function AskPage({
  libraries,
  onUploaded,
  userId,
}: {
  libraries: Library[];
  onUploaded: () => void;
  userId?: string;
}) {
  // An empty archive makes Ask pointless, and this is the landing screen — so
  // the first-run walkthrough lives here now rather than behind Search.
  const [empty, setEmpty] = useState<boolean | null>(null);
  const [stepsHidden, setStepsHidden] = useState(false);

  useEffect(() => {
    // Source files rather than documents: a file that arrived but has not
    // finished processing still means the archive is not empty, and showing
    // "nothing in here yet" while the pipeline runs would be a lie.
    api
      .sourceFiles()
      .then((files) => setEmpty(files.length === 0))
      .catch(() => setEmpty(false));
  }, []);

  const [question, setQuestion] = useState("");
  const providerConfigured = useProviderConfigured();
  const [asked, setAsked] = useState<string | null>(null);
  const [result, setResult] = useState<AskAnswer | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [sourcesOpen, setSourcesOpen] = useState(false);
  const inputRef = useRef<HTMLTextAreaElement>(null);

  async function submit(value: string) {
    const text = value.trim();
    if (text.length < 3) return;
    setAsked(text);
    setLoading(true);
    setError(null);
    setResult(null);
    setSourcesOpen(false);
    try {
      setResult(await api.ask(text));
    } catch (caught) {
      setError(caught);
    } finally {
      setLoading(false);
    }
  }

  // Escape closes the panel before it does anything else on the page.
  useEffect(() => {
    if (!sourcesOpen) return;
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setSourcesOpen(false);
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [sourcesOpen]);

  const sources = result?.citations.length
    ? result.citations
    : (result?.consulted ?? []).map((page) => ({ ...page, quote: "" }));

  if (empty === true) {
    return (
      <div className="mx-auto max-w-3xl">
        <FirstRun libraries={libraries} onUploaded={onUploaded} />
      </div>
    );
  }

  // Between "there is something in here" and "I have found one of my own
  // documents" there is a gap the first-run narration used to leave wide open:
  // it vanishes the moment a file arrives (REQ-146).
  const showNextSteps =
    empty === false && userId !== undefined && !hasFoundSomething(userId) && !stepsHidden;

  return (
    <div className="flex gap-6">
      <div className="mx-auto min-w-0 max-w-3xl flex-1">
        {showNextSteps && (
          <NextSteps documents={1} onDismiss={() => setStepsHidden(true)} />
        )}
        {!asked && <Hero />}

        <form
          className="relative"
          onSubmit={(event) => {
            event.preventDefault();
            void submit(question);
          }}
        >
          <label htmlFor="ask-question" className="sr-only">
            Your question
          </label>
          <textarea
            id="ask-question"
            ref={inputRef}
            autoFocus
            rows={1}
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            onKeyDown={(event) => {
              // Enter asks; Shift+Enter is a newline. A question long enough to
              // need two lines is rare, and pressing Enter is the reflex.
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void submit(question);
              }
            }}
            placeholder="Ask anything about your documents…"
            className="w-full resize-none rounded-2xl border border-field bg-surface py-4 pl-5 pr-14 text-16 outline-none transition-colors focus:border-accent"
          />
          <button
            type="submit"
            disabled={question.trim().length < 3 || loading}
            aria-label="Ask"
            className="absolute bottom-3 right-3 rounded-xl bg-accent p-2 text-ink transition-opacity disabled:opacity-30"
          >
            <ArrowUp size={16} strokeWidth={2.5} />
          </button>
        </form>

        {!asked && <VitalRecords />}

        {!asked && providerConfigured !== false && (
          <ul className="mt-4 flex flex-wrap gap-2">
            {EXAMPLES.map((example) => (
              <li key={example}>
                <button
                  type="button"
                  onClick={() => {
                    setQuestion(example);
                    void submit(example);
                  }}
                  className="rounded-full border border-field px-3 py-1.5 text-sm text-muted transition-colors hover:border-accent/60 hover:text-fg"
                >
                  {example}
                </button>
              </li>
            ))}
          </ul>
        )}

        {/* Not an apology, and not an error — the archive is doing the thing it
            was built to do. Finding the page never needed a model, so with no
            key the honest offer is the search that does work, not four
            questions that cannot be answered (D-02). */}
        {!asked && providerConfigured === false && (
          <div className="mt-4 rounded-lg border border-edge bg-surface p-4 text-sm">
            <p className="text-fg">Finding the page never needed a key.</p>
            <p className="mt-1 text-muted">
              Answering questions in prose does. Without one, asking still returns
              the pages that match — every page is OCR&apos;d and indexed either
              way.
            </p>
            <div className="mt-3 flex flex-wrap items-center gap-3">
              <Link
                to="/search"
                className="rounded-md border border-field px-3 py-1.5 text-sm text-fg transition-colors hover:border-accent/60"
              >
                Search the archive
              </Link>
              <Link
                to="/settings"
                className="text-sm text-accent underline underline-offset-2"
              >
                Add a key in Settings
              </Link>
            </div>
          </div>
        )}

        {asked && (
          <section className="mt-8" aria-live="polite">
            <p className="mb-4 flex items-start gap-2.5 text-16 text-fg">
              <Quote size={15} className="mt-1.5 shrink-0 text-muted" />
              <span className="font-medium">{asked}</span>
            </p>

            {loading && <Thinking />}

            {error != null && (
              <Alert tone="danger" dynamic>
                {error instanceof ApiError ? error.message : String(error)}
              </Alert>
            )}

            {result && (
              <Answer
                result={result}
                sourceCount={sources.length}
                onOpenSources={() => setSourcesOpen(true)}
              />
            )}
          </section>
        )}
      </div>

      {result && sources.length > 0 && (
        <>
          <SourcesPanel
            open={sourcesOpen}
            cited={result.citations.length > 0}
            sources={sources}
            onClose={() => setSourcesOpen(false)}
          />
          {/* The panel is a `lg:block` drawer, so below that breakpoint — a
              phone, or a desktop at 400% zoom — the sheet is the only way the
              evidence is reachable at all. Ask never shows an answer without
              the page it came from, and that has to hold at every width. */}
          <SourcesSheet
            open={sourcesOpen}
            sources={sources}
            onClose={() => setSourcesOpen(false)}
          />
        </>
      )}
    </div>
  );
}

function Hero() {
  return (
    <div className="mb-8 text-center">
      <Logo size={56} variant="mascot" className="mx-auto text-fg" />
      <h1 className="mt-4 text-2xl font-semibold tracking-tight">
        What do you need to find?
      </h1>
      <p className="mx-auto mt-2 max-w-md text-sm text-muted">
        Answers come from your own documents, and every one shows the page it came
        from. If nothing in the archive supports an answer, you get the pages
        instead of a guess.
      </p>
    </div>
  );
}

/**
 * Vital records, pinned to the landing screen (REQ-091).
 *
 * The day you need a DD-214 or a death certificate is not a day you want to be
 * composing a question, so these sit above the prompt: one click, zero recall.
 * Silent when the tier is empty — a box explaining a feature you are not using
 * is worse than nothing on an otherwise calm screen.
 */
function VitalRecords() {
  const [documents, setDocuments] = useState<Document[]>([]);

  useEffect(() => {
    api
      .vital()
      .then(setDocuments)
      .catch(() => {});
  }, []);

  if (documents.length === 0) return null;

  return (
    <section className="mt-6">
      {/* d3-allow: the system has no letter-spacing scale, and four uses across two patterns is not enough evidence to invent one. */}
      <h2 className="flex items-center gap-1.5 text-11 font-semibold uppercase tracking-[0.14em] text-muted">
        <ShieldCheck size={12} className="text-accent" />
        Vital records
      </h2>
      <ul className="mt-2 flex flex-wrap gap-2">
        {documents.map((document) => (
          <li key={document.id}>
            <Link
              to={`/document/${document.id}/page/${document.page_start}`}
              className="flex items-center gap-2 rounded-lg border border-edge bg-surface px-3 py-2 text-sm transition-colors hover:border-accent/60"
            >
              <FileText size={14} className="text-muted" />
              <span className="font-medium">{document.title ?? "Untitled"}</span>
              {document.document_date && (
                <span className="text-xs text-muted">{document.document_date}</span>
              )}
            </Link>
          </li>
        ))}
      </ul>
    </section>
  );
}

function Thinking() {
  return (
    <div className="flex items-center gap-2.5 text-sm text-muted">
      <Sparkles size={15} className="animate-pulse text-accent" />
      Reading the pages that mention it…
    </div>
  );
}

function Answer({
  result,
  sourceCount,
  onOpenSources,
}: {
  result: AskAnswer;
  sourceCount: number;
  onOpenSources: () => void;
}) {
  const answered = Boolean(result.answer);

  return (
    <div className="space-y-4">
      {answered ? (
        <p className="whitespace-pre-wrap text-16 leading-relaxed">{result.answer}</p>
      ) : (
        <div className="rounded-xl border border-edge bg-surface p-4">
          <p className="text-sm">{result.unavailable_reason}</p>
        </div>
      )}

      {sourceCount > 0 && (
        <button
          type="button"
          onClick={onOpenSources}
          className="inline-flex items-center gap-2 rounded-full border border-field px-3.5 py-1.5 text-sm text-muted transition-colors hover:border-accent/60 hover:text-fg"
        >
          <BookOpen size={15} />
          {answered
            ? `${sourceCount} source${sourceCount === 1 ? "" : "s"}`
            : `${sourceCount} page${sourceCount === 1 ? "" : "s"} mention it`}
        </button>
      )}

      {answered && result.model && (
        <p className="text-xs text-muted">
          Written by {result.model} from those pages, and nothing else.
        </p>
      )}
    </div>
  );
}

type Source = {
  document_id: string;
  source_file_id: string;
  title: string;
  page_number: number;
  quote: string;
};

/**
 * The evidence, beside the answer.
 *
 * A drawer rather than a modal: a modal would make checking a source an
 * interruption, and the whole argument for this panel is that checking should
 * be cheap enough to actually do.
 */
function SourcesPanel({
  open,
  cited,
  sources,
  onClose,
}: {
  open: boolean;
  cited: boolean;
  sources: Source[];
  onClose: () => void;
}) {
  return (
    <aside
      // `width: 0`, `overflow: hidden` and `opacity: 0` hide the panel from
      // the eye and from nothing else: its links and its close button stay in
      // the tab order. `aria-hidden` over a focusable subtree is exactly the
      // combination ARIA forbids — Tab off "3 sources" and focus vanishes into
      // a zero-width strip that announces nothing. `inert` removes both.
      inert={!open}
      className={`sticky top-8 hidden h-[calc(100vh-6rem)] shrink-0 overflow-hidden transition-[width,opacity] duration-200 lg:block ${
        open ? "w-96 opacity-100" : "w-0 opacity-0"
      }`}
    >
      <div className="flex h-full w-96 flex-col rounded-xl border border-edge bg-surface">
        <header className="flex items-center justify-between border-b border-edge px-4 py-3">
          <h2 className="flex items-center gap-2 text-sm font-medium">
            <BookOpen size={15} className="text-accent" />
            {cited ? "Where this came from" : "Pages that mention it"}
          </h2>
          <button
            onClick={onClose}
            aria-label="Close sources"
            className="rounded p-1 text-muted hover:text-fg"
          >
            <PanelRightClose size={16} />
          </button>
        </header>

        <ol className="flex-1 divide-y divide-edge overflow-y-auto">
          {sources.map((source, index) => (
            <li key={`${source.document_id}-${source.page_number}-${index}`} className="p-4">
              <Link
                to={`/document/${source.document_id}/page/${source.page_number}`}
                className="flex items-start gap-2 text-sm font-medium underline-offset-2 hover:underline"
              >
                <FileText size={15} className="mt-0.5 shrink-0 text-muted" />
                <span className="min-w-0">{source.title}</span>
              </Link>
              {/* d3-allow: aligns under the icon and gap on the line above — a measured offset, not a spacing step. */}
              <p className="ml-[1.4rem] mt-0.5 text-xs text-muted">
                page {source.page_number}
              </p>
              {source.quote && (
                /* d3-allow: aligns under the icon and gap on the line above — a measured offset, not a spacing step. */
                <blockquote className="ml-[1.4rem] mt-2 border-l-2 border-accent/50 pl-2.5 text-sm text-muted">
                  {source.quote}
                </blockquote>
              )}
            </li>
          ))}
        </ol>
      </div>
    </aside>
  );
}

/** Narrow screens get the same list as a sheet rather than a side panel. */
function SourcesSheet({
  open,
  sources,
  onClose,
}: {
  open: boolean;
  sources: Source[];
  onClose: () => void;
}) {
  if (!open) return null;
  return (
    <Modal
      label="Sources"
      onClose={onClose}
      backdropClassName="fixed inset-0 z-40 flex items-end bg-ink/70 lg:hidden"
      className="max-h-[70vh] w-full overflow-y-auto rounded-t-2xl border-t border-edge bg-surface p-4"
    >
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-medium">Sources</h2>
        <button onClick={onClose} aria-label="Close sources">
          <X size={16} />
        </button>
      </div>
      <ol className="divide-y divide-edge">
        {sources.map((source, index) => (
          <li key={index} className="py-3">
            <Link
              to={`/document/${source.document_id}/page/${source.page_number}`}
              className="text-sm font-medium underline-offset-2 hover:underline"
            >
              {source.title}
            </Link>
            <p className="text-xs text-muted">page {source.page_number}</p>
          </li>
        ))}
      </ol>
    </Modal>
  );
}
