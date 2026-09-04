import { useEffect, useState } from "react";
import { Link, useSearchParams } from "react-router";
import { BookOpen, ChevronRight, HelpCircle, Sparkles } from "lucide-react";

import PageHeader from "../../components/PageHeader";

type Callout = { kind: "note" | "warning"; text: string };
type Section = { heading: string; body: string[]; callout?: Callout };
type Guide = {
  route: string;
  title: string;
  blurb: string;
  screenshot: string;
  sections: Section[];
};

type Content = { guides: Guide[]; faq: { question: string; answer: string }[] };
type Release = { version: string; sections: { heading: string; changes: string[] }[] };
type Changelog = { intro: string; releases: Release[] };

/**
 * The guides are fetched rather than imported.
 *
 * They started as a build-time import from `docs/help/`, which type-checked
 * perfectly on a laptop and failed in the image: `Dockerfile.web` copies only
 * `web/`, so the file was outside the build context entirely. `tsc` run from
 * the repository root resolved a path the container could never see.
 *
 * Living under `web/public` fixes that and colocates the prose with the
 * screenshots that illustrate it. Same origin, so REQ-148 holds — nothing here
 * needs a network.
 */
async function loadContent(): Promise<Content> {
  const response = await fetch("/help/guides.json");
  if (!response.ok) throw new Error(`guides.json: ${response.status}`);
  return (await response.json()) as Content;
}

async function loadChangelog(): Promise<Changelog> {
  const response = await fetch("/help/changelog.json");
  if (!response.ok) throw new Error(`changelog.json: ${response.status}`);
  return (await response.json()) as Changelog;
}

/**
 * Inline markup, deliberately tiny.
 *
 * The guides needed bold and code and nothing else, and a markdown library
 * would have been a parser and a dependency in exchange for two constructs.
 * Everything is escaped by React except what this produces, and what this
 * produces is `<strong>` and `<code>`.
 */
