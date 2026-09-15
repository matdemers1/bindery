import { useState } from "react";
import { Button, CodeInput, FormField, Input, PasswordInput, type CodeInputStatus } from "@d3cloud/ui";

import { ApiError, accountsApi } from "../../api";
import { EntryHeading, EntryShell } from "../entry/EntryShell";
import { retryMessage } from "../entry/messages";
import { judgePassword } from "../entry/strength";

/**
 * The other half of an administrator's reset (REQ-136).
 *
 * The issuing half shipped and this did not, which made the People screen's own
 * instruction — "this account can issue a password reset for every other one" —
 * an instruction to nowhere: a code was generated, read out over the phone, and
 * there was no form to type it into. On a self-hosted archive with no email and
 * no other recovery path, that is an account lost until somebody gets a shell on
 * the host, which is precisely the situation this project designs against.
 *
 * Unauthenticated, on the open internet, and handling a credential — so it
 * behaves like the login page it sits beside. The code is typed, never carried
 * in the URL, because a query string is written to history, to proxy logs and to
 * whatever the browser syncs. `POST /api/account/reset` throttles by address and
 * by IP, and answers "that code is not valid" to every way of being wrong, so
 * this screen deliberately does not improve on the message: telling somebody
 * *which* half was wrong tells an anonymous caller whether an account exists.
 *
 * The code is twelve boxes in three groups of four, as it was read out. The
 * server ignores the dashes, so what the boxes send is accepted as written.
 */
export default function ResetPage() {
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [codeStatus, setCodeStatus] = useState<CodeInputStatus>("idle");
  const [password, setPassword] = useState("");
  const [errors, setErrors] = useState<{ code?: string; password?: string; form?: string }>({});
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);

  async function submit(event: React.FormEvent) {
    event.preventDefault();
    setBusy(true);
    setErrors({});
    try {
      await accountsApi.redeemReset(email, code, password);
      setDone(true);
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 429) {
        setErrors({ form: retryMessage(caught.retryAfter) });
      } else if (caught instanceof ApiError && caught.status === 422) {
        // The one case where the server's own words are the useful ones: it
        // says what is wrong with the password that was chosen.
        setErrors({ password: caught.message });
      } else if (caught instanceof ApiError && caught.status === 400) {
        setErrors({
          code: "That code was not accepted. Codes expire after a day and each works once — ask for a new one.",
        });
        setCodeStatus("error");
      } else {
        setErrors({ form: "Could not reach the server." });
      }
    } finally {
      setBusy(false);
    }
  }

  if (done) {
    return (
      <EntryShell>
        <EntryHeading eyebrow="Done" title="Password changed">
          Everywhere that was signed in as you has been signed out. Sign in again
          with the new password.
        </EntryHeading>
        {/* A whole-document load rather than a router navigation: the router
            lives inside the signed-in tree, which does not exist yet. */}
        <a href="/" className="entry-button entry-button--primary">
          Sign in
        </a>
      </EntryShell>
    );
  }

  return (
    <EntryShell>
      <EntryHeading title="Use a reset code">
        Whoever runs this archive can issue one from People. They never see the
        password you choose here.
      </EntryHeading>

      <form onSubmit={submit} className="grid gap-5">
        <FormField label="Email">
          <Input
            size="lg"
            type="email"
            required
            autoComplete="username"
            autoFocus
            value={email}
            onChange={(event) => setEmail(event.target.value)}
          />
        </FormField>

        <FormField label="Reset code" error={errors.code} help="Works once, for a day.">
          <CodeInput
            mode="alphanumeric"
            length={12}
            groups={[4, 4, 4]}
            autoComplete="off"
            value={code}
            status={codeStatus}
            onValueChange={(next) => {
              setCode(next);
              if (codeStatus !== "idle") setCodeStatus("idle");
            }}
          />
        </FormField>

        <FormField
          label="New password"
          error={errors.password ?? errors.form}
          help="At least 12 characters. Four unrelated words beat one clever word."
        >
          <PasswordInput
            size="lg"
            required
            minLength={12}
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            strength={judgePassword(password, email)}
          />
        </FormField>

        <Button variant="primary" size="lg" type="submit" className="w-full" loading={busy}>
          Set new password
        </Button>
      </form>

      <p className="mt-6 border-t border-border pt-5 text-sm">
        <a href="/" className="entry-link">
          Back to sign in
        </a>
      </p>
    </EntryShell>
  );
}
