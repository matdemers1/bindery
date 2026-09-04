import { Link } from "react-router";
import { Check, Search, Sparkles, Upload } from "lucide-react";

/**
 * The strip between "the archive has something in it" and "I have found one of
 * my own documents".
 *
 * It exists because the first-run narration disappears the moment a file
 * arrives, which leaves someone with a working archive and no idea what to do
 * with it. Three things, in the order they are worth doing, and it goes away
 * for good once a search has actually returned a hit — not when it is
 * dismissed. Being told the archive can find things is not the same as having
 * watched it find one of yours.
 */
export default function NextSteps({
  documents,
  onDismiss,
}: {
  documents: number;
  onDismiss: () => void;
}) {
  const steps = [
    {
      done: documents > 0,
      icon: Upload,
      label: "Add something",
      body: "A scan, a photo of a receipt, a folder of old paperwork.",
      to: "/add",
      cta: "Add files",
    },
    {
      done: false,
      icon: Search,
      label: "Find it again",
      body: "Search a word that appears *inside* the document, not its filename. That is the part worth testing.",
      to: "/search",
      cta: "Try a search",
    },
    {
      done: false,
      icon: Sparkles,
      label: "Ask a question",
      body: "“When did I last get the brakes done?” — answered with the page it came from, or not at all.",
      to: "/",
      cta: "Ask something",
    },
  ];

  return (
    <section className="mb-6 rounded-xl border border-edge bg-surface p-4">
      <div className="flex items-start gap-3">
        <h2 className="flex-1 text-sm font-medium">Getting your bearings</h2>
        <button
          type="button"
          onClick={onDismiss}
          className="text-xs text-muted hover:text-fg"
        >
          Hide this
        </button>
      </div>
      <ul className="mt-3 grid gap-3 sm:grid-cols-3">
        {steps.map((step) => (
          <li
            key={step.label}
            className="rounded-lg border border-edge bg-ink p-3"
          >
            <div className="flex items-center gap-2 text-sm font-medium">
              {step.done ? (
                <Check size={14} className="text-success" />
              ) : (
                <step.icon size={14} className="text-muted" />
              )}
              {step.label}
            </div>
            <p className="mt-1 text-xs text-muted">{step.body}</p>
            {!step.done && (
              <Link
                to={step.to}
                className="mt-2 inline-block text-xs text-accent hover:underline"
              >
                {step.cta} →
              </Link>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
