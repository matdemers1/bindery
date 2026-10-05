import { useCallback, useEffect, useState } from "react";
import { Alert, Button } from "@d3cloud/ui";

import { sessionsApi, type SignedInSession } from "../../api";

const platformNames: Record<string, string> = { ios: "iPhone", ipados: "iPad", macos: "Mac" };

function describe(session: SignedInSession): string {
  if (!session.device_name) return "A browser";
  const platform = session.device_platform ? platformNames[session.device_platform] : undefined;
  return platform && !session.device_name.includes(platform)
    ? `${session.device_name} · ${platform}`
    : session.device_name;
}

/**
 * *Signed-in devices* (BND-T-22.4).
 *
 * Every session this person holds — browsers, and the phones and Macs D3 Constellation signed in,
 * which name themselves — and a way to end any of them. "Sign out" here is the answer to a phone
 * left on a train: the app's next request finds its session ended and asks to sign in again.
 */
export default function SignedInDevices() {
  const [sessions, setSessions] = useState<SignedInSession[] | null>(null);
  const [ending, setEnding] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    () => sessionsApi.list().then(setSessions).catch(() => setSessions(null)),
    [],
  );
  useEffect(() => {
    void load();
  }, [load]);

  if (!sessions) return null;

  async function end(session: SignedInSession) {
    setEnding(session.id);
    setError(null);
    try {
      await sessionsApi.end(session.id);
      await load();
    } catch {
      setError(`Could not sign ${describe(session)} out.`);
    } finally {
      setEnding(null);
    }
  }

  return (
    <section className="mt-6 rounded-lg border border-edge bg-surface p-5">
      <h2 className="font-medium">Signed-in devices</h2>
      <p className="mt-1 text-sm text-muted">
        Where this account is signed in. Signing one out ends it there at once.
      </p>
      {error ? (
        <Alert tone="danger" dynamic className="mt-3">
          {error}
        </Alert>
      ) : null}
      <ul className="mt-3 divide-y divide-edge">
        {sessions.map((session) => (
          <li key={session.id} className="flex items-center justify-between gap-3 py-2">
            <div className="min-w-0">
              <p className="truncate text-sm text-fg">{describe(session)}</p>
              <p className="text-xs text-muted">
                {session.current
                  ? "This device"
                  : `Last active ${new Date(session.last_active).toLocaleString()}`}
              </p>
            </div>
            {session.current ? null : (
              <Button size="sm" loading={ending === session.id} onClick={() => void end(session)}>
                Sign out
              </Button>
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
