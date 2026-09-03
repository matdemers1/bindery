import { Settings as SettingsIcon } from "lucide-react";

import PageHeader from "../../components/PageHeader";
import { useCallback, useEffect, useState } from "react";

import LogViewer from "../../components/LogViewer";
import PendingReviewPanel from "../../components/PendingReview";
import { ApiError, api, type ApiTokenRecord, type IssuedApiToken, type OffsiteTest, type Settings, type SettingsTest } from "../../api";

/**
 * Settings (screen 19).
 *
 * The API key above all — "add your key" is the first thing anyone does, and
 * the alternative was editing a compose file over SSH.
 *
 * The key is **never sent back to the browser**. The form shows whether one is
 * configured and its last four characters; a settings page that renders your
 * key into the DOM has handed it to every extension you run.
 */
export default function SettingsPage() {
  const [settings, setSettings] = useState<Settings | null>(null);
  const [key, setKey] = useState("");
  const [busy, setBusy] = useState(false);
  const [test, setTest] = useState<SettingsTest | null>(null);
  const [notice, setNotice] = useState<string | null>(null);

  const load = useCallback(() => api.settings().then(setSettings).catch(() => {}), []);
  useEffect(() => {
    void load();
  }, [load]);

  async function save(value: string | null) {
    setBusy(true);
    setTest(null);
    setNotice(null);
    try {
      setSettings(await api.updateSettings({ anthropic_api_key: value ?? "" }));
      setKey("");
      setNotice(value ? "Key saved. Testing it now…" : "Key cleared.");
      if (value) setTest(await api.testAi());
    } catch (error) {
      setNotice(error instanceof ApiError ? error.message : "Could not save.");
    } finally {
      setBusy(false);
    }
  }

  if (!settings) {
    return <div className="mx-auto max-w-3xl py-8 text-center text-muted">Loading…</div>;
  }

  return (
    <div className="mx-auto max-w-3xl">
      <PageHeader icon={SettingsIcon} title="Settings">
        Configuration you can change without a restart.
      </PageHeader>

      <section className="rounded-lg border border-edge bg-surface p-5">
        <h2 className="text-base font-medium">Classification</h2>
        <p className="mt-1 text-sm text-muted">
          Bindery works without this. Documents are OCR'd, indexed and searchable
          either way — a key only adds titles, dates and tags.
        </p>

        <div className="mt-4 flex items-center gap-2 text-sm">
          <span
            className={`h-2 w-2 rounded-full ${
              settings.anthropic_key_configured ? "bg-emerald-400" : "bg-muted"
            }`}
          />
          {settings.anthropic_key_configured ? (
            <span>
              Key configured <span className="font-mono text-muted">{settings.anthropic_key_hint}</span>
            </span>
          ) : (
            <span className="text-muted">No key — classification is deferred</span>
          )}
        </div>

        <form
          className="mt-4 flex flex-wrap gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            if (key.trim()) void save(key.trim());
          }}
        >
          <label htmlFor="claude-api-key" className="sr-only">
            Claude API key
          </label>
          <input
            id="claude-api-key"
            type="password"
            value={key}
            onChange={(event) => setKey(event.target.value)}
            placeholder="sk-ant-…"
            autoComplete="off"
            spellCheck={false}
            className="min-w-0 flex-1 rounded-md border border-field bg-ink px-3 py-2 font-mono text-sm outline-none focus:border-accent"
          />
          <button
            type="submit"
            disabled={busy || !key.trim()}
            className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-ink disabled:opacity-40"
          >
            {busy ? "Saving…" : "Save and test"}
          </button>
          {settings.anthropic_key_configured && (
            <button
              type="button"
              onClick={() => void save(null)}
              disabled={busy}
              className="rounded-md border border-edge px-3 py-2 text-sm disabled:opacity-40"
            >
              Remove
            </button>
          )}
        </form>

        <p className="mt-2 text-xs text-muted">
          Stored encrypted in the database, never sent back to this page, and never
          written to the audit log.
        </p>

        {notice && <p className="mt-3 text-sm text-accent">{notice}</p>}

        {test && (
          <div
            className={`mt-3 rounded-md border p-3 text-sm ${
              test.ok
                ? "border-emerald-500/40 text-emerald-300"
                : "border-red-500/40 text-red-300"
            }`}
          >
            <p className="font-medium">{test.ok ? "The key works." : "The key was rejected."}</p>
            <p className="mt-1 font-mono text-xs break-words opacity-90">{test.detail}</p>
            {test.ok && test.input_tokens !== null && (
              <p className="mt-1 text-xs opacity-75">
                {test.input_tokens} in / {test.output_tokens} out on this check.
              </p>
            )}
          </div>
        )}

        <button
          onClick={async () => {
            setBusy(true);
            try {
              setTest(await api.testAi());
            } finally {
              setBusy(false);
            }
          }}
          disabled={busy}
          className="mt-4 rounded-md border border-edge px-3 py-1.5 text-sm disabled:opacity-40"
        >
          Test the current key
        </button>
      </section>

      <ModelPicker settings={settings} onSaved={load} />

      {/* Directly under the key field: finishing that form is exactly the
          moment someone wants to be asked about the documents that arrived
          before it was filled in. */}
      <div className="mt-6">
        <PendingReviewPanel key={String(settings.anthropic_key_configured)} />
      </div>

      <NotificationSettings settings={settings} onSaved={load} />
      <OffsiteReplication settings={settings} onSaved={load} />
      <ApiTokens />

      <section className="mt-6">
        <h2 className="text-base font-medium">Diagnostics</h2>
        <p className="mb-3 mt-1 max-w-2xl text-sm text-muted">
          Everything the archive logs, kept and searchable. This used to go to the
          container&apos;s output and nowhere else, which meant explaining a failure
          started with finding a terminal.
        </p>
        {/* Warnings and above by default: the reason anyone opens a log is to
            find the red line, and the informational chatter of a healthy
            pipeline buries it. */}
        <LogViewer title="Recent activity" defaultLevel="warning" />
      </section>
    </div>
  );
}

