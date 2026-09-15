import { useCallback, useId, useState } from "react";
import { Link } from "react-router";
import { Users, UserPlus, Copy, Check, Lock, Ban, RotateCcw, ShieldCheck } from "lucide-react";

import { ApiError, type AdminAccount, type AdminInvitation, accountsApi } from "../../api";
import { useLiveQuery } from "../../live/LiveProvider";
import { Alert, Button, IconButton, Input, PageHeader } from "@d3cloud/ui";

const GB = 1024 ** 3;

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
 * Administration of *accounts*.
 *
 * Every figure on this screen is a count, a state or a timestamp. There is no
 * document title anywhere in it, and there is no route from here to one —
 * that is ADR-009, and it is what lets the operator say honestly to the people
 * they invite that they cannot read their documents.
 *
 * The screen is deliberately a little blunt about that, because a promise
 * nobody is told about is not a promise.
 */
export default function AdminPage() {
  const [accounts, setAccounts] = useState<AdminAccount[]>([]);
  const [invitations, setInvitations] = useState<AdminInvitation[]>([]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    const [people, invites] = await Promise.all([
      accountsApi.accounts(),
      accountsApi.invitations(),
    ]);
    setAccounts(people);
    setInvitations(invites);
  }, []);
  useLiveQuery(["settings", "files"], load);

  async function act(what: () => Promise<unknown>) {
    setError(null);
    try {
      await what();
      await load();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    }
  }

  return (
    <div className="mx-auto max-w-5xl space-y-5">
      <PageHeader icon={<Users size={20} strokeWidth={1.8} />} title="People"
        description={
          <>
          Accounts, invitations and storage. Nothing on this screen can show you
          what is inside anyone's documents — including yours, which live in the
          archive like everyone else's.
          </>
        }
      />

      {accounts.some((a) => a.is_admin && !a.totp_enabled) && (
        <Alert
          tone="warning"
          title="An administrator here has no two-factor authentication."
        >
          Granting administrator rights now requires it (REQ-156), but the
          account that existed before this shipped was made an administrator by
          the migration — it had to be, or nobody could reach this screen. Set it
          up on{" "}
          <Link to="/account" className="underline">
            your account
          </Link>
          . This account can issue a password reset for every other one.
        </Alert>
      )}

      {error && (
        <Alert tone="danger" dynamic>
          {error}
        </Alert>
      )}

      <Invite onInvited={load} />

      <section className="overflow-hidden rounded-xl border border-edge bg-surface">
        <table className="w-full text-sm">
          <thead className="border-b border-edge text-left text-xs text-muted">
            <tr>
              <th className="px-4 py-2 font-medium">Account</th>
              <th className="px-4 py-2 font-medium">Storage</th>
              <th className="px-4 py-2 font-medium">Two-factor</th>
              <th className="px-4 py-2 font-medium">State</th>
              <th className="px-4 py-2" />
            </tr>
          </thead>
          <tbody>
            {accounts.map((account) => (
              <Row key={account.id} account={account} act={act} />
            ))}
          </tbody>
        </table>
      </section>

      {invitations.length > 0 && (
        <section className="rounded-xl border border-edge bg-surface p-4">
          <h2 className="text-sm font-medium">Invitations</h2>
          <ul className="mt-2 divide-y divide-edge text-sm">
            {invitations.map((invite) => (
              <li key={invite.id} className="flex items-center gap-3 py-2">
                <span className="flex-1">{invite.email}</span>
                <span className="text-xs text-muted">
                  {invite.accepted_at
                    ? "accepted"
                    : invite.revoked_at
                      ? "withdrawn"
                      : `expires ${invite.expires_at.slice(0, 10)}`}
                </span>
                {!invite.accepted_at && !invite.revoked_at && (
                  <Button size="sm" onClick={() => void act(() => accountsApi.revokeInvite(invite.id))}>
                    Withdraw
                  </Button>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}

function Row({
  account,
  act,
}: {
  account: AdminAccount;
  act: (what: () => Promise<unknown>) => Promise<void>;
}) {
  const [code, setCode] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [editingQuota, setEditingQuota] = useState(false);
  const [quotaGb, setQuotaGb] = useState("");
  const quotaFieldId = useId();
  const locked = account.locked_until && new Date(account.locked_until) > new Date();
  const share = account.storage_quota_bytes
    ? Math.min(1, account.used_bytes / account.storage_quota_bytes)
    : 0;

  return (
    <tr className="border-b border-edge/60 last:border-0 align-top">
      <td className="px-4 py-3">
        <div>{account.display_name ?? account.email}</div>
        {account.display_name && (
          <div className="text-xs text-muted">{account.email}</div>
        )}
        {account.is_admin && (
          <span className="mt-1 inline-block rounded-full border border-accent/50 px-2 py-0.5 text-11 text-accent">
            administrator
          </span>
        )}
        {code && (
          <Alert tone="warning" className="mt-2">
            Read this out. It works once and expires in a day. They enter it
            on the sign-in page, under <em>Use a reset code</em>.
            <div className="mt-1 flex items-center gap-2">
              <code className="font-mono text-sm">{code}</code>
              <IconButton
                size="sm"
                label="Copy the code"
                icon={copied ? <Check size={13} /> : <Copy size={13} />}
                onClick={() => {
                  void navigator.clipboard.writeText(code);
                  setCopied(true);
                  window.setTimeout(() => setCopied(false), 1500);
                }}
              />
            </div>
          </Alert>
        )}
      </td>
      <td className="px-4 py-3">
        <div className="text-xs">
          {humanBytes(account.used_bytes)}
          {account.storage_quota_bytes
            ? ` of ${humanBytes(account.storage_quota_bytes)}`
            : " — no limit"}
        </div>
        {account.storage_quota_bytes && (
          <div className="mt-1 h-1.5 w-24 overflow-hidden rounded-full bg-ink">
            <div
              className={`h-full ${share > 0.9 ? "bg-danger" : share > 0.75 ? "bg-warning" : "bg-accent"}`}
              style={{ width: `${Math.max(3, share * 100)}%` }}
            />
          </div>
        )}
        {/* The quota was settable at invitation and nowhere else, so changing
            one afterwards meant a psql write against the account table — on a
            number this screen already displays and enforces. Blank means no
            limit, which is what the endpoint reads a null as. */}
        {editingQuota ? (
          <form
            className="mt-2 flex items-center gap-1"
            onSubmit={(event) => {
              event.preventDefault();
              const trimmed = quotaGb.trim();
              const bytes = trimmed === "" ? null : Math.round(Number(trimmed) * GB);
              if (bytes !== null && !Number.isFinite(bytes)) return;
              void act(async () => {
                await accountsApi.setQuota(account.id, bytes);
                setEditingQuota(false);
              });
            }}
          >
            <label htmlFor={quotaFieldId} className="sr-only">
              Storage quota in GB for {account.email}
            </label>
            <Input
              size="sm"
              id={quotaFieldId}
              value={quotaGb}
              onChange={(event) => setQuotaGb(event.target.value)}
              inputMode="decimal"
              placeholder="no limit"
              className="w-20"
            />
            <span className="text-xs text-muted">GB</span>
            <Button size="sm" type="submit">
              Save
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setEditingQuota(false)}>
              Cancel
            </Button>
          </form>
        ) : (
          <Button size="sm" className="mt-1.5" onClick={() => { setQuotaGb( account.storage_quota_bytes ? String(Math.round((account.storage_quota_bytes / GB) * 10) / 10) : "", ); setEditingQuota(true); }}>
            Change quota
          </Button>
        )}
      </td>
      <td className="px-4 py-3 text-xs">
        {account.totp_enabled ? (
          <span className="text-success">on</span>
        ) : (
          <span className="text-muted">off</span>
        )}
      </td>
      <td className="px-4 py-3 text-xs">
        {!account.is_active ? (
          <span className="text-danger">suspended</span>
        ) : locked ? (
          <span className="text-warning">locked out</span>
        ) : (
          <span className="text-muted">active</span>
        )}
      </td>
      <td className="px-4 py-3">
        <div className="flex flex-wrap justify-end gap-1.5">
          <Button size="sm" onClick={() => void act(async () => { const issued = await accountsApi.resetCode(account.id); setCode(issued.code); }) }>
            Reset code
          </Button>
          {locked && (
            <Button size="sm" onClick={() => void act(() => accountsApi.unlock(account.id))}
              icon={<Lock size={11} />}
            >
              Unlock
            </Button>
          )}
          {!account.is_admin && (
            <Button size="sm" onClick={() => void act(() => accountsApi.grantAdmin(account.id))}
              icon={<ShieldCheck size={11} />}
            >
              Make admin
            </Button>
          )}
          {account.is_active ? (
            <Button variant="danger-ghost" size="sm" onClick={() => void act(() => accountsApi.suspend(account.id))}
              icon={<Ban size={11} />}
            >
              Suspend
            </Button>
          ) : (
            <Button size="sm" onClick={() => void act(() => accountsApi.restore(account.id))}
              icon={<RotateCcw size={11} />}
            >
              Restore
            </Button>
          )}
        </div>
      </td>
    </tr>
  );
}

function Invite({ onInvited }: { onInvited: () => Promise<void> }) {
  const [email, setEmail] = useState("");
  const [libraryName, setLibraryName] = useState("Documents");
  const [quotaGb, setQuotaGb] = useState("10");
  const [note, setNote] = useState("");
  const [link, setLink] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setError(null);
    try {
      const issued = await accountsApi.invite({
        email,
        library_name: libraryName,
        storage_quota_bytes: quotaGb ? Math.round(Number(quotaGb) * GB) : null,
        note: note || null,
      });
      // The API returns a path, not a URL — it does not know the public
      // hostname, and a guessed one produces a link that silently goes nowhere.
      setLink(`${window.location.origin}${issued.path}`);
      setEmail("");
      setNote("");
      await onInvited();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    }
  }

  return (
    <section className="rounded-xl border border-edge bg-surface p-4">
      <h2 className="flex items-center gap-2 text-sm font-medium">
        <UserPlus size={15} className="text-muted" />
        Invite someone
      </h2>
      <p className="mt-1 text-sm text-muted">
        There is no email service here, so this gives you a link to send them
        yourself. It works once and expires in a fortnight.
      </p>

      <form onSubmit={submit} className="mt-3 grid gap-3 sm:grid-cols-2">
        <Input
          type="email"
          required
          aria-label="Their email address"
          placeholder="their@email.address"
          value={email}
          onChange={(event) => setEmail(event.target.value)}
        />
        <Input
          required
          aria-label="What their library is called"
          placeholder="What their library is called"
          value={libraryName}
          onChange={(event) => setLibraryName(event.target.value)}
        />
        <label className="text-sm">
          <span className="mb-1 block text-xs text-muted">Space, in GB (blank for no limit)</span>
          <Input
            inputMode="decimal"
            value={quotaGb}
            onChange={(event) => setQuotaGb(event.target.value)}
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-xs text-muted">A note they will see</span>
          <Input
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="Optional"
          />
        </label>
        <div className="sm:col-span-2">
          {error && <p className="mb-2 text-sm text-danger">{error}</p>}
          <Button variant="primary" type="submit">
            Create the link
          </Button>
        </div>
      </form>

      {link && (
        <Alert
          tone="warning"
          className="mt-3"
          title="Send this to them. It is not stored and will not be shown again."
        >
          <div className="flex items-center gap-2">
            <code className="flex-1 break-all rounded border border-edge bg-ink px-2 py-1.5 font-mono text-xs">
              {link}
            </code>
            <IconButton
              variant="secondary"
              label="Copy the link"
              icon={copied ? <Check size={14} /> : <Copy size={14} />}
              onClick={() => {
                void navigator.clipboard.writeText(link);
                setCopied(true);
                window.setTimeout(() => setCopied(false), 1500);
              }}
            />
          </div>
        </Alert>
      )}
    </section>
  );
}
