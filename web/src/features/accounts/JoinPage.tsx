import { useCallback, useEffect, useState } from "react";

import { ApiError, accountsApi } from "../../api";
import { Logo } from "../../components/brand/Logo";

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
  const [confirm, setConfirm] = useState("");
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
    if (password !== confirm) {
      setError("Those two passwords are different.");
      return;
    }
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
      <Centered>
        <Logo size={44} variant="mascot" className="text-neutral-300" />
        <h1 className="mt-3 text-2xl font-semibold tracking-tight">Bindery</h1>
        <p className="mt-3 text-sm text-muted">{problem}</p>
        <p className="mt-4 text-sm text-muted">
          Ask whoever sent it for a new one — invitations expire, and each works
          once.
        </p>
      </Centered>
    );
  }

  if (!invite) {
    return <Centered>Checking that link…</Centered>;
  }

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <form
        onSubmit={submit}
        className="w-full max-w-md rounded-xl border border-edge bg-surface p-8"
      >
        <Logo size={44} variant="mascot" className="text-neutral-300" />
        <h1 className="mt-3 text-2xl font-semibold tracking-tight">
          Set up your archive
        </h1>
        <p className="mt-1 text-sm text-muted">
          for <span className="text-neutral-200">{invite.email}</span>
        </p>

        {invite.note && (
          <p className="mt-4 rounded-lg border border-edge bg-ink p-3 text-sm text-muted">
            {invite.note}
          </p>
        )}

        <ul className="mt-5 space-y-2 text-sm text-muted">
          <li>
            Your documents go in <span className="text-neutral-200">{invite.library_name}</span>,
            which only you can open.
          </li>
          <li>
            <span className="text-neutral-200">
              Whoever runs this server cannot read them.
            </span>{" "}
            They can see how much space you are using and whether anything went
            wrong with a file — not what is in it.
          </li>
          {invite.storage_quota_bytes && (
            <li>
              You have {humanBytes(invite.storage_quota_bytes)} of space. Ask for
              more if you run out.
            </li>
          )}
          <li>Nothing here is ever deleted automatically.</li>
        </ul>

        <label className="mt-6 mb-4 block text-sm">
          <span className="mb-1 block text-muted">Your name</span>
          <input
            value={displayName}
            onChange={(event) => setDisplayName(event.target.value)}
            autoComplete="name"
            placeholder="Optional"
            className="w-full rounded-md border border-edge bg-ink px-3 py-2 outline-none focus:border-accent"
          />
        </label>

        <label className="mb-4 block text-sm">
          <span className="mb-1 block text-muted">Choose a password</span>
          <input
            type="password"
            required
            minLength={12}
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className="w-full rounded-md border border-edge bg-ink px-3 py-2 outline-none focus:border-accent"
          />
          <span className="mt-1 block text-xs text-muted">
            At least 12 characters. Four unrelated words beats one clever word.
          </span>
        </label>

        <label className="mb-6 block text-sm">
          <span className="mb-1 block text-muted">And again</span>
          <input
            type="password"
            required
            autoComplete="new-password"
            value={confirm}
            onChange={(event) => setConfirm(event.target.value)}
            className="w-full rounded-md border border-edge bg-ink px-3 py-2 outline-none focus:border-accent"
          />
        </label>

        {error && <p className="mb-4 text-sm text-red-400">{error}</p>}

        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-md bg-accent px-3 py-2 font-medium text-ink disabled:opacity-50"
        >
          {busy ? "Setting up…" : "Create my archive"}
        </button>
      </form>
    </div>
  );
}

function Centered({ children }: { children: React.ReactNode }) {
  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <div className="w-full max-w-sm rounded-xl border border-edge bg-surface p-8 text-center">
        {children}
      </div>
    </div>
  );
}