/**
 * Which model does the classifying.
 *
 * A real trade-off rather than a better/worse ladder, so each option carries
 * the sentence someone needs to decide — and the price, since cost is the
 * reason anyone opens this list at all.
 *
 * Changing it is safe and not retroactive: every classification records the
 * model that produced it, so earlier documents stay traceable to what actually
 * classified them and the spend figure keeps costing them at their own rates.
 */
function ModelPicker({ settings, onSaved }: { settings: Settings; onSaved: () => void }) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  async function choose(id: string) {
    if (id === settings.model) return;
    setBusy(true);
    setNotice(null);
    try {
      await api.updateSettings({ model: id });
      setNotice("Saved. It applies to the next document classified, not to past ones.");
      onSaved();
    } catch (error) {
      setNotice(error instanceof ApiError ? error.message : "Could not save.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="mt-6 rounded-lg border border-edge bg-surface p-5">
      <h2 className="text-base font-medium">Model</h2>
      <p className="mt-1 max-w-2xl text-sm text-muted">
        Used for classification and for answering questions on the Ask screen.
      </p>

      <fieldset className="mt-4 space-y-2" disabled={busy}>
        <legend className="sr-only">Choose a model</legend>
        {settings.available_models.map((choice) => {
          const selected = choice.id === settings.model;
          return (
            <label
              key={choice.id}
              className={`flex cursor-pointer gap-3 rounded border p-3 ${
                selected ? "border-accent bg-ink" : "border-edge hover:border-accent/50"
              }`}
            >
              <input
                type="radio"
                name="model"
                value={choice.id}
                checked={selected}
                onChange={() => void choose(choice.id)}
                className="mt-1"
              />
              <span className="min-w-0">
                <span className="flex flex-wrap items-baseline gap-2">
                  <span className="text-sm font-medium">{choice.name}</span>
                  <span className="font-mono text-xs text-muted">
                    ${choice.input_per_mtok}/M in · ${choice.output_per_mtok}/M out
                  </span>
                </span>
                <span className="mt-0.5 block text-sm text-muted">{choice.blurb}</span>
              </span>
            </label>
          );
        })}
      </fieldset>

      {notice && <p className="mt-3 text-sm text-muted">{notice}</p>}

      <dl className="mt-4 grid grid-cols-[10rem_1fr] gap-y-1 text-xs">
        <dt className="text-muted">Prompt version</dt>
        <dd className="font-mono text-muted">{settings.prompt_version}</dd>
      </dl>
      <p className="mt-2 text-xs text-muted">
        Every classification records the model and prompt version that produced it, so
        changing either leaves earlier documents traceable to what actually classified
        them — and the spend figure on Trust keeps costing them at their own rates.
      </p>
    </section>
  );
}

/**
 * Where a stopped pipeline goes to be noticed (REQ-110).
 *
 * A webhook rather than an integration, because ntfy, Pushover, Gotify, Slack
 * and Discord all accept one and a self-hosted archive should not require an
 * account with anybody. Treated as a secret: most of these services put the
 * credential in the URL itself.
 */
function NotificationSettings({
  settings,
  onSaved,
}: {
  settings: Settings;
  onSaved: () => void;
}) {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  async function save(value: string) {
    setBusy(true);
    setNotice(null);
    try {
      await api.updateSettings({ notify_webhook_url: value });
      setUrl("");
      setNotice(value ? "Saved." : "Notifications turned off.");
      onSaved();
    } catch (error) {
      setNotice(error instanceof ApiError ? error.message : "Could not save.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="mt-6 rounded-lg border border-edge bg-surface p-5">
      <h2 className="text-base font-medium">Notifications</h2>
      <p className="mt-1 text-sm text-muted">
        The worst case for an archive is silence: a document that never made it in,
        discovered years later when you go looking. If the pipeline stops or a
        document permanently fails, Bindery will POST to this URL.
      </p>
      <p className="mt-2 text-sm text-muted">
        Any service that accepts a JSON POST works — ntfy, Pushover, Gotify, a Slack
        or Discord hook. Repeats are held back for six hours: an alert you learn to
        ignore is worse than no alert.
      </p>

      <div className="mt-4 flex items-center gap-2 text-sm">
        <span
          aria-hidden
          className={`inline-block h-2 w-2 rounded-full ${
            settings.notify_webhook_configured ? "bg-emerald-400" : "bg-muted"
          }`}
        />
        {settings.notify_webhook_configured ? (
          <span>
            Configured <span className="font-mono text-muted">{settings.notify_webhook_hint}</span>
          </span>
        ) : (
          <span className="text-muted">Not configured — nothing will be sent.</span>
        )}
      </div>

      <form
        className="mt-3 flex flex-wrap gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          if (url.trim()) void save(url.trim());
        }}
      >
        <label htmlFor="webhook" className="sr-only">
          Webhook URL
        </label>
        <input
          id="webhook"
          type="url"
          value={url}
          onChange={(event) => setUrl(event.target.value)}
          placeholder="https://ntfy.sh/your-topic"
          className="w-96 max-w-full rounded border border-field bg-ink px-2 py-1.5 text-sm"
        />
        <button
          type="submit"
          disabled={busy || !url.trim()}
          className="rounded bg-accent px-3 py-1.5 text-sm font-medium text-ink disabled:opacity-40"
        >
          Save
        </button>
        {settings.notify_webhook_configured && (
          <button
            type="button"
            disabled={busy}
            onClick={() => void save("")}
            className="rounded border border-edge px-3 py-1.5 text-sm disabled:opacity-40"
          >
            Turn off
          </button>
        )}
      </form>
      {notice && <p className="mt-2 text-sm text-muted">{notice}</p>}
    </section>
  );
}

const SCOPE_LABELS: Record<string, string> = {
  read: "Search and read",
  upload: "Add files",
  export: "Export and back up",
  admin: "Everything you can do",
};

/**
 * Scoped API tokens (REQ-107).
 *
 * The secret is shown exactly once, because only its hash is stored. That is
 * stated plainly rather than implied — a person who closes this panel expecting
 * to copy the token later has lost it, and should have been told so first.
 */
/**
 * Offsite replication (T-13.2, REQ-159, ADR-010).
 *
 * The third copy of 3-2-1. Copies 1 and 2 are the RAID pool and the local
 * backup target — both in the same building, on the same array, one fire from
 * zero. This is the one that leaves.
 *
 * The secret access key follows the same rule as the Anthropic key: stored
 * encrypted, never sent back, four characters shown so you can tell which
 * credential is loaded. The other four fields are returned in full on purpose —
 * none is a credential, and the KMS key id in particular has to be readable
 * *outside* the archive, because a restore needs to know which key to ask for.
 */
function OffsiteReplication({
  settings,
  onSaved,
}: {
  settings: Settings;
  onSaved: () => void;
}) {
  const [keyId, setKeyId] = useState(settings.aws_access_key_id ?? "");
  const [secret, setSecret] = useState("");
  const [bucket, setBucket] = useState(settings.offsite_bucket ?? "");
  const [region, setRegion] = useState(settings.offsite_region ?? "us-east-1");
  const [kms, setKms] = useState(settings.offsite_kms_key_id ?? "");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [test, setTest] = useState<OffsiteTest | null>(null);
  const [testing, setTesting] = useState(false);

  const configured =
    settings.aws_secret_configured && !!settings.aws_access_key_id && !!settings.offsite_bucket;

  async function runTest() {
    setTesting(true);
    setNotice(null);
    try {
      setTest(await api.testOffsite());
    } catch (error) {
      setTest({
        ok: false,
        detail: error instanceof ApiError ? error.message : "Could not reach the server.",
        encryption: null,
        kms_key_arn: null,
        bucket_key_enabled: null,
        checks: [],
      });
    } finally {
      setTesting(false);
    }
  }

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setNotice(null);
    try {
      await api.updateSettings({
        aws_access_key_id: keyId.trim(),
        // Empty means "leave the stored secret alone" — retyping a 40-character
        // secret to change the region would be its own kind of hostile.
        ...(secret.trim() ? { aws_secret_access_key: secret.trim() } : {}),
        offsite_bucket: bucket.trim(),
        offsite_region: region.trim(),
        offsite_kms_key_id: kms.trim(),
      });
      setSecret("");
      setNotice("Saved.");
      // A result from the previous configuration must not sit above the new
      // one. This panel exists to avoid claiming more than it has checked.
      setTest(null);
      onSaved();
    } catch (error) {
      // The server's message is the diagnosis here — it names the field and
      // says what was wrong with it, including the swapped-fields case.
      setNotice(error instanceof ApiError ? error.message : "Could not save.");
    } finally {
      setBusy(false);
    }
  }

  // `border-field`, not `border-edge`: these five credential boxes sit on a
  // `bg-surface` card, where `edge` measures 1.23:1 and the fill itself only
  // 1.32:1 — no channel reaches 3:1, so the inputs had no perceivable
  // boundary (WCAG 1.4.11). `field` is the token the rest of the app's
  // controls moved to; this local string was the one place that missed it.
  const field = "w-full rounded-md border border-field bg-ink px-3 py-2 font-mono text-sm outline-none focus:border-accent";

  return (
    <section className="mt-6 rounded-lg border border-edge bg-surface p-5">
      <h2 className="text-base font-medium">Offsite replication</h2>
      <p className="mt-1 max-w-2xl text-sm text-muted">
        A copy of the originals and the database, in S3, encrypted with a key you
        control. RAID survives a dead disk; it does not survive the building.
      </p>
      <p className="mt-2 max-w-2xl text-sm text-muted">
        The credentials are deliberately unable to delete anything. Rotation is
        done by S3 itself, so a compromised server can add to this copy but never
        erase it.
      </p>

      <div className="mt-4 flex items-center gap-2 text-sm">
        <span
          className={`h-2 w-2 rounded-full ${configured ? "bg-emerald-400" : "bg-muted"}`}
        />
        {configured ? (
          <span>
            Credentials saved{" "}
            <span className="font-mono text-muted">{settings.aws_secret_hint}</span>
          </span>
        ) : (
          <span className="text-muted">Not configured — nothing is leaving this machine</span>
        )}
      </div>

      {/* Deliberately not the word "connected" until something has actually
          talked to AWS. "Four fields are non-empty" is not a connection, and a
          green light that means less than it looks like is worse than none. */}
      {configured && !test && (
        <p className="mt-2 text-sm text-amber-400/90">
          Saved, but not tested. This says the fields are filled in — not that a
          backup would succeed. Run the test.
        </p>
      )}

      {test && (
        <div
          className={`mt-3 rounded-md border p-3 text-sm ${
            test.ok
              ? "border-emerald-500/40 bg-emerald-500/5"
              : "border-red-500/40 bg-red-500/5"
          }`}
        >
          <p className={test.ok ? "text-emerald-300" : "text-red-300"}>{test.detail}</p>
          {test.checks.length > 0 && (
            <ul className="mt-2 space-y-0.5 text-xs text-muted">
              {test.checks.map((check) => (
                <li key={check}>
                  {/* Everything listed is something that happened, so a failure
                      reads as "it got this far" rather than only "it stopped". */}
                  <span className="text-emerald-400">✓</span> {check}
                </li>
              ))}
            </ul>
          )}
          {test.kms_key_arn && (
            <p className="mt-2 break-all font-mono text-xs text-muted">{test.kms_key_arn}</p>
          )}
        </div>
      )}

      <form className="mt-4 grid gap-3 sm:grid-cols-2" onSubmit={save}>
        <label className="text-sm">
          <span className="mb-1 block text-muted">Access key ID</span>
          <input
            value={keyId}
            onChange={(event) => setKeyId(event.target.value)}
            placeholder="AKIA…"
            autoComplete="off"
            spellCheck={false}
            className={field}
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-muted">
            Secret access key{" "}
            {settings.aws_secret_configured && (
              <span className="text-xs">— leave blank to keep the stored one</span>
            )}
          </span>
          <input
            type="password"
            value={secret}
            onChange={(event) => setSecret(event.target.value)}
            placeholder={settings.aws_secret_configured ? "••••••••" : "40 characters"}
            autoComplete="off"
            spellCheck={false}
            className={field}
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-muted">Bucket</span>
          <input
            value={bucket}
            onChange={(event) => setBucket(event.target.value)}
            placeholder="bindery-offsite-…"
            autoComplete="off"
            spellCheck={false}
            className={field}
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-muted">Region</span>
          <input
            value={region}
            onChange={(event) => setRegion(event.target.value)}
            placeholder="us-east-1"
            autoComplete="off"
            spellCheck={false}
            className={field}
          />
        </label>
        <label className="text-sm sm:col-span-2">
          <span className="mb-1 block text-muted">
            KMS key ID{" "}
            <span className="text-xs">
              — write this down somewhere that is not this server. A restore needs it.
            </span>
          </span>
          <input
            value={kms}
            onChange={(event) => setKms(event.target.value)}
            placeholder="alias/bindery-offsite, or the key UUID"
            autoComplete="off"
            spellCheck={false}
            className={field}
          />
        </label>

        <div className="flex items-center gap-3 sm:col-span-2">
          <button
            type="submit"
            disabled={busy}
            className="rounded-md bg-accent px-3 py-2 text-sm font-medium text-ink disabled:opacity-40"
          >
            {busy ? "Saving…" : "Save"}
          </button>
          <button
            type="button"
            onClick={() => void runTest()}
            disabled={testing || !configured}
            title={configured ? undefined : "Fill in and save the fields first"}
            className="rounded-md border border-edge px-3 py-2 text-sm disabled:opacity-40"
          >
            {testing ? "Testing…" : "Test connection"}
          </button>
          {notice && <span className="text-sm text-muted">{notice}</span>}
        </div>
      </form>
    </section>
  );
}

