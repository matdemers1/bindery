import { useState } from "react";

import { ApiError, api } from "../api";

export default function Login({ onSignedIn }: { onSignedIn: () => Promise<void> }) {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(email, password);
      await onSignedIn();
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.status === 401
          ? "Those credentials were not accepted."
          : "Could not reach the server.",
      );
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
        <h1 className="text-2xl font-semibold tracking-tight">Bindery</h1>
        <p className="mt-1 mb-6 text-sm text-muted">
          There is no self-service registration. Accounts are created on the host.
        </p>

        <label className="mb-4 block text-sm">
          <span className="mb-1 block text-muted">Email</span>
          <input
            type="email"
            required
            autoComplete="username"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            className="w-full rounded-md border border-edge bg-ink px-3 py-2 outline-none focus:border-accent"
          />
        </label>

        <label className="mb-6 block text-sm">
          <span className="mb-1 block text-muted">Password</span>
          <input
            type="password"
            required
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            className="w-full rounded-md border border-edge bg-ink px-3 py-2 outline-none focus:border-accent"
          />
        </label>

        {error && <p className="mb-4 text-sm text-red-400">{error}</p>}

        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-md bg-accent px-3 py-2 font-medium text-ink disabled:opacity-50"
        >
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </div>
  );
}
