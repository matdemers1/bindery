import { useCallback, useState } from "react";
import { KeyRound, ShieldCheck, HardDrive, Copy, Check } from "lucide-react";

import { ApiError, type Account, accountsApi } from "../../api";
import { useLiveQuery } from "../../live/LiveProvider";
import { Alert, Button, CodeInput, FormField, PageHeader, PasswordInput } from "@d3cloud/ui";

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

export default function AccountPage() {
  const [account, setAccount] = useState<Account | null>(null);

  const load = useCallback(async () => {
    setAccount(await accountsApi.me());
  }, []);
  useLiveQuery(["files", "documents"], load);

  if (!account) return null;

  return (
    <div className="mx-auto max-w-2xl space-y-5">
      <PageHeader icon={<KeyRound size={20} strokeWidth={1.8} />} title="Your account"
        description={
          <>
          {account.email}
          </>
        }
      />

      <Storage account={account} />
      <TwoFactor account={account} onChanged={load} />
      <ChangePassword />
    </div>
  );
}

function Storage({ account }: { account: Account }) {
  const { used_bytes, quota_bytes, files } = account.storage;
  const share = quota_bytes ? Math.min(1, used_bytes / quota_bytes) : 0;

  return (
    <section className="rounded-xl border border-edge bg-surface p-4">
      <h2 className="flex items-center gap-2 text-sm font-medium">
        <HardDrive size={15} className="text-muted" />
        Storage
      </h2>
      <p className="mt-1 text-sm text-muted">
        {humanBytes(used_bytes)} across {files} file{files === 1 ? "" : "s"}
        {quota_bytes ? ` of ${humanBytes(quota_bytes)}` : " — no limit set"}.
      </p>
      {quota_bytes && (
        <div className="mt-3 h-2 overflow-hidden rounded-full bg-ink">
          <div
            className={`h-full rounded-full ${
              share > 0.9 ? "bg-danger" : share > 0.75 ? "bg-warning" : "bg-accent"
            }`}
            style={{ width: `${Math.max(2, share * 100)}%` }}
          />
        </div>
      )}
    </section>
  );
}

/**
 * Enrolment is three steps and the middle one matters: the secret is not
 * active until a code from it has been verified. Storing it as active when it
 * is generated locks the account holder out of their own archive whenever the
 * enrolment did not take — a mistyped secret, a phone with the wrong clock, a
 * closed tab.
 */
function TwoFactor({ account, onChanged }: { account: Account; onChanged: () => Promise<void> }) {
  const [enrolling, setEnrolling] = useState<{ secret: string; uri: string } | null>(null);
  const [code, setCode] = useState("");
  const [recovery, setRecovery] = useState<string[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  async function start() {
    setError(null);
    setEnrolling(await accountsApi.totpStart());
  }

  async function confirm(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      setRecovery(await accountsApi.totpConfirm(code));
      setEnrolling(null);
      setCode("");
      await onChanged();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    }
  }

  async function disable() {
    setError(null);
    try {
      await accountsApi.totpDisable();
      await onChanged();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    }
  }

  return (
    <section className="rounded-xl border border-edge bg-surface p-4">
      <h2 className="flex items-center gap-2 text-sm font-medium">
        <ShieldCheck
          size={15}
          className={account.totp_enabled ? "text-success" : "text-muted"}
        />
        Two-factor authentication
        {account.is_admin && (
          <span className="rounded-full border border-edge px-2 py-0.5 text-11 text-muted">
            required for administrators
          </span>
        )}
      </h2>

      {account.totp_enabled ? (
        <>
          <p className="mt-1 text-sm text-muted">
            On. Signing in asks for a code from your authenticator.
          </p>
          {!account.is_admin && (
            <Button size="sm" className="mt-3" onClick={() => void disable()}>
              Turn off
            </Button>
          )}
          {account.is_admin && (
            <p className="mt-2 text-xs text-muted">
              This account can issue a password reset for every other account, so
              it cannot be left with a password alone.
            </p>
          )}
        </>
      ) : enrolling ? (
        <form onSubmit={confirm} className="mt-3">
          <p className="text-sm text-muted">
            Add this to your authenticator, then type the code it shows.
          </p>
          <div className="mt-3 flex items-center gap-2">
            <code className="flex-1 break-all rounded border border-edge bg-ink px-3 py-2 font-mono text-xs">
              {enrolling.secret}
            </code>
            <button
              type="button"
              onClick={() => {
                void navigator.clipboard.writeText(enrolling.secret);
                setCopied(true);
                window.setTimeout(() => setCopied(false), 1500);
              }}
              className="rounded border border-field p-2 text-muted hover:text-fg"
              aria-label="Copy the secret"
            >
              {copied ? <Check size={14} /> : <Copy size={14} />}
            </button>
          </div>
          <CodeInput
            aria-label="Code from your authenticator"
            autoComplete="one-time-code"
            required
            value={code}
            onValueChange={setCode}
            className="mt-3"
          />
          {error && <p className="mt-2 text-sm text-danger">{error}</p>}
          <Button variant="primary" className="mt-3" type="submit">
            Turn on
          </Button>
        </form>
      ) : (
        <>
          <p className="mt-1 text-sm text-muted">
            Off. Adding it means a stolen password is not enough on its own.
          </p>
          {error && <p className="mt-2 text-sm text-danger">{error}</p>}
          <Button variant="primary" className="mt-3" onClick={() => void start()}>
            Set up
          </Button>
        </>
      )}

      {recovery && (
        <Alert
          tone="warning"
          className="mt-4"
          title="Write these down now — they are not shown again."
        >
          Each works once, and gets you in if you lose your phone. Generating a
          new set retires these.
          <ul className="mt-2 grid grid-cols-2 gap-1 font-mono text-xs">
            {recovery.map((one) => (
              <li key={one}>{one}</li>
            ))}
          </ul>
        </Alert>
      )}
    </section>
  );
}

function ChangePassword() {
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      await accountsApi.changePassword(current, next);
      setDone(true);
      setCurrent("");
      setNext("");
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    }
  }

  return (
    <section className="rounded-xl border border-edge bg-surface p-4">
      <h2 className="text-sm font-medium">Change your password</h2>
      <p className="mt-1 text-sm text-muted">
        Every other signed-in device is signed out — which is usually the reason
        for changing it.
      </p>
      <form onSubmit={submit} className="mt-3 space-y-3">
        {/* Labelled, not placeholder-only: two password fields that go blank
            on the first keystroke leave nothing saying which one is which. */}
        <FormField label="Current password">
          <PasswordInput
            required
            autoComplete="current-password"
            value={current}
            onChange={(event) => setCurrent(event.target.value)}
          />
        </FormField>
        <FormField label="New password" help="At least 12 characters.">
          <PasswordInput
            required
            minLength={12}
            autoComplete="new-password"
            value={next}
            onChange={(event) => setNext(event.target.value)}
          />
        </FormField>
        {error && <p className="text-sm text-danger">{error}</p>}
        {done && <p className="text-sm text-success">Changed.</p>}
        <Button variant="primary" type="submit">
          Change it
        </Button>
      </form>
    </section>
  );
}