function ApiTokens() {
  const [tokens, setTokens] = useState<ApiTokenRecord[]>([]);
  const [issued, setIssued] = useState<IssuedApiToken | null>(null);
  const [name, setName] = useState("");
  const [scopes, setScopes] = useState<string[]>(["read"]);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () => api.apiTokens().then(setTokens).catch(() => {}),
    [],
  );
  useEffect(() => {
    void load();
  }, [load]);

  async function create() {
    setError(null);
    try {
      setIssued(await api.createApiToken({ name: name.trim(), scopes }));
      setName("");
      await load();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : String(caught));
    }
  }

  return (
    <section className="mt-6 rounded-lg border border-edge bg-surface p-5">
      <h2 className="text-base font-medium">API tokens</h2>
      <p className="mt-1 text-sm text-muted">
        For a script or a scanner that needs to reach Bindery without a browser
        login. A token can never do more than you can, and its libraries are
        re-checked against your memberships on every request — so losing access to
        a library immediately shrinks every token you made.
      </p>

      {error && (
        <p role="alert" className="mt-3 rounded border border-red-900 bg-red-950/40 p-2 text-sm text-red-300">
          {error}
        </p>
      )}

      {issued && (
        <div className="mt-4 rounded border border-accent/60 bg-ink p-3">
          <p className="text-sm font-medium">Copy this now — it is not shown again.</p>
          <p className="mt-1 text-xs text-muted">
            Only a hash is stored, so nobody, including this screen, can recover it.
          </p>
          <code className="mt-2 block break-all rounded bg-black/50 p-2 font-mono text-xs">
            {issued.secret}
          </code>
          <button
            type="button"
            onClick={() => setIssued(null)}
            className="mt-2 rounded border border-edge px-2 py-1 text-xs"
          >
            I have copied it
          </button>
        </div>
      )}

      <form
        className="mt-4 flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          if (name.trim()) void create();
        }}
      >
        <label className="flex flex-col gap-1">
          <span className="text-xs text-muted">Name</span>
          <input
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Brother scanner"
            className="w-56 rounded border border-field bg-ink px-2 py-1.5 text-sm"
          />
        </label>
        <fieldset className="flex flex-wrap gap-3">
          <legend className="sr-only">Scopes</legend>
          {Object.entries(SCOPE_LABELS).map(([scope, label]) => (
            <label key={scope} className="flex items-center gap-1.5 text-sm">
              <input
                type="checkbox"
                checked={scopes.includes(scope)}
                onChange={(event) =>
                  setScopes((current) =>
                    event.target.checked
                      ? [...current, scope]
                      : current.filter((s) => s !== scope),
                  )
                }
              />
              {label}
            </label>
          ))}
        </fieldset>
        <button
          type="submit"
          disabled={!name.trim() || scopes.length === 0}
          className="rounded bg-accent px-3 py-1.5 text-sm font-medium text-ink disabled:opacity-40"
        >
          Create
        </button>
      </form>

      {tokens.length > 0 && (
        <ul className="mt-4 divide-y divide-edge border-t border-edge">
          {tokens.map((token) => (
            <li key={token.id} className="flex flex-wrap items-center gap-2 py-2 text-sm">
              <span className={token.revoked_at ? "text-muted line-through" : "font-medium"}>
                {token.name}
              </span>
              <span className="font-mono text-xs text-muted">{token.prefix}…</span>
              <span className="text-xs text-muted">{token.scopes.join(", ")}</span>
              <span className="flex-1" />
              {token.revoked_at ? (
                <span className="text-xs text-muted">revoked</span>
              ) : (
                <button
                  type="button"
                  onClick={() => void api.revokeApiToken(token.id).then(load)}
                  className="rounded border border-edge px-2 py-1 text-xs"
                >
                  Revoke
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
      <p className="mt-3 text-xs text-muted">
        Revoked tokens stay listed. What existed and what it could reach is a
        question worth being able to answer after an incident.
      </p>
    </section>
  );
}
