import { useCallback, useEffect, useState } from "react";

import { ApiError, api, type ApiTokenRecord, type IssuedApiToken, type Settings, type SettingsTest } from "../../api";

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
    return <div className="mx-auto max-w-3xl px-6 py-16 text-center text-muted">Loading…</div>;
  }

  return (
    <div className="mx-auto max-w-3xl px-6 py-8">
      <h1 className="text-xl font-semibold tracking-tight">Settings</h1>
      <p className="mt-1 mb-6 text-sm text-muted">Configuration you can change without a restart.</p>

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
          <input
            type="password"
            value={key}
            onChange={(event) => setKey(event.target.value)}
            placeholder="sk-ant-…"
            autoComplete="off"
            spellCheck={false}
            className="min-w-0 flex-1 rounded-md border border-edge bg-ink px-3 py-2 font-mono text-sm outline-none focus:border-accent"
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

      <section className="mt-6 rounded-lg border border-edge bg-surface p-5">
        <h2 className="text-base font-medium">Model</h2>
        <dl className="mt-3 grid grid-cols-[10rem_1fr] gap-y-2 text-sm">
          <dt className="text-muted">Model</dt>
          <dd className="font-mono">{settings.model}</dd>
          <dt className="text-muted">Prompt version</dt>
          <dd className="font-mono">{settings.prompt_version}</dd>
        </dl>
        <p className="mt-3 text-xs text-muted">
          Every classification records the model and prompt version that produced it,
          so changing either leaves earlier documents traceable to what actually
          classified them.
        </p>
      </section>

      <NotificationSettings settings={settings} onSaved={load} />
      <ApiTokens />
    </div>
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
          className="w-96 max-w-full rounded border border-edge bg-ink px-2 py-1.5 text-sm"
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
            className="w-56 rounded border border-edge bg-ink px-2 py-1.5 text-sm"
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
