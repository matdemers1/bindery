import { useState } from "react";
import { Button, Input } from "@d3cloud/ui";

import { ApiError, api, type Settings } from "../../api";

/**
 * Where an operator configures *Sign in with D3 Auth* (Phase 20, REQ-203).
 *
 * The other half of this feature — `ConnectD3Auth` — links one person's account to an identity
 * at a provider that is already configured. This is the step before it, and without it the
 * whole phase was reachable only by editing a compose file over SSH, which is the answer this
 * screen exists to replace.
 *
 * Administrator-only, and the server says so too: an account that could point this at a
 * provider it controls could grant itself `admin` there and arrive here holding it. The card is
 * simply absent for everybody else rather than disabled — there is nothing here for them to do.
 *
 * The mode is the last field on purpose. Reading downwards it is: here is the provider, here is
 * who we are to it, here is the secret, and *now* how much of the front door it owns.
 */
const MODES: { value: string; label: string; blurb: string }[] = [
  {
    value: "off",
    label: "Off",
    blurb: "No button, and no mention of a provider on the sign-in screen.",
  },
  {
    value: "optional",
    label: "Optional",
    blurb: "Both ways in. Passwords keep working, and nothing changes for an account that never uses this.",
  },
  {
    value: "required",
    label: "Required",
    blurb: "The provider is the way in. The owner's own sign-in stays reachable at /login/local, which is what you use if the provider is ever down.",
  },
];

export default function D3AuthProvider({
  settings,
  onSaved,
}: {
  settings: Settings;
  onSaved: () => void;
}) {
  const [issuer, setIssuer] = useState(settings.oidc_issuer ?? "");
  const [clientId, setClientId] = useState(settings.oidc_client_id ?? "");
  const [secret, setSecret] = useState("");
  const [mode, setMode] = useState(settings.sso_mode || "off");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);

  const configured = !!settings.oidc_issuer && !!settings.oidc_client_id
    && settings.oidc_secret_configured;
  const host = settings.oidc_issuer ? safeHost(settings.oidc_issuer) : null;

  async function save(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setNotice(null);
    try {
      await api.updateSettings({
        oidc_issuer: issuer.trim(),
        oidc_client_id: clientId.trim(),
        // Empty means "keep the stored one", as it does for every other secret on this screen:
        // retyping a client secret to change the mode would be its own kind of hostile.
        ...(secret.trim() ? { oidc_client_secret: secret.trim() } : {}),
        sso_mode: mode,
      });
      setSecret("");
      setNotice("Saved.");
      onSaved();
    } catch (error) {
      // The server's message is the diagnosis — it names what is missing and refuses to turn
      // SSO on without it, because a button that cannot work is worse than no button.
      setNotice(error instanceof ApiError ? error.message : "Could not save.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="mt-6 rounded-lg border border-edge bg-surface p-5">
      <h2 className="text-base font-medium">Sign in with D3 Auth</h2>
      <p className="mt-1 max-w-2xl text-sm text-muted">
        An optional second way in, through an OpenID Connect provider you run. Register Bindery
        there first; the three values below are on the connection sheet it shows you once.
      </p>
      <p className="mt-2 max-w-2xl text-sm text-muted">
        The redirect URI to register, character for character:{" "}
        <span className="break-all font-mono text-fg">
          {window.location.origin}/api/auth/oidc/callback
        </span>
      </p>

      <div className="mt-4 flex items-center gap-2 text-sm">
        <span className={`h-2 w-2 rounded-full ${configured ? "bg-success" : "bg-muted"}`} />
        {configured ? (
          <span>
            Configured against <span className="font-mono">{host}</span>{" "}
            <span className="font-mono text-muted">{settings.oidc_secret_hint}</span>
          </span>
        ) : (
          <span className="text-muted">Not configured — the sign-in screen says nothing</span>
        )}
      </div>

      {/* "Saved" is four non-empty fields, which is not the same as a sign-in that worked. */}
      {configured && settings.sso_mode !== "off" && (
        <p className="mt-2 max-w-2xl text-sm text-warning/90">
          Saved and turned on. That the fields are filled in is not proof a sign-in succeeds —
          connect your own account below and try it before anyone else depends on it.
        </p>
      )}

      <form className="mt-4 grid gap-3 sm:grid-cols-2" onSubmit={save}>
        <label className="text-sm sm:col-span-2">
          <span className="mb-1 block text-muted">Issuer</span>
          <Input
            value={issuer}
            onChange={(event) => setIssuer(event.target.value)}
            placeholder="https://auth.example.com"
            autoComplete="off"
            spellCheck={false}
            className="font-mono"
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-muted">Client ID</span>
          <Input
            value={clientId}
            onChange={(event) => setClientId(event.target.value)}
            placeholder="bindery"
            autoComplete="off"
            spellCheck={false}
            className="font-mono"
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-muted">
            Client secret{" "}
            {settings.oidc_secret_configured && (
              <span className="text-xs">— leave blank to keep the stored one</span>
            )}
          </span>
          <Input
            type="password"
            value={secret}
            onChange={(event) => setSecret(event.target.value)}
            placeholder={settings.oidc_secret_configured ? "••••••••" : "shown once, at the provider"}
            autoComplete="off"
            data-1p-ignore
            spellCheck={false}
            className="font-mono"
          />
        </label>

        <fieldset className="sm:col-span-2">
          <legend className="mb-2 text-sm text-muted">How much of the front door it owns</legend>
          <div className="grid gap-2">
            {MODES.map((choice) => (
              <label
                key={choice.value}
                className={`flex gap-3 rounded-md border p-3 text-sm ${
                  mode === choice.value ? "border-accent bg-accent/5" : "border-field"
                }`}
              >
                <input
                  type="radio"
                  name="sso_mode"
                  value={choice.value}
                  checked={mode === choice.value}
                  onChange={() => setMode(choice.value)}
                  className="mt-1 flex-none"
                />
                <span>
                  <span className="font-medium">{choice.label}</span>
                  <span className="block text-muted">{choice.blurb}</span>
                </span>
              </label>
            ))}
          </div>
        </fieldset>

        <div className="flex items-center gap-3 sm:col-span-2">
          <Button variant="primary" type="submit" disabled={busy}>
            {busy ? "Saving…" : "Save"}
          </Button>
          {notice && <span className="text-sm text-muted">{notice}</span>}
        </div>
      </form>
    </section>
  );
}

/** A stored issuer is whatever was typed, and `new URL` throws on the rest. */
function safeHost(issuer: string): string {
  try {
    return new URL(issuer).host;
  } catch {
    return issuer;
  }
}
