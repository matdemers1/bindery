import { useId, useState } from "react";
import { ShieldPlus } from "lucide-react";

import { api, type VaultState } from "../../api";

/**
 * Creating the vault, once.
 *
 * The warning is not decoration. The passphrase is the only thing that can
 * recover a vault whose PIN has been switched off, and nothing in this
 * application — not an admin, not the database, not a backup — can reset it.
 * That has to be said before the field is filled in rather than after.
 */
export default function SetupForm({ onCreated }: { onCreated: (next: VaultState) => void }) {
  const [passphrase, setPassphrase] = useState("");
  const [again, setAgain] = useState("");
  const [pin, setPin] = useState("");
  const [understood, setUnderstood] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const mismatch = again.length > 0 && passphrase !== again;
  const mismatchId = useId();
  const ready =
    understood && passphrase.length >= 12 && passphrase === again && pin.length >= 4;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      onCreated(await api.vaultSetup(passphrase, pin));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className="max-w-xl space-y-4">
      <div className="rounded-lg border border-amber-900/60 bg-amber-950/20 p-3 text-sm text-amber-300">
        <p className="font-medium">This passphrase cannot be recovered.</p>
        <p className="mt-1 text-amber-300/80">
          It is not stored anywhere — only a key wrapped with it is. If you forget
          it and the PIN has been switched off, everything in the vault stays
          encrypted for good. Write it down somewhere physical before you go on.
        </p>
      </div>

      <div>
        <label htmlFor="setup-passphrase" className="block text-sm text-muted">
          Vault passphrase
          <span className="ml-1 text-xs">(at least 12 characters, different from your login)</span>
        </label>
        <input
          id="setup-passphrase"
          type="password"
          autoComplete="new-password"
          value={passphrase}
          onChange={(event) => setPassphrase(event.target.value)}
          className="mt-1 w-full rounded-lg border border-field bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
        />
      </div>

      <div>
        <label htmlFor="setup-again" className="block text-sm text-muted">
          Type it again
        </label>
        <input
          id="setup-again"
          type="password"
          autoComplete="new-password"
          aria-invalid={mismatch}
          aria-describedby={mismatch ? mismatchId : undefined}
          value={again}
          onChange={(event) => setAgain(event.target.value)}
          className="mt-1 w-full rounded-lg border border-field bg-surface px-3 py-2 text-sm outline-none focus:border-accent"
        />
        {mismatch && (
          <p id={mismatchId} role="alert" className="mt-1 text-xs text-red-400">
            These do not match.
          </p>
        )}
      </div>

      <div>
        <label htmlFor="setup-pin" className="block text-sm text-muted">
          PIN <span className="ml-1 text-xs">(for day to day; 4 digits or more)</span>
        </label>
        <input
          id="setup-pin"
          type="password"
          inputMode="numeric"
          autoComplete="off"
          value={pin}
          onChange={(event) => setPin(event.target.value.replace(/\D/g, ""))}
          className="mt-1 w-40 rounded-lg border border-field bg-surface px-3 py-2 font-mono text-lg tracking-[0.4em] outline-none focus:border-accent"
        />
        <p className="mt-1 text-xs text-muted">
          Five wrong entries switch the PIN off. Nothing is deleted — the
          passphrase still opens the vault and you can set a new PIN.
        </p>
      </div>

      <label className="flex items-start gap-2 text-sm text-muted">
        <input
          type="checkbox"
          checked={understood}
          onChange={(event) => setUnderstood(event.target.checked)}
          className="mt-0.5"
        />
        I have written the passphrase down somewhere I will still have it in ten
        years.
      </label>

      {error && (
        <p
          role="alert"
          className="rounded-lg border border-red-900/60 bg-red-950/20 px-3 py-2 text-sm text-red-300"
        >
          {error}
        </p>
      )}

      <button
        type="submit"
        disabled={!ready || busy}
        className="flex items-center gap-1.5 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-ink disabled:opacity-40"
      >
        <ShieldPlus size={14} />
        {busy ? "Creating…" : "Create the vault"}
      </button>
    </form>
  );
}
