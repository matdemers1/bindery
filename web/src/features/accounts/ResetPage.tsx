import { useId, useState } from "react";

import { ApiError, accountsApi } from "../../api";
import { Logo } from "../../components/brand/Logo";
import { Button } from "@d3cloud/ui";

/**
 * The other half of an administrator's reset (REQ-136).
 *
 * The issuing half shipped and this did not, which made the People screen's own
 * instruction — "this account can issue a password reset for every other one" —
 * an instruction to nowhere: a code was generated, read out over the phone, and
 * there was no form to type it into. On a self-hosted archive with no email and
 * no other recovery path, that is an account lost until somebody gets a shell on
 * the host, which is precisely the situation this project designs against.
 *
 * Unauthenticated, on the open internet, and handling a credential — so it
 * behaves like the login page it sits beside. The code is typed, never carried
 * in the URL, because a query string is written to history, to proxy logs and to
 * whatever the browser syncs. `POST /api/account/reset` throttles by address and
 * by IP, and answers "that code is not valid" to every way of being wrong, so
 * this screen deliberately does not improve on the message: telling somebody
 * *which* half was wrong tells an anonymous caller whether an account exists.
 */
export default function ResetPage() {
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const errorId = useId();
  const describedBy = error ? errorId : undefined;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (password !== confirm) {
      setError("Those two passwords are different.");
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await accountsApi.redeemReset(email, code, password);
      setDone(true);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 429) {
        const seconds = caught.retryAfter ?? 60;
        setError(
          `Too many attempts. Try again in ${
            seconds < 90 ? `${seconds} seconds` : `${Math.ceil(seconds / 60)} minutes`
          }.`,
        );
      } else if (caught instanceof ApiError && caught.status === 422) {
        // The one case where the server's own words are the useful ones: it
        // says what is wrong with the password that was chosen.
        setError(caught.message);
      } else if (caught instanceof ApiError && caught.status === 400) {
        setError(
          "That code was not accepted. Codes expire after a day and each works " +
            "once — ask for a new one.",
        );
      } else {
        setError("Could not reach the server.");
      }
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <div className="flex min-h-full items-center justify-center p-6">
        <div className="w-full max-w-sm rounded-xl border border-edge bg-surface p-8 text-center">
          <Logo size={44} variant="mascot" className="text-fg" />
          <h1 className="mt-3 text-2xl font-semibold tracking-tight">
            Password changed
          </h1>
          <p className="mt-3 text-sm text-muted">
            Everywhere that was signed in as you has been signed out. Sign in
            again with the new password.
          </p>
          {/* A whole-document load rather than a router navigation: the router
              lives inside the signed-in tree, which does not exist yet. */}
          <a
            href="/"
            className="mt-6 block w-full rounded-md bg-accent px-3 py-2 font-medium text-ink"
          >
            Sign in
          </a>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-xl border border-edge bg-surface p-8"
      >
        <Logo size={44} variant="mascot" className="text-fg" />
        <h1 className="mt-3 text-2xl font-semibold tracking-tight">
          Use a reset code
        </h1>
        <p className="mt-1 mb-6 text-sm text-muted">
          Whoever administers this archive can issue you one. They never see the
          password you choose here.
        </p>

        <label className="mb-4 block text-sm">
          <span className="mb-1 block text-muted">Email</span>
          <input
            type="email"
            required
            autoComplete="username"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            aria-invalid={Boolean(error)}
            aria-describedby={describedBy}
            className="w-full rounded-md border border-field bg-ink px-3 py-2 outline-none focus:border-accent"
          />
        </label>

        <label className="mb-4 block text-sm">
          <span className="mb-1 block text-muted">Reset code</span>
          <input
            required
            autoComplete="one-time-code"
            spellCheck={false}
            value={code}
            onChange={(event) => setCode(event.target.value)}
            aria-invalid={Boolean(error)}
            aria-describedby={describedBy}
            placeholder="ABCD-EFGH-JKLM"
            className="w-full rounded-md border border-field bg-ink px-3 py-2 font-mono uppercase tracking-widest outline-none focus:border-accent"
          />
        </label>

        <label className="mb-4 block text-sm">
          <span className="mb-1 block text-muted">New password</span>
          <input
            type="password"
            required
            minLength={12}
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            aria-invalid={Boolean(error)}
            aria-describedby={describedBy}
            className="w-full rounded-md border border-field bg-ink px-3 py-2 outline-none focus:border-accent"
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
            aria-invalid={Boolean(error)}
            aria-describedby={describedBy}
            className="w-full rounded-md border border-field bg-ink px-3 py-2 outline-none focus:border-accent"
          />
        </label>

        {error && (
          <p id={errorId} role="alert" className="mb-4 text-sm text-danger">
            {error}
          </p>
        )}

        <Button variant="primary" className="w-full" type="submit" disabled={busy}>
          {busy ? "Setting the password…" : "Set my password"}
        </Button>

        <a href="/" className="mt-4 block text-center text-sm text-muted hover:text-fg">
          Back to sign in
        </a>
      </form>
    </div>
  );
}
