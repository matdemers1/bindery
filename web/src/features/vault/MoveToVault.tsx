import { useState } from "react";
import { useNavigate } from "react-router";
import { ShieldCheck } from "lucide-react";

import { ApiError, api, type VaultState } from "../../api";
import Modal from "../../components/Modal";
import UnlockForm from "./UnlockForm";

/**
 * "Move to vault", from wherever you are looking at the thing.
 *
 * The move is the one irreversible-feeling action in the archive — the
 * plaintext really is deleted — so it asks first, and says plainly what will
 * stop working afterwards. It is not actually irreversible: "Take out" on the
 * vault screen decrypts it back. Saying so is what stops the confirmation from
 * being scarier than the truth.
 *
 * If the vault is shut, unlocking happens here rather than by sending someone
 * to another screen and losing what they were doing.
 */
export default function MoveToVault({
  documentId,
  title,
  onMoved,
  className,
}: {
  documentId: string;
  title?: string | null;
  onMoved?: () => void;
  className?: string;
}) {
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<VaultState | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const heading = `Move ${title ? `“${title}”` : "this document"} to the vault?`;

  async function begin() {
    setError(null);
    setState(await api.vault());
    setOpen(true);
  }

  async function confirm() {
    setBusy(true);
    setError(null);
    try {
      await api.vaultMoveIn(documentId);
      setOpen(false);
      onMoved?.();
      navigate("/vault");
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.status === 423
          ? "The vault locked while this was open. Unlock it and try again."
          : caught instanceof Error
            ? caught.message
            : String(caught),
      );
      if (caught instanceof ApiError && caught.status === 423) {
        setState(await api.vault());
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <>
      <button
        type="button"
        onClick={() => void begin()}
        title="Encrypt this behind the vault passphrase and take it out of search"
        className={
          className ??
          "flex items-center gap-1.5 rounded-md border border-edge px-3 py-1.5 text-sm text-muted hover:border-accent/60"
        }
      >
        <ShieldCheck size={14} />
        Move to vault
      </button>

      {open && state && (
        <Modal
          label={heading}
          onClose={() => setOpen(false)}
          className="w-full max-w-md space-y-3 rounded-xl border border-edge bg-surface p-5"
        >
          <h2 className="text-sm font-medium">{heading}</h2>

          {!state.exists ? (
            <>
              <p className="text-sm text-muted">
                There is no vault on this account yet. Setting one up takes a
                passphrase and a PIN.
              </p>
              <button
                type="button"
                onClick={() => navigate("/vault")}
                className="rounded-lg bg-accent px-3 py-2 text-sm font-medium text-ink"
              >
                Set up the vault
              </button>
            </>
          ) : !state.unlocked ? (
            <>
              <p className="text-sm text-muted">
                The vault is locked. Open it to move something in.
              </p>
              <UnlockForm state={state} onUnlocked={setState} compact />
            </>
          ) : (
            <>
              <ul className="space-y-1 text-sm text-muted">
                <li>· The original is encrypted and the plaintext copy deleted.</li>
                <li>· It leaves search, Ask, the archive, and every count.</li>
                <li>· Its tags, type and correspondent are taken back.</li>
                <li>
                  · It stays in backups and offsite copies, as ciphertext.
                </li>
              </ul>
              <p className="rounded-lg border border-edge bg-ink/40 px-3 py-2 text-xs text-muted">
                This can be undone. “Take out” on the vault screen decrypts it
                back into the archive, with its title and page text.
              </p>
              {error && (
                <p
                  role="alert"
                  className="rounded-lg border border-red-900/60 bg-red-950/20 px-3 py-2 text-sm text-red-300"
                >
                  {error}
                </p>
              )}
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  onClick={() => void confirm()}
                  disabled={busy}
                  className="rounded-lg bg-accent px-3 py-2 text-sm font-medium text-ink disabled:opacity-40"
                >
                  {busy ? "Moving…" : "Move to vault"}
                </button>
                <button
                  type="button"
                  onClick={() => setOpen(false)}
                  className="rounded-lg border border-edge px-3 py-2 text-sm text-muted hover:text-neutral-100"
                >
                  Cancel
                </button>
              </div>
            </>
          )}
        </Modal>
      )}
    </>
  );
}
