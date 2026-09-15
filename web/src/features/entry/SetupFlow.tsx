import { useEffect, useRef, useState } from "react";
import {
  Alert,
  Button,
  Checkbox,
  CodeInput,
  FormField,
  Input,
  PasswordInput,
  type CodeInputStatus,
} from "@d3cloud/ui";

import { ApiError, accountsApi, setupApi } from "../../api";
import { EntryHeading, EntryShell } from "./EntryShell";
import { retryMessage } from "./messages";
import { judgePassword } from "./strength";

const PRINT_COMMAND = "docker compose logs api | grep 'bindery setup'";

/**
 * Claiming a fresh install in the browser (Phase 19, REQ-200, REQ-201).
 *
 * Three steps, because administrator rights require two-factor (REQ-156) and
 * setup goes through that rule rather than around it:
 *
 * 1. **Claim** — the setup code printed in the api's log, and the owner's
 *    account. Only someone who can read the host's container logs has the
 *    code, which is what stops whoever finds the URL first owning the archive.
 * 2. **Secure** — enrol an authenticator.
 * 3. **Recovery codes** — shown once; then the account becomes administrator.
 *
 * Closing the tab after step 1 is fine: signing in again lands on step 2,
 * because the server records who claimed the archive until setup finishes.
 */
export default function SetupFlow({
  stage,
  onChanged,
}: {
  /** Where the server says setup stands. */
  stage: "claim" | "secure";
  /** Re-read the session and setup state — after claiming, and after finishing. */
  onChanged: () => Promise<void>;
}) {
  return stage === "claim" ? <Claim onClaimed={onChanged} /> : <Secure onDone={onChanged} />;
}

function Steps({ at }: { at: 1 | 2 | 3 }) {
  return (
    <div className="mb-5">
      <div className="flex gap-1.5" aria-hidden="true">
        {[1, 2, 3].map((n) => (
          <span key={n} className="entry-step" data-on={n <= at || undefined} />
        ))}
      </div>
      <p className="sr-only">Step {at} of 3</p>
    </div>
  );
}

// ---------------------------------------------------------------------------
// 1 · Claim
// ---------------------------------------------------------------------------

