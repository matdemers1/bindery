import { useEffect, useState } from "react";

import { versionApi } from "../../api";
import { Logo } from "../../components/brand/Logo";
import { PagesIllustration } from "./PagesIllustration";
import "./entry.css";

/**
 * Every screen somebody sees before they are signed in (Phase 19, REQ-202).
 *
 * Split, because the sign-in page is the only place Bindery gets to say what it
 * is to someone who has not used it yet — an invited relative, or a stranger
 * who has just run `docker compose up`. The left half makes the promise and
 * names the three claims the app can back up on the next screen; the right half
 * is the form. Below `lg` the story is dropped rather than squeezed: on a phone
 * the form is the whole job.
 *
 * The claims are deliberately the verifiable ones. "Secure" and "private" are
 * what every login page says; "originals are never modified" is a thing a
 * person can check.
 */
export function EntryShell({
  children,
  wide = false,
}: {
  children: React.ReactNode;
  /** For the long forms — setup and invitations — which need more than 22rem. */
  wide?: boolean;
}) {
  return (
    <div className="grid min-h-full bg-bg text-fg lg:grid-cols-[minmax(0,1.05fr)_minmax(0,1fr)]">
      <StoryPanel />
      <main className="flex justify-center px-4 py-8 sm:px-8 lg:items-center lg:py-12">
        <div className={wide ? "w-full max-w-md" : "w-full max-w-sm"}>
          <div className="mb-8 flex items-center gap-2.5 text-base font-semibold lg:hidden">
            <Logo size={28} /> Bindery
          </div>
          {children}
        </div>
      </main>
    </div>
  );
}

function StoryPanel() {
  const build = useBuild();
  return (
    <aside
      aria-label="About Bindery"
      className="entry-story relative hidden overflow-hidden border-r border-border bg-bg-sunken px-14 py-12 lg:flex lg:flex-col"
    >
      <div className="relative z-10 flex items-center gap-2.5 text-base font-semibold">
        <Logo size={28} /> Bindery
      </div>

      <PagesIllustration className="pointer-events-none absolute top-20 right-12 z-0 w-[19rem] opacity-90" />

      <div className="relative z-10 mt-auto">
        {/* d3-allow: the app's one display headline, on the one screen that introduces it — the type scale tops out at 24px for working screens */}
        <h2 className="max-w-[17ch] text-[2.5rem] leading-[1.08] font-semibold tracking-tight">
          Find the page, <span className="text-accent">not just the file.</span>
        </h2>
        <p className="mt-4 max-w-[38ch] text-16 text-fg-muted">
          A hundred-page scan from the filing cabinet goes in. Page 47 — the one
          you needed — comes back out, in seconds.
        </p>
        <ul className="mt-7 grid max-w-[44ch] gap-2.5 text-sm leading-snug text-fg-muted">
          <Claim title="Originals are never modified.">Every file is kept byte for byte.</Claim>
          <Claim title="Nothing is ever deleted automatically." />
          <Claim title="It runs on this machine.">
            Search works with no internet and no AI key.
          </Claim>
        </ul>
      </div>

      <footer className="relative z-10 mt-auto flex gap-3.5 pt-8 text-xs text-fg-faint">
        {build ? <span className="font-mono">{build}</span> : null}
        <span>self-hosted</span>
      </footer>
    </aside>
  );
}

function Claim({ title, children }: { title: string; children?: React.ReactNode }) {
  return (
    <li className="flex gap-2.5">
      <svg
        width="16"
        height="16"
        viewBox="0 0 16 16"
        fill="none"
        aria-hidden="true"
        className="mt-0.5 flex-none text-success"
      >
        <path
          d="M3.5 8.5l3 3 6-7"
          stroke="currentColor"
          strokeWidth="1.8"
          strokeLinecap="round"
          strokeLinejoin="round"
        />
      </svg>
      <span>
        <span className="font-medium text-fg">{title}</span>
        {children ? <> {children}</> : null}
      </span>
    </li>
  );
}

/** The running build, from the unauthenticated version report (REQ-152). Decorative. */
function useBuild(): string | null {
  const [build, setBuild] = useState<string | null>(null);
  useEffect(() => {
    let live = true;
    versionApi
      .report()
      .then((report) => {
        if (live) setBuild(report.services.api?.short ?? null);
      })
      .catch(() => {});
    return () => {
      live = false;
    };
  }, []);
  return build;
}

// d3-allow: tracking on an 11px uppercase eyebrow, unreadable set solid; the system has no letter-spacing scale by decision
const EYEBROW = "mb-1.5 text-11 font-semibold tracking-[0.08em] text-accent uppercase";

/** The heading every entry form opens with. */
export function EntryHeading({
  eyebrow,
  title,
  children,
}: {
  eyebrow?: string;
  title: string;
  children?: React.ReactNode;
}) {
  return (
    <header className="mb-6">
      {eyebrow ? (
        <p className={EYEBROW}>
          {eyebrow}
        </p>
      ) : null}
      <h1 className="text-2xl font-semibold tracking-tight">{title}</h1>
      {children ? <div className="mt-1.5 leading-relaxed text-fg-muted">{children}</div> : null}
    </header>
  );
}
