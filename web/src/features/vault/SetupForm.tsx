import { useState } from "react";
import { ShieldPlus } from "lucide-react";

import { api, type VaultState } from "../../api";
import { Alert, Button, Checkbox, FormField, Input, PasswordInput } from "@d3cloud/ui";
import { judgePassword } from "../entry/strength";

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
    <form onSubmit={submit} className="grid max-w-xl gap-5">
      <Alert tone="warning" title="This passphrase cannot be recovered.">
        It is not stored anywhere — only a key wrapped with it is. If you forget
        it and the PIN has been switched off, everything in the vault stays
        encrypted for good. Write it down somewhere physical before you go on.
      </Alert>

      {/* Typed twice here, unlike an account password: nobody can reset this
          one, so a typo on creation is a vault nobody can open. */}
      <FormField
        label="Vault passphrase"
        help="At least 12 characters, and different from your login."
      >
        <PasswordInput
          size="lg"
          autoComplete="new-password"
          value={passphrase}
          onChange={(event) => setPassphrase(event.target.value)}
          strength={judgePassword(passphrase)}
        />
      </FormField>

      <FormField label="Type it again" error={mismatch ? "These do not match." : undefined}>
        <PasswordInput
          size="lg"
          autoComplete="new-password"
          value={again}
          onChange={(event) => setAgain(event.target.value)}
        />
      </FormField>

      <FormField
        label="PIN"
        help="For day to day; 4 digits or more. Five wrong entries switch the PIN off. Nothing is deleted — the passphrase still opens the vault and you can set a new PIN."
      >
        <Input
          size="lg"
          type="password"
          inputMode="numeric"
          autoComplete="off"
          value={pin}
          onChange={(event) => setPin(event.target.value.replace(/\D/g, ""))}
          /* d3-allow: a PIN field, spaced so the digits can be counted. Not a type choice. */
          className="max-w-48 font-mono tracking-[0.4em]"
        />
      </FormField>

      <Checkbox
        checked={understood}
        onCheckedChange={(value) => setUnderstood(value === true)}
        label="I have written the passphrase down somewhere I will still have it in ten years."
      />

      {error && (
        <Alert tone="danger" dynamic>
          {error}
        </Alert>
      )}

      <Button
        variant="primary"
        type="submit"
        icon={<ShieldPlus size={14} />}
        loading={busy}
        disabled={!ready}
      >
        {busy ? "Creating…" : "Create the vault"}
      </Button>
    </form>
  );
}
