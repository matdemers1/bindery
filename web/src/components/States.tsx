import type { ReactNode } from "react";

import { ApiError } from "../api";
import { Alert, Button } from "@d3cloud/ui";

/**
 * The four states every screen owes the reader (T-8.7).
 *
 * They exist as shared components because the wording matters more than the
 * markup and was drifting: a screen that says "Error" has told the reader
 * nothing they can act on, and a spinner with no label leaves them unsure
 * whether anything is happening at all.
 *
 * `Denied` is the one usually missing. A 403 in a household archive is not a
 * bug — it is the boundary working — and it should read that way rather than as
 * a failure the reader caused.
 */
export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <p role="status" aria-live="polite" className="py-6 text-sm text-muted">
      {label}
    </p>
  );
}

export function Skeleton({ rows = 3 }: { rows?: number }) {
  return (
    <ul className="space-y-3" aria-hidden>
      {Array.from({ length: rows }, (_, n) => (
        <li key={n} className="h-20 animate-pulse rounded-lg border border-edge bg-surface" />
      ))}
    </ul>
  );
}

export function Empty({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <div className="rounded-lg border border-edge bg-surface p-6">
      <p className="text-sm font-medium">{title}</p>
      {children && <div className="mt-1 max-w-2xl text-sm text-muted">{children}</div>}
    </div>
  );
}

export function Denied({ what = "this" }: { what?: string }) {
  return (
    <div className="rounded-lg border border-edge bg-surface p-6">
      <p className="text-sm font-medium">Not yours to see</p>
      <p className="mt-1 max-w-2xl text-sm text-muted">
        You do not have access to {what}. In Bindery access is per library, not per
        document, so this means you are not a member of the library it is in — ask
        an owner to add you on the{" "}
        <a href="/libraries" className="underline underline-offset-2">
          Libraries
        </a>{" "}
        screen.
      </p>
    </div>
  );
}

export function ErrorState({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry?: () => void;
}) {
  if (error instanceof ApiError && error.status === 403) {
    return <Denied />;
  }
  const message =
    error instanceof ApiError
      ? error.message
      : error instanceof Error
        ? error.message
        : String(error);

  return (
    <Alert
      tone="danger"
      dynamic
      title="That did not work."
      actions={
        onRetry ? (
          <Button variant="danger-ghost" size="sm" onClick={onRetry}>
            Try again
          </Button>
        ) : undefined
      }
    >
      {/* The server's message verbatim: it is written for a person, and a
          generic replacement would throw away the only diagnosis available. */}
      {message}
    </Alert>
  );
}
