import { useId, useState } from "react";

import { ApiError, api } from "../api";
import { Logo } from "../components/brand/Logo";
import { Button } from "@d3cloud/ui";

export default function Login({ onSignedIn }: { onSignedIn: () => Promise<void> }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [needsCode, setNeedsCode] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // A rejected password and a rate-limit delay are both things a sighted user
  // reads the moment they appear; without a live region neither is spoken at
  // all, and the field that has to be retyped says nothing about them.
  const errorId = useId();
  const describedBy = error ? errorId : undefined;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(email, password, code || undefined);
      await onSignedIn();
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 429) {
        // The throttle names a delay; saying so beats a login that just
        // stops working.
        const seconds = caught.retryAfter ?? 60;
        setError(
          `Too many attempts. Try again in ${
            seconds < 90 ? `${seconds} seconds` : `${Math.ceil(seconds / 60)} minutes`
          }.`,
        );
      } else if (
        caught instanceof ApiError &&
        caught.status === 401 &&
        caught.message.includes("second factor")
      ) {
        // Only ever reached once the password was right, so asking for the
        // code here tells an anonymous caller nothing they did not have.
        setNeedsCode(true);
        setError(needsCode ? "That code was not accepted." : null);
      } else {
        setError(
          caught instanceof ApiError && caught.status === 401
            ? "Those credentials were not accepted."
            : "Could not reach the server.",
        );
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex h-full items-center justify-center p-6">
      <form
        onSubmit={submit}
        className="w-full max-w-sm rounded-xl border border-edge bg-surface p-8"
      >
        {/* The mascot rather than the plain mark: this is the one screen
            where nothing has happened yet and a little warmth costs nothing. */}
        <Logo size={44} variant="mascot" className="text-fg" />
        <h1 className="mt-3 text-2xl font-semibold tracking-tight">Bindery</h1>
        <p className="mt-1 mb-6 text-sm text-muted">
          Accounts are by invitation. If someone sent you a link, open that
          instead — it will set your password and sign you in.
        </p>

        <label className="mb-4 block text-sm">
          <span className="mb-1 block text-muted">Email</span>
          <input
            type="email"
            required
            autoComplete="username"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            className="w-full rounded-md border border-field bg-ink px-3 py-2 outline-none focus:border-accent"
          />
        </label>

        <label className="mb-6 block text-sm">
          <span className="mb-1 block text-muted">Password</span>
          <input
            type="password"
            required
            autoComplete="current-password"
            aria-invalid={Boolean(error) && !needsCode}
            aria-describedby={needsCode ? undefined : describedBy}
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className="w-full rounded-md border border-field bg-ink px-3 py-2 outline-none focus:border-accent"
          />
        </label>

        {needsCode && (
          <label className="mb-6 block text-sm">
            <span className="mb-1 block text-muted">
              Code from your authenticator
            </span>
            <input
              inputMode="numeric"
              autoComplete="one-time-code"
              autoFocus
              required
              aria-invalid={Boolean(error)}
              aria-describedby={describedBy}
              value={code}
              onChange={(event) => setCode(event.target.value)}
              placeholder="123456 — or a recovery code"
              className="w-full rounded-md border border-field bg-ink px-3 py-2 font-mono tracking-widest outline-none focus:border-accent"
            />
          </label>
        )}

        {error && (
          <p id={errorId} role="alert" className="mb-4 text-sm text-danger">
            {error}
          </p>
        )}

        <Button variant="primary" className="w-full" type="submit" disabled={busy}>
          {busy ? "Signing in…" : needsCode ? "Confirm" : "Sign in"}
        </Button>

        {/* The only way back in. There is no email here to send a link to, so
            recovery is an administrator reading a code out and this form
            accepting it — and a code with nowhere to be typed is an account
            lost. A whole-document load: /reset is handled above the router. */}
        <a
          href="/reset"
          className="mt-4 block text-center text-sm text-muted hover:text-fg"
        >
          I have a reset code
        </a>
      </form>
    </div>
  );
}
