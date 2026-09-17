import { useRef, useState } from "react";
import { Button, CodeInput, FormField, Input, PasswordInput, type CodeInputStatus } from "@d3cloud/ui";

import { ApiError, api } from "../api";
import { EntryHeading, EntryShell } from "../features/entry/EntryShell";
import { SignInWithD3Auth } from "../features/entry/SignInWithD3Auth";
import { retryMessage, ssoMessage } from "../features/entry/messages";

type Step = "password" | "code";
type CodeKind = "totp" | "recovery";

/**
 * Sign in (Phase 19, REQ-202).
 *
 * Two steps, and the second only exists for an account with two-factor on. The
 * server asks for it only after the password was right, so moving to the code
 * screen tells an anonymous caller nothing they did not already have.
 *
 * The code submits itself when the last box is filled — paste a code, or let
 * the phone's "from your authenticator" suggestion fill it, and there is nothing
 * left to press. A rejected code shakes, says so in words, and clears itself
 * once the shake has finished, so the next attempt starts from an empty row.
 */
export default function Login({ onSignedIn }: { onSignedIn: () => Promise<void> }) {
  const [step, setStep] = useState<Step>("password");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [code, setCode] = useState("");
  const [codeKind, setCodeKind] = useState<CodeKind>("totp");
  const [codeStatus, setCodeStatus] = useState<CodeInputStatus>("idle");
  // A refused sign-in through the provider comes back as a redirect, so its message belongs to
  // the first render rather than to an effect.
  const [error, setError] = useState<string | null>(() => ssoMessage(window.location.search));
  const [busy, setBusy] = useState(false);
  const clearing = useRef<number | undefined>(undefined);

  async function attempt(withCode?: string) {
    setBusy(true);
    setError(null);
    try {
      await api.login(email, password, withCode);
      if (withCode) setCodeStatus("success");
      await onSignedIn();
    } catch (caught) {
      if (caught instanceof ApiError && caught.status === 429) {
        setError(retryMessage(caught.retryAfter));
        if (withCode) reject();
      } else if (
        caught instanceof ApiError &&
        caught.status === 401 &&
        caught.message.includes("second factor")
      ) {
        if (step === "password") {
          setStep("code");
        } else {
          setError(
            codeKind === "totp"
              ? "That code was not accepted. Codes change every 30 seconds — try the one showing now."
              : "That recovery code was not accepted. Each one works once.",
          );
          reject();
        }
      } else if (caught instanceof ApiError && caught.status === 401) {
        setError("That email and password do not match an account here.");
      } else {
        setError("Could not reach the server.");
      }
    } finally {
      setBusy(false);
    }
  }

  function reject() {
    setCodeStatus("error");
    // Cleared after the shake, not during it: the row empties once it has
    // finished saying no, and focus stays in the field for the next try.
    window.clearTimeout(clearing.current);
    clearing.current = window.setTimeout(() => setCode(""), 460);
  }

  function changeCode(next: string) {
    setCode(next);
    if (codeStatus !== "idle") setCodeStatus("idle");
  }

  if (step === "code") {
    const recovery = codeKind === "recovery";
    return (
      <EntryShell>
        <EntryHeading eyebrow="Two-step check" title={recovery ? "Use a recovery code" : "Enter your code"}>
          {recovery ? (
            <>One of the codes you saved when you turned on two-factor. Each works once.</>
          ) : (
            <>
              From the authenticator app on your phone, for{" "}
              <span className="font-medium text-fg">{email}</span>.
            </>
          )}
        </EntryHeading>

        <form
          onSubmit={(event) => {
            event.preventDefault();
            void attempt(code);
          }}
          // Spacing from the parent: kit components carry no margin by design.
          className="grid gap-6"
        >
          <FormField
            label={recovery ? "Recovery code" : "Six-digit code"}
            error={error ?? undefined}
          >
            <CodeInput
              key={codeKind}
              autoFocus
              value={code}
              status={codeStatus}
              onValueChange={changeCode}
              // Not disabled while checking: a disabled field drops focus, and
              // after a rejection the next try should start without a click.
              onComplete={(full) => {
                if (!busy) void attempt(full);
              }}
              {...(recovery
                ? { mode: "alphanumeric" as const, length: 10, groups: [5, 5], autoComplete: "off" }
                : { mode: "numeric" as const, length: 6 })}
            />
          </FormField>

          <Button variant="primary" size="lg" type="submit" className="w-full" loading={busy}>
            Confirm
          </Button>
        </form>

        <div className="mt-6 flex flex-wrap items-center justify-between gap-3 border-t border-border pt-5 text-sm">
          <button
            type="button"
            className="entry-link"
            onClick={() => {
              setCodeKind(recovery ? "totp" : "recovery");
              setCode("");
              setCodeStatus("idle");
              setError(null);
            }}
          >
            {recovery ? "Use my authenticator instead" : "Lost your phone? Use a recovery code"}
          </button>
          <button
            type="button"
            className="entry-link entry-link--quiet"
            onClick={() => {
              setStep("password");
              setCode("");
              setCodeKind("totp");
              setCodeStatus("idle");
              setError(null);
            }}
          >
            Start over
          </button>
        </div>
      </EntryShell>
    );
  }

  return (
    <EntryShell>
      <EntryHeading title="Sign in">Welcome back to the archive.</EntryHeading>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          void attempt();
        }}
        className="grid gap-5"
      >
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

        <FormField label="Password" error={error ?? undefined}>
          <PasswordInput
            size="lg"
            required
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
          />
        </FormField>

        <Button variant="primary" size="lg" type="submit" className="w-full" loading={busy}>
          Sign in
        </Button>
      </form>

      {/* Below the password form, and only when an operator has configured it. */}
      <div className="mt-5">
        <SignInWithD3Auth />
      </div>

      <div className="mt-6 grid gap-2 border-t border-border pt-5 text-sm leading-relaxed text-fg-muted">
        <p>
          Locked out?{" "}
          {/* A whole-document load: /reset is handled above the router. */}
          <a href="/reset" className="entry-link">
            Use a reset code
          </a>{" "}
          from whoever runs this archive.
        </p>
        <p>New here? Accounts are by invitation — open the link you were sent.</p>
      </div>
    </EntryShell>
  );
}

