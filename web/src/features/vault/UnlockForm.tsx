import { useId, useState } from "react";
import { KeyRound, Lock } from "lucide-react";

import { ApiError, api, type VaultState } from "../../api";
import { Alert, Button, FormField, Input, PasswordInput } from "@d3cloud/ui";

/**
 * Opening the vault, by PIN or by passphrase.
 *
 * Used on the vault screen and again inside search, because "let me look in the
 * vault too" is the same question asked from somewhere else. It reports how
 * many attempts are left rather than saving that for the moment the PIN stops
 * working: a wrapper that vanishes without warning reads as lost data even
 * though the passphrase still opens everything.
 */
export default function UnlockForm({
  state,
  onUnlocked,
  compact = false,
}: {
  state: VaultState;
  onUnlocked: (next: VaultState) => void;
  compact?: boolean;
}) {
  const [pin, setPin] = useState("");
  const [passphrase, setPassphrase] = useState("");
  // The PIN is the everyday way in; the passphrase is the way back when the
  // PIN has gone. Showing both at once makes the quick path look complicated.
  const [usePassphrase, setUsePassphrase] = useState(!state.pin_enabled);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const remaining = Math.max(0, 5 - state.pin_failures);

  // The field has to point at whatever is being said about it. A wrong PIN
  // used to produce a paragraph nobody was told about, so the first thing a
  // screen-reader user learned was that the PIN had been switched off.
  const errorId = useId();
  const attemptsId = useId();
  const describedBy =
    [state.pin_failures > 0 ? attemptsId : null, error ? errorId : null]
      .filter(Boolean)
      .join(" ") || undefined;

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const next = await api.vaultUnlock(
        usePassphrase ? { passphrase } : { pin },
      );
      setPin("");
      setPassphrase("");
      onUnlocked(next);
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.status === 401
          ? usePassphrase
            ? "That passphrase does not open this vault."
            : remaining - 1 > 0
              // The count lives under the input, in one place. Repeating it
              // here made the same sentence appear twice on one screen.
              ? "That PIN is wrong."
              : "That was the last attempt, so the PIN is now switched off. The passphrase still opens the vault and nothing in it is lost."
          : caught instanceof Error
            ? caught.message
            : String(caught),
      );
      // The counter moved, so the caller's copy of the state is stale.
      onUnlocked(await api.vault());
    } finally {
      setBusy(false);
    }
  }

  return (
    <form onSubmit={submit} className={compact ? "space-y-2" : "space-y-3"}>
      {usePassphrase ? (
        <FormField label="Vault passphrase">
          <PasswordInput
            autoComplete="off"
            invalid={Boolean(error)}
            aria-describedby={error ? errorId : undefined}
            value={passphrase}
            onChange={(event) => setPassphrase(event.target.value)}
          />
        </FormField>
      ) : (
        <div>
          <FormField label="Vault PIN">
            <Input
              type="password"
              inputMode="numeric"
              autoComplete="off"
              invalid={Boolean(error)}
              aria-describedby={describedBy}
              value={pin}
              onChange={(event) => setPin(event.target.value.replace(/\D/g, ""))}
              /* d3-allow: a PIN field, spaced so the digits can be counted. Not a type choice. */
              className="font-mono tracking-[0.4em]"
            />
          </FormField>
          {state.pin_failures > 0 && (
            <p id={attemptsId} role="status" className="mt-1 text-xs text-warning">
              {remaining} attempt{remaining === 1 ? "" : "s"} left. After that the
              PIN is switched off and the passphrase is the way in — nothing in
              the vault is lost.
            </p>
          )}
        </div>
      )}

      {error && (
        <Alert tone="danger" dynamic id={errorId}>
          {error}
        </Alert>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <Button
          variant="primary"
          type="submit"
          icon={<KeyRound size={14} />}
          loading={busy}
          disabled={usePassphrase ? !passphrase : pin.length < 4}
        >
          {busy ? "Opening…" : "Unlock"}
        </Button>
        {state.pin_enabled && (
          <button
            type="button"
            onClick={() => {
              setUsePassphrase((value) => !value);
              setError(null);
            }}
            className="text-xs text-muted underline underline-offset-2 hover:text-fg"
          >
            {usePassphrase ? "Use the PIN instead" : "Use the passphrase instead"}
          </button>
        )}
        {!state.pin_enabled && (
          <span className="flex items-center gap-1 text-xs text-muted">
            <Lock size={12} /> The PIN is switched off for this vault.
          </span>
        )}
      </div>
    </form>
  );
}