function rich(text: string, key: number) {
  const parts = text.split(/(\*\*[^*]+\*\*|`[^`]+`|\*[^*]+\*)/g);
  return (
    <p key={key} className="mt-2 text-sm leading-relaxed text-muted first:mt-0">
      {parts.map((part, index) => {
        if (part.startsWith("**") && part.endsWith("**")) {
          return (
            <strong key={index} className="font-medium text-fg">
              {part.slice(2, -2)}
            </strong>
          );
        }
        if (part.startsWith("`") && part.endsWith("`")) {
          return (
            <code key={index} className="rounded bg-ink px-1 py-0.5 font-mono text-xs">
              {part.slice(1, -1)}
            </code>
          );
        }
        if (part.startsWith("*") && part.endsWith("*") && part.length > 2) {
          return <em key={index}>{part.slice(1, -1)}</em>;
        }
        return part;
      })}
    </p>
  );
}

export default function HelpPage() {
  const [params, setParams] = useSearchParams();
  const [content, setContent] = useState<Content | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    loadContent().then(setContent).catch(() => setFailed(true));
  }, []);

  const view = params.get("view");
  const selected = params.get("screen");
  const guides = content?.guides ?? [];
  const guide = guides.find((one) => one.route === selected);

  return (
    <div className="mx-auto max-w-5xl">
      <PageHeader icon={BookOpen} title="Guides">
        How each screen works, and what the archive promises. Everything here is
        served from this machine — no part of it needs a network.
      </PageHeader>

      <div className="mt-4 flex flex-col gap-6 md:flex-row">
        <nav className="md:w-56 md:shrink-0" aria-label="Guides">
          <ul className="space-y-0.5">
            {guides.map((one) => (
              <li key={one.route}>
                <button
                  type="button"
                  onClick={() => setParams({ screen: one.route })}
                  className={`flex w-full items-center gap-1.5 rounded-md px-2.5 py-1.5 text-left text-sm ${
                    one.route === selected
                      ? "bg-accent/15 text-accent"
                      : "text-muted hover:text-fg"
                  }`}
                >
                  {one.title}
                  <ChevronRight size={13} className="ml-auto opacity-40" />
                </button>
              </li>
            ))}
            <li className="pt-2">
              <button
                type="button"
                onClick={() => setParams({ view: "changelog" })}
                className={`flex w-full items-center gap-1.5 rounded-md px-2.5 py-1.5 text-left text-sm ${
                  view === "changelog"
                    ? "bg-accent/15 text-accent"
                    : "text-muted hover:text-fg"
                }`}
              >
                <Sparkles size={13} />
                What changed
              </button>
            </li>
            <li>
              <button
                type="button"
                onClick={() => setParams({})}
                className={`flex w-full items-center gap-1.5 rounded-md px-2.5 py-1.5 text-left text-sm ${
                  !selected ? "bg-accent/15 text-accent" : "text-muted hover:text-fg"
                }`}
              >
                <HelpCircle size={13} />
                Common questions
              </button>
            </li>
          </ul>
        </nav>

        <div className="min-w-0 flex-1">
          {failed ? (
            <p className="rounded-xl border border-edge bg-surface p-5 text-sm text-muted">
              The guides could not be loaded. They are served from this machine,
              so this usually means the deployment is mid-update.
            </p>
          ) : !content ? (
            <p className="text-sm text-muted">Loading…</p>
          ) : guide ? (
            <GuideBody guide={guide} />
          ) : view === "changelog" ? (
            <Changes />
          ) : (
            <Faq faq={content.faq} />
          )}
        </div>
      </div>
    </div>
  );
}

function GuideBody({ guide }: { guide: Guide }) {
  const [shotFailed, setShotFailed] = useState(false);

  return (
    <article className="rounded-xl border border-edge bg-surface p-5">
      <h2 className="text-lg font-semibold tracking-tight">{guide.title}</h2>
      <p className="mt-1 text-sm text-muted">{guide.blurb}</p>
      <Link to={guide.route} className="mt-2 inline-block text-sm text-accent hover:underline">
        Open {guide.title} →
      </Link>

      {!shotFailed && (
        <img
          src={`/help/screens/${guide.screenshot}`}
          alt={`The ${guide.title} screen`}
          loading="lazy"
          onError={() => setShotFailed(true)}
          className="mt-4 w-full rounded-lg border border-edge"
        />
      )}

      {guide.sections.map((section) => (
        <section key={section.heading} className="mt-5">
          <h3 className="text-sm font-medium">{section.heading}</h3>
          <div className="mt-1">{section.body.map(rich)}</div>
          {section.callout && (
            <p
              className={`mt-3 rounded-lg border p-3 text-sm ${
                section.callout.kind === "warning"
                  ? "border-warning/40 bg-warning-muted/20 text-warning"
                  : "border-edge bg-ink text-muted"
              }`}
            >
              {section.callout.text}
            </p>
          )}
        </section>
      ))}
    </article>
  );
}

function Changes() {
  const [log, setLog] = useState<Changelog | null>(null);
  useEffect(() => {
    loadChangelog().then(setLog).catch(() => setLog(null));
  }, []);

  if (!log) return <p className="text-sm text-muted">Loading…</p>;

  return (
    <div className="rounded-xl border border-edge bg-surface p-5">
      <p className="text-sm text-muted">{log.intro}</p>
      {log.releases.map((release) => (
        <section key={release.version} className="mt-5">
          <h2 className="text-base font-semibold tracking-tight">{release.version}</h2>
          {release.sections.map((section) => (
            <div key={section.heading} className="mt-3">
              <h3 className="text-sm font-medium">{section.heading}</h3>
              <ul className="mt-1 space-y-1.5">
                {section.changes.map((change, index) => (
                  <li key={index} className="flex gap-2 text-sm text-muted">
                    <span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-muted" />
                    <span>{rich(change, index).props.children}</span>
                  </li>
                ))}
              </ul>
            </div>
          ))}
        </section>
      ))}
    </div>
  );
}

function Faq({ faq }: { faq: { question: string; answer: string }[] }) {
  return (
    <div className="space-y-3">
      {faq.map((entry) => (
        <details
          key={entry.question}
          className="rounded-xl border border-edge bg-surface p-4"
        >
          <summary className="cursor-pointer text-sm font-medium">
            {entry.question}
          </summary>
          <p className="mt-2 text-sm leading-relaxed text-muted">{entry.answer}</p>
        </details>
      ))}
    </div>
  );
}