function Claim({ onClaimed }: { onClaimed: () => Promise<void> }) {
  const [code, setCode] = useState("");
  const [codeStatus, setCodeStatus] = useState<CodeInputStatus>("idle");
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [library, setLibrary] = useState("Personal");
  const [errors, setErrors] = useState<{ code?: string; password?: string; form?: string }>({});
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    if (code.length < 12) {
      setErrors({ code: "The setup code is twelve characters." });
      setCodeStatus("error");
      return;
    }
    setBusy(true);
    setErrors({});
    try {
      await setupApi.claim({
        code,
        email,
        password,
        display_name: name.trim() || null,
        library_name: library,
      });
      await onClaimed();
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 400) {
        setErrors({
          code: "That setup code is not valid. It changes each time the api starts — print the current one.",
        });
        setCodeStatus("error");
      } else if (caught instanceof ApiError && caught.status === 422) {
        setErrors({ password: caught.message });
      } else if (caught instanceof ApiError && caught.status === 409) {
        // Somebody finished first, or this tab is stale. The page to show is
        // sign-in, and only a fresh load decides that correctly.
        window.location.assign("/");
      } else if (caught instanceof ApiError && caught.status === 429) {
        setErrors({ form: retryMessage(caught.retryAfter) });
      } else {
        setErrors({ form: "Could not reach the server." });
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <EntryShell wide>
      <Steps at={1} />
      <EntryHeading eyebrow="First run" title="Set up this archive">
        There are no accounts yet. The one you make now owns the archive: it
        invites everyone else, and it is the only account that can issue reset
        codes.
      </EntryHeading>

      <form onSubmit={submit} className="grid gap-5" noValidate>
        <FormField
          label="Setup code"
          error={errors.code}
          help={
            <>
              Printed in the api container's log when it starts. It proves you are
              the person running this server.
              <span className="mt-2 flex items-center gap-2">
                <code className="min-w-0 flex-1 truncate rounded-md border border-border bg-bg-sunken px-2.5 py-1.5 font-mono text-xs text-fg">
                  {PRINT_COMMAND}
                </code>
                <button
                  type="button"
                  className="entry-link flex-none text-xs"
                  onClick={() => {
                    void navigator.clipboard?.writeText(PRINT_COMMAND);
                    setCopied(true);
                    window.setTimeout(() => setCopied(false), 1600);
                  }}
                >
                  {copied ? "Copied" : "Copy"}
                </button>
              </span>
            </>
          }
        >
          <CodeInput
            mode="alphanumeric"
            length={12}
            groups={[4, 4, 4]}
            autoComplete="off"
            autoFocus
            value={code}
            status={codeStatus}
            onValueChange={(next) => {
              setCode(next);
              if (codeStatus !== "idle") setCodeStatus("idle");
            }}
          />
        </FormField>

        <div className="grid gap-5 sm:grid-cols-2">
          <FormField label="Your name" optional>
            <Input size="lg" autoComplete="name" value={name} onChange={(e) => setName(e.target.value)} />
          </FormField>
          <FormField label="First library" help="Yours alone. Shared ones come later.">
            <Input size="lg" required value={library} onChange={(e) => setLibrary(e.target.value)} />
          </FormField>
        </div>

        <FormField label="Email">
          <Input
            size="lg"
            type="email"
            required
            autoComplete="username"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </FormField>

        <FormField
          label="Password"
          error={errors.password}
          help="At least 12 characters. Four unrelated words beat one clever word."
        >
          <PasswordInput
            size="lg"
            required
            autoComplete="new-password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            strength={judgePassword(password, email)}
          />
        </FormField>

        {errors.form ? (
          <Alert tone="danger" dynamic>
            {errors.form}
          </Alert>
        ) : null}

        <Button variant="primary" size="lg" type="submit" className="w-full" loading={busy}>
          Create the owner account
        </Button>
      </form>
    </EntryShell>
  );
}

// ---------------------------------------------------------------------------
// 2 · Secure, and 3 · Recovery codes
// ---------------------------------------------------------------------------

type Enrolment = { secret: string; uri: string; qr_svg: string };

function Secure({ onDone }: { onDone: () => Promise<void> }) {
  const [enrolment, setEnrolment] = useState<Enrolment | null>(null);
  const [alreadyOn, setAlreadyOn] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);
  const [code, setCode] = useState("");
  const [status, setStatus] = useState<CodeInputStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const [recovery, setRecovery] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const started = useRef(false);

  useEffect(() => {
    // Once: starting an enrolment issues a new secret, and a second one would
    // silently invalidate the QR code already on screen.
    if (started.current) return;
    started.current = true;
    accountsApi
      .totpStart()
      .then(setEnrolment)
      .catch((caught) => {
        if (caught instanceof ApiError && caught.status === 409) setAlreadyOn(true);
        else setProblem("Could not start two-factor setup. Reload to try again.");
      });
  }, []);

  async function confirm(full: string) {
    if (busy) return;
    setBusy(true);
    setError(null);
    try {
      const codes = await accountsApi.totpConfirm(full);
      setStatus("success");
      // Let the wave finish before the page changes under it.
      window.setTimeout(() => setRecovery(codes), 520);
    } catch (caught) {
      setError(
        caught instanceof ApiError && caught.status === 422
          ? "That code was not accepted. Check the time on your phone, then try the code showing now."
          : "Could not reach the server.",
      );
      setStatus("error");
      window.setTimeout(() => setCode(""), 460);
    } finally {
      setBusy(false);
    }
  }

  if (recovery || alreadyOn) {
    return <Finish codes={recovery} onDone={onDone} />;
  }

  return (
    <EntryShell wide>
      <Steps at={2} />
      <EntryHeading eyebrow="First run" title="Secure the owner account">
        This account can reset every other password, so it needs a second
        factor. Scan the code with an authenticator app — 1Password, Google
        Authenticator, Authy, or your phone's own.
      </EntryHeading>

      {problem ? (
        <Alert tone="danger">{problem}</Alert>
      ) : (
        <div className="grid gap-6">
          <div className="flex flex-col items-center gap-5 sm:flex-row sm:items-start">
            <div
              className="entry-qr"
              role="img"
              aria-label="QR code for your authenticator app. The same key is written out beside it."
              // Drawn by the api from the provisioning URI (api/auth/totp.py);
              // markup Bindery itself produced, never user input.
              dangerouslySetInnerHTML={enrolment ? { __html: enrolment.qr_svg } : undefined}
              data-loading={enrolment ? undefined : true}
            />
            <div className="min-w-0 flex-1 text-sm leading-relaxed text-fg-muted">
              <p>Can't scan? Type this key into the app instead:</p>
              <p className="mt-2 font-mono text-13 tracking-wider break-words text-fg">
                {enrolment ? enrolment.secret.replace(/(.{4})/g, "$1 ").trim() : "…"}
              </p>
              <p className="mt-2">Then enter the six-digit code it shows.</p>
            </div>
          </div>

          <FormField label="Code from the app" error={error ?? undefined}>
            <CodeInput
              value={code}
              status={status}
              disabled={!enrolment}
              onValueChange={(next) => {
                setCode(next);
                if (status !== "idle") setStatus("idle");
              }}
              onComplete={(full) => void confirm(full)}
            />
          </FormField>
        </div>
      )}
    </EntryShell>
  );
}

