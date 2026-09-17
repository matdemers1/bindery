import { useEffect, useState } from "react";

import { oidcApi, oidcPaths, type OidcStatus } from "../../api";

/**
 * The *Sign in with D3 Auth* button, and the three states it has (Phase 20, REQ-209).
 *
 * - **off** — nothing at all. Not a disabled button, not an explanation: an archive whose
 *   operator has never configured a provider should not mention one.
 * - **optional, provider down** — also nothing. The password form below it works, and an
 *   affordance that cannot work is worse than an absent one.
 * - **required, provider down** — a disabled button that says why, because here it *is* the way
 *   in, and silence would read as a broken page. The owner's local sign-in stays reachable.
 *
 * A whole-document navigation rather than a fetch: the provider answers with a redirect, and the
 * browser has to follow it.
 */
export function SignInWithD3Auth({ onState }: { onState?: (status: OidcStatus) => void }) {
  const [status, setStatus] = useState<OidcStatus | null>(null);

  useEffect(() => {
    let live = true;
    void (async () => {
      try {
        const answer = await oidcApi.status();
        if (!live) return;
        setStatus(answer);
        onState?.(answer);
      } catch {
        // An archive that cannot answer is one where SSO is not configured, or not reachable.
        // Either way the password form is the way in and this says nothing.
        if (live) setStatus({ mode: "off", ready: false, issuer: null });
      }
    })();
    return () => {
      live = false;
    };
  }, [onState]);

  if (!status || status.mode === "off") return null;
  if (!status.ready && status.mode !== "required") return null;

  const unavailable = !status.ready;
  return (
    <div className="grid gap-3">
      <div className="flex items-center gap-3 text-xs text-fg-faint">
        <span className="h-px flex-1 bg-border" />
        or
        <span className="h-px flex-1 bg-border" />
      </div>

      {unavailable ? (
        <p className="entry-sso entry-sso--down" role="status">
          <KeyGlyph />
          D3 Auth is unavailable
          <span className="block text-xs font-normal text-fg-faint">
            Try again shortly, or sign in with a password below.
          </span>
        </p>
      ) : (
        <a className="entry-sso" href={oidcPaths.signIn}>
          <KeyGlyph />
          Sign in with D3 Auth
        </a>
      )}
    </div>
  );
}

function KeyGlyph() {
  return (
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.9"
      strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" className="flex-none">
      <circle cx="8" cy="12" r="4" />
      <path d="M12 12h9M18 12v3M15.5 12v2" />
    </svg>
  );
}
