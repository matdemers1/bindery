import { useCallback, useEffect, useState } from "react";
import { Alert, Button, FormField, PasswordInput } from "@d3cloud/ui";

import { ApiError, oidcApi, oidcPaths, type OidcLink } from "../../api";

/**
 * *Connect D3 Auth* (Phase 20, REQ-206).
 *
 * Linking is deliberately an act by somebody already signed in here, and unlinking is behind
 * the local password. Both directions matter: without the password, a stolen session could
 * quietly detach an account from the thing that revokes its access — or, on an account that
 * arrived through the provider in the first place, lock it out of itself.
 *
 * The card is absent entirely when no provider is configured. There is nothing useful to say to
 * somebody whose archive has no second way in.
 */
export default function ConnectD3Auth() {
  const [state, setState] = useState<OidcLink | null>(null);
  const [password, setPassword] = useState("");
  const [disconnecting, setDisconnecting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // A link finishes as a redirect back to this page, so the notice is part of the first render
  // rather than something an effect sets afterwards.
  const [notice, setNotice] = useState<string | null>(() =>
    new URLSearchParams(window.location.search).get("linked") === "1"
      ? "Connected. You can now sign in with D3 Auth."
      : null,
  );

  const load = useCallback(() => oidcApi.link().then(setState).catch(() => setState(null)), []);
  useEffect(() => {
    void load();
  }, [load]);

  if (!state || state.mode === "off") return null;

  async function disconnect(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await oidcApi.disconnect(password);
      setPassword("");
      setDisconnecting(false);
      setNotice("Disconnected. Your password still signs you in.");
      await load();
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.status === 403
          ? "That password is not right."
          : "Could not disconnect.",
      );
    } finally {
      setBusy(false);
    }
  }

  const host = state.issuer ? new URL(state.issuer).host : "D3 Auth";

  return (
    <section className="mt-6 rounded-lg border border-edge bg-surface p-5">
      <h2 className="font-medium">Sign in with D3 Auth</h2>
      <p className="mt-1 text-sm text-muted">
        {state.linked ? (
          <>
            This account is connected to <span className="text-fg">{host}</span>
            {state.preferred_username ? (
              <>
                {" "}as <span className="text-fg">{state.preferred_username}</span>
              </>
            ) : null}
            . Both ways in work: your password still signs you in.
          </>
        ) : (
          <>
            Connect this account to <span className="text-fg">{host}</span> and you can sign in
            either way. Your password keeps working, and nothing here moves.
          </>
        )}
      </p>

      {notice ? (
        <Alert tone="success" dynamic className="mt-3">
          {notice}
        </Alert>
      ) : null}

      {state.linked ? (
        disconnecting ? (
          <form onSubmit={disconnect} className="mt-4 grid max-w-sm gap-3">
            <FormField
              label="Your Bindery password"
              error={error ?? undefined}
              help="Asked for because disconnecting changes how you get in."
            >
              <PasswordInput
                autoComplete="current-password"
                required
                value={password}
                onChange={(event) => setPassword(event.target.value)}
              />
            </FormField>
            <div className="flex gap-2">
              <Button type="submit" variant="danger" loading={busy}>
                Disconnect
              </Button>
              <Button type="button" onClick={() => { setDisconnecting(false); setError(null); }}>
                Keep it
              </Button>
            </div>
          </form>
        ) : (
          <Button className="mt-4" onClick={() => setDisconnecting(true)}>
            Disconnect
          </Button>
        )
      ) : (
        <div className="mt-4">
          {/* A whole-document navigation: the provider answers with a redirect. */}
          <a className="entry-sso max-w-xs" href={oidcPaths.connect}>
            Connect D3 Auth
          </a>
        </div>
      )}
    </section>
  );
}
