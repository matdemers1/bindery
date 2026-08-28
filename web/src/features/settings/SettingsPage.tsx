import { useCallback, useEffect, useState } from "react";

import { ApiError, api, type Settings, type SettingsTest } from "../../api";

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
    </div>
  );
}
