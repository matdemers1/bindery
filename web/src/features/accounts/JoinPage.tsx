import { useCallback, useEffect, useState } from "react";
import { Alert, Button, FormField, Input, PasswordInput, Skeleton } from "@d3cloud/ui";

import { ApiError, accountsApi } from "../../api";
import { EntryHeading, EntryShell } from "../entry/EntryShell";
import { judgePassword } from "../entry/strength";

function humanBytes(bytes: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value < 10 && unit > 0 ? value.toFixed(1) : Math.round(value)} ${units[unit]}`;
}

/**
 * The first screen anyone invited will ever see.
 *
 * It exists unauthenticated on the open internet, so it says as little as it
 * can while still being usable: a bad token gets "this link is not valid" and
 * nothing about whether it once was, who it was for, or whether the archive has
 * an account at that address.
 *
 * What it does say is what the person is agreeing to — whose server this is,
 * what they get, and the one fact that matters most to somebody handing over
 * their medical records: that the person running it cannot read them.
 */
export default function JoinPage({ token }: { token: string }) {
  const [invite, setInvite] = useState<Awaited<
    ReturnType<typeof accountsApi.previewInvite>
  > | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      setInvite(await accountsApi.previewInvite(token));
    } catch (caught) {
      setProblem(
        caught instanceof ApiError
          ? caught.message
          : "Could not reach the server.",
      );
    }
  }, [token]);

  useEffect(() => {
    // An async data load: the state is genuinely unavailable on the first
    // render, so the extra pass is the point rather than a mistake.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    void load();
  }, [load]);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await accountsApi.acceptInvite(token, password, displayName || null);
      // A full reload rather than a router navigation: the session cookie is
      // new, and everything above this component was mounted without one.
      window.location.assign("/");
    } catch (caught) {
      setError(
        caught instanceof ApiError ? caught.message : "Could not reach the server.",
      );
      setBusy(false);
    }
  }

  if (problem) {
    return (
      <EntryShell>
        {/* The server's own words lead ("that invitation has expired"), in
            sentence case, because they are the only specific fact here. */}
        <EntryHeading title="This link can't be used">
          {problem.charAt(0).toUpperCase() + problem.slice(1)}.
        </EntryHeading>
        <Alert tone="info">
          Invitations work once and expire. Ask whoever sent it for a new one — and
          if you already joined, your account is still there.
        </Alert>
        <a href="/" className="entry-button mt-6">
          Go to sign in
        </a>
      </EntryShell>
    );
  }

  if (!invite) {
    return (
      <EntryShell wide>
        <div aria-busy="true" aria-label="Checking that link" className="grid gap-3">
          <Skeleton className="h-4 w-24" />
          <Skeleton className="h-7 w-56" />
          <Skeleton className="mt-4 h-16 w-full" />
        </div>
      </EntryShell>
    );
  }

  return (
    <EntryShell wide>
      <EntryHeading eyebrow="Invitation" title="Join the archive">
        As <span className="font-medium text-fg">{invite.email}</span>.
      </EntryHeading>

      <div className="mb-6 grid gap-3 rounded-lg border border-border bg-bg-sunken p-4 text-sm text-fg-muted">
        <p>
          Your documents go in{" "}
          <span className="font-medium text-fg">{invite.library_name}</span>
          {invite.storage_quota_bytes ? (
            <>, with {humanBytes(invite.storage_quota_bytes)} of space</>
          ) : null}
          .
        </p>
        {invite.note ? (
          <p className="border-l-2 border-border pl-3 text-fg italic">{invite.note}</p>
        ) : null}
        <ul className="grid gap-2 border-t border-border pt-3">
          <li>
            <span className="font-medium text-fg">Only you can open your library.</span>{" "}
            Whoever runs this server can see how much space you use and whether a
            file went wrong — not what is in it.
          </li>
          <li>
            <span className="font-medium text-fg">Nothing here is ever deleted automatically.</span>
          </li>
        </ul>
      </div>

      <form onSubmit={submit} className="grid gap-5">
        <FormField label="Your name" optional>
          <Input
            size="lg"
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            autoComplete="name"
          />
        </FormField>

        {/* Hidden, so a password manager saves the new password against the
            right address rather than against nothing. */}
        <input type="email" name="username" autoComplete="username" value={invite.email} readOnly hidden />

        <FormField
          label="Choose a password"
          error={error ?? undefined}
          help="At least 12 characters. Four unrelated words beat one clever word."
        >
          <PasswordInput
            size="lg"
            required
            minLength={12}
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            strength={judgePassword(password, invite.email)}
          />
        </FormField>

        <Button variant="primary" size="lg" type="submit" className="w-full" loading={busy}>
          Join and sign in
        </Button>
      </form>
    </EntryShell>
  );
}