function Finish({ codes, onDone }: { codes: string[] | null; onDone: () => Promise<void> }) {
  const [saved, setSaved] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  async function finish() {
    setBusy(true);
    setError(null);
    try {
      await setupApi.complete();
      await onDone();
    } catch (caught) {
      setError(caught instanceof ApiError ? caught.message : "Could not reach the server.");
      setBusy(false);
    }
  }

  return (
    <EntryShell wide>
      <Steps at={3} />
      <EntryHeading eyebrow="First run" title={codes ? "Save your recovery codes" : "Finish setting up"}>
        {codes ? (
          <>
            If you lose your phone, each of these gets you in once. They are not
            shown again — put them somewhere that is not this computer.
          </>
        ) : (
          <>
            Two-factor is already on for this account. Its recovery codes were
            shown when it was turned on; new ones can be made from Account.
          </>
        )}
      </EntryHeading>

      <div className="grid gap-6">
      {codes ? (
        <div>
          <ol className="grid grid-cols-2 gap-2 rounded-lg border border-border bg-bg-sunken p-4 font-mono text-sm text-fg">
            {codes.map((one, i) => (
              <li key={one} className="entry-code" style={{ "--entry-code-i": i } as React.CSSProperties}>
                {one}
              </li>
            ))}
          </ol>
          <div className="mt-3 flex gap-4 text-sm">
            <button
              type="button"
              className="entry-link"
              onClick={() => {
                void navigator.clipboard?.writeText(codes.join("\n"));
                setCopied(true);
              }}
            >
              {copied ? "Copied" : "Copy all"}
            </button>
            <button
              type="button"
              className="entry-link"
              onClick={() => {
                const blob = new Blob(
                  [`Bindery recovery codes — each works once.\n\n${codes.join("\n")}\n`],
                  { type: "text/plain" },
                );
                const url = URL.createObjectURL(blob);
                const a = Object.assign(document.createElement("a"), {
                  href: url,
                  download: "bindery-recovery-codes.txt",
                });
                a.click();
                URL.revokeObjectURL(url);
              }}
            >
              Download
            </button>
          </div>
        </div>
      ) : null}

      {codes ? (
        <Checkbox
          checked={saved}
          onCheckedChange={(value) => setSaved(value === true)}
          label="I have saved these somewhere safe"
        />
      ) : null}

      {error ? (
        <Alert tone="danger" dynamic>
          {error}
        </Alert>
      ) : null}

      <Button
        variant="primary"
        size="lg"
        className="w-full"
        disabled={codes !== null && !saved}
        loading={busy}
        onClick={() => void finish()}
      >
        Open the archive
      </Button>
      </div>
    </EntryShell>
  );
}
