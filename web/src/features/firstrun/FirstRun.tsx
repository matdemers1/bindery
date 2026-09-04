import { useCallback, useRef, useState } from "react";
import { Link } from "react-router";

import { ApiError, api, type Library, type PipelineStatus } from "../../api";
import { useLiveQuery } from "../../live/LiveProvider";
import { Button } from "@d3cloud/ui";

/**
 * First run (T-8.6, REQ-119).
 *
 * The first minute decides whether someone trusts this with their documents, so
 * it narrates rather than congratulates. What it shows is the actual pipeline
 * running on the actual file they just dropped — no canned demo, no sample data
 * to delete afterwards.
 *
 * The claims it makes are the ones that matter and are all verifiable on the
 * next screen: your original is never modified, nothing is ever deleted, and
 * search finds the page rather than the file.
 *
 * It disappears the moment the archive is not empty, and never returns.
 */
const STEPS = [
  {
    title: "Drop something in",
    body:
      "A PDF, a photo of a receipt, a whole scanner batch. The original is stored " +
      "byte-for-byte and never modified — everything Bindery does happens alongside it.",
  },
  {
    title: "It gets read",
    body:
      "Every page is OCR'd and indexed. This works with no API key and no network: " +
      "finding your documents never depends on anything outside this machine.",
  },
  {
    title: "Then it gets filed",
    body:
      "With a key configured, a scan holding twelve documents is split into twelve — " +
      "by page range, without cutting the original — and each one titled, dated and " +
      "tagged. Anything it is not sure about waits for you instead of guessing.",
  },
];

export default function FirstRun({
  libraries,
  onUploaded,
}: {
  libraries: Library[];
  onUploaded: () => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [status, setStatus] = useState<PipelineStatus | null>(null);
  const [started, setStarted] = useState(false);

  const poll = useCallback(async () => {
    try {
      setStatus(await api.pipeline());
    } catch {
      // The panel is decorative once the upload succeeded; a failed poll is not
      // worth an error state on someone's first thirty seconds.
    }
  }, []);

  useLiveQuery(started ? ["jobs", "files"] : [], poll);

  async function upload(files: FileList | null) {
    const target = libraries[0];
    if (!files?.length || !target) return;
    setBusy(true);
    setError(null);
    try {
      await Promise.all(Array.from(files).map((file) => api.upload(target.id, file)));
      setStarted(true);
      onUploaded();
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.message : "That upload did not go through.",
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="mt-10 rounded-lg border border-edge bg-surface p-6">
      <h2 className="text-base font-medium">Nothing in here yet</h2>
      <p className="mt-1 max-w-2xl text-sm text-muted">
        Bindery is a place to put the paperwork you would otherwise have to keep in a
        drawer and remember. Here is what happens when you add something.
      </p>

      <ol className="mt-5 space-y-4">
        {STEPS.map((step, index) => (
          <li key={step.title} className="flex gap-3">
            <span
              aria-hidden
              className="mt-0.5 flex h-6 w-6 shrink-0 items-center justify-center rounded-full border border-edge text-xs text-muted"
            >
              {index + 1}
            </span>
            <div>
              <p className="text-sm font-medium">{step.title}</p>
              <p className="mt-0.5 max-w-2xl text-sm text-muted">{step.body}</p>
            </div>
          </li>
        ))}
      </ol>

      <div className="mt-6 flex flex-wrap items-center gap-3">
        <input
          ref={input}
          type="file"
          multiple
          className="hidden"
          onChange={(event) => void upload(event.target.files)}
        />
        <Button variant="primary" onClick={() => input.current?.click()} disabled={busy || libraries.length === 0}>
          {busy ? "Adding…" : "Add your first document"}
        </Button>
        <span className="text-xs text-muted">
          Or drop files anywhere on this page.
        </span>
      </div>

      {error && (
        <p role="alert" className="mt-3 text-sm text-danger">
          {error}
        </p>
      )}

      {started && (
        <div className="mt-6 border-t border-edge pt-4" aria-live="polite">
          <p className="text-sm font-medium">Working on it</p>
          <p className="mt-1 text-sm text-muted">
            {status && status.in_flight.length > 0
              ? `${status.in_flight.length} step${
                  status.in_flight.length === 1 ? "" : "s"
                } in flight.`
              : "Reading the pages. This takes a few seconds per page."}{" "}
            <Link to="/pipeline" className="underline underline-offset-2">
              Watch it in detail
            </Link>
            , or just search for a word you know is in the document.
          </p>
        </div>
      )}

      <p className="mt-6 text-xs text-muted">
        Nothing in Bindery is ever deleted — corrections supersede, they do not erase.
        Whatever you put in, you can take back out: the{" "}
        <Link to="/trust" className="underline underline-offset-2">
          full export
        </Link>{" "}
        is a folder of your originals plus a page you open in a browser, and it works
        with Bindery switched off.
      </p>
    </section>
  );
}
